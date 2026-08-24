"""
집행 데스크 — NautilusTrader.

목표비중과 현재 장부의 '차이만' 시장가 주문으로 만든다.
주문은 NautilusTrader 도메인 객체로 생성되지만, 브로커로 전송되지 않는다.
전송 경로 자체가 이 모듈에 존재하지 않는다 (설계상 의도).

■ 장부는 '비중'이 아니라 '주식 수 + 현금'으로 관리한다
비중 장부는 정수 주식 반올림·최소주문금액 때문에 실제 체결과 계속 어긋나고,
그 오차가 평가자산에 조용히 누적된다. 주식 수 장부는
  - 총 노출이 100%를 넘는 것이 구조적으로 불가능하고 (현금이 먼저 바닥난다)
  - 거래비용을 현금에서 직접 빼므로 성과가 낙관 편향되지 않으며
  - 가격이 결손된 종목도 마지막 알려진 가격으로 평가만 하고 포지션은 유지한다.
"""
import logging

import pandas as pd
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import (
    ClientOrderId, InstrumentId, StrategyId, Symbol, TraderId, Venue,
)
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import MarketOrder

from . import config

log = logging.getLogger("execution")


class ExecutionDesk:
    def __init__(self, equity=None):
        if config.EXECUTION_MODE != "paper" or config.ALLOW_LIVE_ORDERS:
            raise RuntimeError(
                "이 에이전트는 모의투자 전용입니다. 실거래를 하려면 브로커 커넥터, "
                "리스크 한도, 감사 로그, 그리고 해당 관할의 규제 검토가 별도로 필요합니다."
            )
        self.trader_id = TraderId(config.TRADER_ID)
        self.strategy_id = StrategyId(config.STRATEGY_ID)
        self.venue = Venue(config.VENUE)

        self.cash = float(equity if equity is not None else config.INITIAL_EQUITY)
        self.positions = {}        # 종목 -> 보유 주식 수 (정수)
        self.last_prices = {}      # 마지막으로 알려진 가격 (평가 폴백용, 주문에는 쓰지 않음)
        self.seq = 0
        self.degraded = []         # 이번 사이클에 가격을 못 받은 종목
        self.costs_paid = 0.0      # 누적 거래비용
        self.turnover = 0.0        # 이번 사이클 비중 이동 총량
        self.turnover_sell = 0.0   # 그중 기존 포지션을 줄인 양 (실질 교체량)

    # ---------------------------------------------------------------- 평가
    def observe_prices(self, prices):
        """관측된 가격으로 평가 기준을 갱신한다."""
        for tk, px in prices.items():
            if px and px > 0:
                self.last_prices[tk] = float(px)

    def _mark_price(self, tk, prices):
        """평가용 가격. 이번 사이클 가격이 없으면 마지막 알려진 가격으로 대신한다.

        주문 생성에는 절대 쓰지 않는다 — 낡은 가격으로 주문을 내면 안 되기 때문이다.
        """
        px = prices.get(tk)
        if px and px > 0:
            return float(px)
        return self.last_prices.get(tk)

    def market_value(self, prices):
        total = 0.0
        for tk, sh in self.positions.items():
            px = self._mark_price(tk, prices)
            if px is not None:
                total += sh * px
        return total

    def equity(self, prices):
        return self.cash + self.market_value(prices)

    def weights(self, prices):
        """현재 평가액 기준 비중 Series (현금은 포함하지 않으므로 합 <= 1)."""
        eq = self.equity(prices)
        if eq <= 0:
            return pd.Series(dtype=float)
        out = {}
        for tk, sh in self.positions.items():
            px = self._mark_price(tk, prices)
            if px is not None and sh > 0:
                out[tk] = sh * px / eq
        return pd.Series(out, dtype=float)

    def cash_weight(self, prices):
        eq = self.equity(prices)
        return (self.cash / eq) if eq > 0 else 1.0

    # ---------------------------------------------------------------- 집행
    def rebalance(self, target_weights, prices, ts):
        """목표비중으로 가는 주문을 만들고 모의 체결한다.

        반환: (주문 dict 리스트, 체결 후 비중 Series)
        """
        self.observe_prices(prices)
        self.degraded = []

        equity_before = self.equity(prices)
        weights_before = self.weights(prices)
        target = {tk: float(w) for tk, w in target_weights.items() if float(w) > 0}

        # 목표 유니버스 ∪ 현재 보유 = 이번에 손댈 종목 전부.
        # 보유 종목을 빠뜨리면 유니버스에서 탈락한 종목의 청산 주문이 누락되고
        # 포지션이 장부에서 조용히 증발한다 (과거 회귀 버그).
        universe = set(target) | {tk for tk, sh in self.positions.items() if sh > 0}

        plan = []
        for tk in sorted(universe):
            px = prices.get(tk)
            held = self.positions.get(tk, 0)
            if not px or px <= 0:
                if held > 0 or target.get(tk, 0.0) > 0:
                    log.error("%s: 가격 없음 — 주문 생략 (보유 %d주 유지)", tk, held)
                    self.degraded.append(tk)
                continue
            want = int(target.get(tk, 0.0) * equity_before / px)
            delta = want - held
            if abs(delta) * px < config.MIN_ORDER_NOTIONAL:
                continue
            plan.append((tk, delta, float(px)))

        # 매도를 먼저 체결해 현금을 확보한 뒤 매수한다.
        # 이 순서가 아니면 현금이 모자라 매수가 잘리거나 레버리지가 생긴다.
        plan.sort(key=lambda r: r[1])

        orders = []
        for tk, delta, px in plan:
            if delta > 0:
                # 수수료까지 감안해 현금으로 살 수 있는 만큼만 산다 (레버리지 차단)
                affordable = int(self.cash / (px * (1.0 + config.TX_COST_BPS)))
                if affordable < delta:
                    log.warning("%s: 현금 부족 — 매수 %d주 → %d주로 축소",
                                tk, delta, max(affordable, 0))
                    delta = max(affordable, 0)
                if delta == 0:
                    continue

            shares = abs(delta)
            notional = shares * px
            cost = notional * config.TX_COST_BPS

            self.seq += 1
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            order = MarketOrder(
                trader_id=self.trader_id,
                strategy_id=self.strategy_id,
                instrument_id=InstrumentId(Symbol(tk), self.venue),
                client_order_id=ClientOrderId(f"O-{self.seq:06d}"),
                order_side=side,
                quantity=Quantity.from_int(shares),
                init_id=UUID4(),
                ts_init=int(pd.Timestamp(ts, tz="UTC").value),
                time_in_force=TimeInForce.DAY,
            )

            # 모의 체결 — 시장가가 현재가에 전량 체결됐다고 보고 장부에 반영한다.
            # 슬리피지는 모델링하지 않으므로 실제보다 유리한 체결임을 잊지 말 것.
            self.positions[tk] = self.positions.get(tk, 0) + delta
            self.cash -= delta * px      # 매수면 현금 감소, 매도면 증가
            self.cash -= cost            # 거래비용은 양방향 모두 현금에서 나간다
            self.costs_paid += cost

            orders.append({
                "ts": pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M"),
                "client_order_id": str(order.client_order_id),
                "instrument": str(order.instrument_id),
                "ticker": tk,
                "side": order.side_string(),
                "quantity": shares,
                "price": round(px, 2),
                "notional": round(notional, 2),
                "cost": round(cost, 2),
                "status": "SIMULATED",     # 전송되지 않음
            })

        # 0주가 된 종목은 장부에서 지운다
        self.positions = {tk: sh for tk, sh in self.positions.items() if sh > 0}

        weights_after = self.weights(prices)
        # 실현 회전율. 두 정의를 같이 남긴다 —
        #   turnover      : 비중이 움직인 총량 (최초 편입도 100%로 잡힌다)
        #   turnover_sell : 기존 포지션을 갈아엎은 양. 게이트와 성과 통계는 이 쪽을 쓴다.
        idx = weights_before.index.union(weights_after.index)
        diff = weights_after.reindex(idx).fillna(0.0) - weights_before.reindex(idx).fillna(0.0)
        self.turnover = float(diff.abs().sum())
        self.turnover_sell = float((-diff).clip(lower=0).sum())

        log.info("리밸런스: 주문 %d건, 보유 %d종목, 현금 %.1f%%, 회전율 %.1f%%, 자산 $%.0f%s",
                 len(orders), len(self.positions), self.cash_weight(prices) * 100,
                 self.turnover * 100, self.equity(prices),
                 f" [degraded: {self.degraded}]" if self.degraded else "")
        return orders, weights_after

    # ---------------------------------------------------------------- 상태 직렬화
    def snapshot(self, prices):
        return {
            "cash": round(self.cash, 2),
            "cash_weight": round(self.cash_weight(prices), 6),
            "positions": dict(self.positions),
            "costs_paid": round(self.costs_paid, 2),
            "turnover": round(self.turnover, 6),
            "turnover_sell": round(self.turnover_sell, 6),
        }

    def restore(self, cash, positions, seq=0, costs_paid=0.0, last_prices=None):
        self.cash = float(cash)
        self.positions = {tk: int(sh) for tk, sh in (positions or {}).items() if int(sh) > 0}
        self.seq = int(seq or 0)
        self.costs_paid = float(costs_paid or 0.0)
        self.last_prices = {k: float(v) for k, v in (last_prices or {}).items()}
