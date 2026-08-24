"""
집행 데스크 회귀 테스트.

과거에 두 번 물린 유형을 중심으로 짰다.
  1) 유니버스에서 탈락한 종목의 청산 주문이 누락되어 포지션이 장부에서 증발
  2) 부분 실패(가격 결손) 상태에서 총 노출이 100%를 넘어 레버리지 발생

주식 수 + 현금 장부로 바꾼 뒤에는 두 사고 모두 구조적으로 막히지만,
'구조적으로 막힌다'는 주장 자체가 테스트로 고정돼 있어야 의미가 있다.
"""
import pandas as pd
import pytest

from core import config
from core.execution_desk import ExecutionDesk

PRICES = {"AAA": 100.0, "BBB": 50.0, "CCC": 25.0}


def w(**kwargs):
    return pd.Series(kwargs, dtype=float)


def make_desk(equity=100_000.0):
    return ExecutionDesk(equity=equity)


def total_exposure(desk, prices):
    return float(desk.weights(prices).sum())


# ------------------------------------------------------------------ 기본 동작
def test_최초편입은_현금을_주식으로_바꾼다():
    d = make_desk()
    orders, weights = d.rebalance(w(AAA=0.5, BBB=0.5), PRICES, "2026-01-02")

    assert {o["ticker"] for o in orders} == {"AAA", "BBB"}
    assert all(o["side"] == "BUY" for o in orders)
    assert d.positions["AAA"] > 0 and d.positions["BBB"] > 0
    assert d.cash >= 0
    assert total_exposure(d, PRICES) <= 1.0 + 1e-9


def test_주문수량과_장부가_정확히_일치한다():
    """정수 주식 반올림 후에도 장부가 실제 체결을 그대로 반영해야 한다."""
    d = make_desk()
    orders, _ = d.rebalance(w(AAA=0.5, BBB=0.3), PRICES, "2026-01-02")

    filled = {}
    for o in orders:
        sign = 1 if o["side"] == "BUY" else -1
        filled[o["ticker"]] = filled.get(o["ticker"], 0) + sign * o["quantity"]
    assert filled == d.positions


def test_거래비용이_현금에서_실제로_빠진다():
    d = make_desk()
    orders, _ = d.rebalance(w(AAA=0.5), PRICES, "2026-01-02")

    expected = sum(o["notional"] * config.TX_COST_BPS for o in orders)
    assert d.costs_paid == pytest.approx(expected, rel=1e-9)
    assert d.costs_paid > 0
    # 가격이 그대로면 자산은 정확히 거래비용만큼 줄어야 한다
    assert d.equity(PRICES) == pytest.approx(100_000.0 - d.costs_paid, rel=1e-9)


def test_최소주문금액_미만은_주문하지_않는다():
    d = make_desk()
    d.rebalance(w(AAA=0.5), PRICES, "2026-01-02")
    before = dict(d.positions)

    # 0.1%p 조정 = $100 → MIN_ORDER_NOTIONAL(500) 미만
    orders, _ = d.rebalance(w(AAA=0.501), PRICES, "2026-01-03")
    assert orders == []
    assert d.positions == before


# ------------------------------------------- 회귀 1: 탈락 종목 포지션 증발
def test_유니버스에서_탈락한_종목은_청산된다():
    d = make_desk()
    d.rebalance(w(AAA=0.4, BBB=0.4), PRICES, "2026-01-02")
    assert "BBB" in d.positions

    # BBB 가 목표에서 통째로 빠짐 — 청산 주문이 반드시 나와야 한다
    orders, weights = d.rebalance(w(AAA=0.8), PRICES, "2026-01-03")

    sells = [o for o in orders if o["side"] == "SELL" and o["ticker"] == "BBB"]
    assert sells, "탈락 종목의 청산 주문이 생성되지 않았다 (포지션 증발)"
    assert "BBB" not in d.positions
    assert "BBB" not in weights.index


def test_탈락종목이_가격맵에_없어도_노출이_늘지_않는다():
    """가격이 결손된 종목은 청산할 수 없다. 그때도 레버리지가 생기면 안 된다."""
    d = make_desk()
    d.rebalance(w(AAA=0.4, BBB=0.4), PRICES, "2026-01-02")

    partial = {"AAA": 100.0}          # BBB 가격 누락
    orders, weights = d.rebalance(w(AAA=0.9), partial, "2026-01-03")

    assert "BBB" in d.degraded, "가격 결손이 degraded 로 보고되지 않았다"
    assert d.positions.get("BBB", 0) > 0, "가격이 없는데 포지션이 사라졌다"
    assert total_exposure(d, PRICES) <= 1.0 + 1e-9
    assert d.cash >= 0


# ------------------------------------------- 회귀 2: 레버리지 / 현금 불변식
@pytest.mark.parametrize("target", [
    {"AAA": 1.0},
    {"AAA": 0.6, "BBB": 0.4},
    {"AAA": 0.34, "BBB": 0.33, "CCC": 0.33},
])
def test_현금은_어떤_목표에서도_음수가_되지_않는다(target):
    d = make_desk()
    d.rebalance(pd.Series(target, dtype=float), PRICES, "2026-01-02")
    assert d.cash >= 0
    assert total_exposure(d, PRICES) <= 1.0 + 1e-9


def test_목표합이_1을_넘어도_레버리지가_생기지_않는다():
    """옵티마이저 버그로 합이 1을 넘는 목표가 들어와도 현금이 먼저 바닥난다."""
    d = make_desk()
    d.rebalance(w(AAA=0.8, BBB=0.8), PRICES, "2026-01-02")
    assert d.cash >= 0
    assert total_exposure(d, PRICES) <= 1.0 + 1e-9


def test_매도가_매수보다_먼저_체결된다():
    """매수를 먼저 하면 현금이 모자라 목표에 도달하지 못한다."""
    d = make_desk()
    d.rebalance(w(AAA=0.9), PRICES, "2026-01-02")

    # AAA 를 거의 다 팔고 BBB 로 갈아탄다
    orders, _ = d.rebalance(w(BBB=0.85), PRICES, "2026-01-03")
    sides = [o["side"] for o in orders]
    assert sides.index("SELL") < sides.index("BUY")
    assert d.cash >= 0


# ------------------------------------------------------------------ 평가
def test_가격이_오르면_자산이_늘고_비중이_따라간다():
    d = make_desk()
    d.rebalance(w(AAA=0.5, BBB=0.5), PRICES, "2026-01-02")
    before = d.equity(PRICES)

    up = dict(PRICES, AAA=110.0)      # AAA +10%
    assert d.equity(up) > before
    assert d.weights(up)["AAA"] > d.weights(PRICES)["AAA"]


def test_가격이_결손되면_마지막_알려진_가격으로_평가한다():
    d = make_desk()
    d.rebalance(w(AAA=0.5, BBB=0.5), PRICES, "2026-01-02")
    full = d.equity(PRICES)

    # BBB 가격이 빠져도 평가액이 통째로 사라지면 안 된다
    assert d.equity({"AAA": 100.0}) == pytest.approx(full, rel=1e-9)


def test_회전율은_매도측_교체량을_반영한다():
    d = make_desk()
    d.rebalance(w(AAA=0.8), PRICES, "2026-01-02")
    assert d.turnover > 0            # 최초 편입도 비중 이동은 있었다

    d.rebalance(w(AAA=0.8), PRICES, "2026-01-03")
    assert d.turnover == pytest.approx(0.0, abs=1e-6)   # 그대로면 회전 없음


# ------------------------------------------------------------------ 안전장치
def test_실거래_모드는_시작을_거부한다(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_LIVE_ORDERS", True)
    with pytest.raises(RuntimeError, match="모의투자 전용"):
        ExecutionDesk()

    monkeypatch.setattr(config, "ALLOW_LIVE_ORDERS", False)
    monkeypatch.setattr(config, "EXECUTION_MODE", "live")
    with pytest.raises(RuntimeError, match="모의투자 전용"):
        ExecutionDesk()


def test_모든_주문은_전송되지_않은_상태로_표시된다():
    d = make_desk()
    orders, _ = d.rebalance(w(AAA=0.5, BBB=0.3), PRICES, "2026-01-02")
    assert orders
    assert all(o["status"] == "SIMULATED" for o in orders)


def test_상태_복원이_장부를_그대로_되살린다():
    d = make_desk()
    d.rebalance(w(AAA=0.5, BBB=0.3), PRICES, "2026-01-02")
    snap = d.snapshot(PRICES)
    equity = d.equity(PRICES)

    restored = make_desk()
    restored.restore(cash=snap["cash"], positions=snap["positions"],
                     seq=d.seq, costs_paid=snap["costs_paid"])
    assert restored.positions == d.positions
    assert restored.equity(PRICES) == pytest.approx(equity, rel=1e-6)
    assert restored.seq == d.seq
