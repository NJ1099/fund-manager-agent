"""
실제 보유 종목 장부.

■ 이것은 봇의 장부가 아니다 — 절대 섞지 말 것

이 프로젝트에는 장부가 **두 개** 있고, 둘은 완전히 분리돼 있어야 한다.

| | `execution_desk.ExecutionDesk` | `holdings.HoldingsBook` (이 모듈) |
|---|---|---|
| 무엇 | 봇이 모의로 굴리는 포트폴리오 | 사용자가 **실제 증권사에서 산** 종목 |
| 누가 바꾸나 | 사이클마다 봇이 자동으로 | 사용자가 직접, 또는 증권사 조회로 |
| 파일 | `state/state.json` | `state/holdings.json` |
| 주문 | 모의 주문을 만든다 | **주문을 만들지 않는다** |

섞이면 두 가지가 동시에 망가진다. 첫째, 봇의 장부 불변식(총 노출 100% 이하,
주문 수량 합 = 포지션 변화량)이 사용자가 손으로 넣은 수량 때문에 깨진다.
둘째, 사용자가 "내 실제 보유가 봇의 매매 판단에 반영된다"고 오해하게 된다 —
이 모듈은 **분석과 표시**에만 쓰이고, 어떤 목표비중에도 들어가지 않는다.

■ 통화

한국 주식(KRW)과 미국 주식(USD)이 한 계좌 안에 섞인다. 종목마다 통화를 기록하고,
합산할 때만 기준통화로 환산한다. 환율을 못 구하면 **0으로 채우지 않고** 그 종목을
합산에서 빼고 `unconverted` 로 보고한다 — 0으로 채우면 자산이 조용히 줄어든다.
"""
import logging
from datetime import datetime, timezone

from . import config, storage

log = logging.getLogger("holdings")

VALID_SOURCES = ("manual", "toss", "kis", "csv")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_ticker(ticker):
    """대문자 · 공백 제거. 한국 종목의 접미사(.KS/.KQ)는 그대로 둔다."""
    return str(ticker or "").strip().upper()


def make_holding(ticker, quantity, avg_cost=None, currency=None, account=None,
                 source="manual", note=None, name=None):
    """보유 한 줄. 수량이 0 이하면 보유가 아니다."""
    ticker = normalize_ticker(ticker)
    if not ticker:
        raise ValueError("종목 코드가 비어 있습니다")
    qty = float(quantity)
    if qty <= 0:
        raise ValueError(f"{ticker}: 수량은 0보다 커야 합니다 (받은 값 {quantity})")
    if source not in VALID_SOURCES:
        raise ValueError(f"알 수 없는 출처 '{source}' (가능: {', '.join(VALID_SOURCES)})")

    return {
        "ticker": ticker,
        "name": name,
        "quantity": qty,
        "avg_cost": float(avg_cost) if avg_cost not in (None, "") else None,
        "currency": (currency or infer_currency(ticker)).upper(),
        "account": account,
        "source": source,
        "note": note,
        "updated_at": _now(),
    }


def infer_currency(ticker):
    """접미사로 통화를 추정한다. 틀릴 수 있으므로 호출부가 덮어쓸 수 있게 둔다."""
    t = normalize_ticker(ticker)
    if t.endswith((".KS", ".KQ")):
        return "KRW"
    if t.endswith(".T"):
        return "JPY"
    if t.endswith((".L",)):
        return "GBP"
    if t.endswith((".HK",)):
        return "HKD"
    return "USD"


class HoldingsBook:
    """보유 종목 목록. 종목당 한 줄이며, 같은 종목이 여러 계좌에 있으면 합산한다."""

    def __init__(self, items=None, path=None):
        self.path = path or config.HOLDINGS_FILE
        self.items = {}
        for h in (items or []):
            self.items[normalize_ticker(h["ticker"])] = h

    # ---------------------------------------------------------------- 입출력
    @classmethod
    def load(cls, path=None):
        path = path or config.HOLDINGS_FILE
        if not path.exists():
            return cls(path=path)
        try:
            data = storage.read_json(path)
        except Exception as e:
            # 깨진 파일을 조용히 빈 장부로 대체하면 사용자의 입력이 증발한 것처럼 보인다.
            log.error("보유 장부를 읽지 못했습니다 (%s): %s", path, e)
            raise
        return cls(items=data.get("holdings", []), path=path)

    def save(self):
        storage.write_json(self.path, {
            "updated_at": _now(),
            "base_currency": config.HOLDINGS_BASE_CURRENCY,
            "holdings": list(self.items.values()),
        })
        return self.path

    # ---------------------------------------------------------------- 편집
    def upsert(self, **kwargs):
        """추가하거나 덮어쓴다. 같은 종목이 이미 있으면 교체한다."""
        h = make_holding(**kwargs)
        self.items[h["ticker"]] = h
        return h

    def add_lot(self, ticker, quantity, price, **kwargs):
        """매수 한 건을 더한다 — 수량은 합치고 평단은 가중평균으로 다시 계산한다.

        같은 종목을 여러 번 사는 것이 정상이므로, 덮어쓰기(upsert)만 있으면
        사용자가 직접 평단을 계산해야 한다.
        """
        ticker = normalize_ticker(ticker)
        qty, px = float(quantity), float(price)
        if qty <= 0:
            raise ValueError("추가 수량은 0보다 커야 합니다")

        old = self.items.get(ticker)
        if old is None or old.get("avg_cost") in (None, ""):
            return self.upsert(ticker=ticker, quantity=(old["quantity"] + qty) if old else qty,
                               avg_cost=px, **kwargs)

        total_qty = old["quantity"] + qty
        avg = (old["avg_cost"] * old["quantity"] + px * qty) / total_qty
        merged = dict(old)
        merged.update(quantity=total_qty, avg_cost=avg, updated_at=_now())
        merged.update({k: v for k, v in kwargs.items() if v is not None})
        self.items[ticker] = merged
        return merged

    def remove(self, ticker):
        return self.items.pop(normalize_ticker(ticker), None)

    def replace_source(self, source, items):
        """한 출처(증권사)의 보유를 통째로 갈아끼운다.

        증권사 조회는 '그 계좌의 현재 전부'를 돌려주므로, 팔아서 사라진 종목은
        장부에서도 사라져야 한다. 그런데 **다른 출처(수동 입력·다른 증권사)의
        줄까지 지우면 안 된다** — 그래서 출처별로만 교체한다.
        """
        kept = {tk: h for tk, h in self.items.items() if h.get("source") != source}
        incoming = {}
        for h in items:
            hh = dict(h)
            hh["source"] = source
            hh["updated_at"] = _now()
            incoming[normalize_ticker(hh["ticker"])] = hh

        removed = [tk for tk in self.items
                   if self.items[tk].get("source") == source and tk not in incoming]
        self.items = {**kept, **incoming}
        return {"synced": len(incoming), "removed": removed, "source": source}

    # ---------------------------------------------------------------- 조회
    @property
    def tickers(self):
        return sorted(self.items)

    def __len__(self):
        return len(self.items)

    def to_list(self):
        return [self.items[tk] for tk in self.tickers]

    def valuation(self, prices, fx=None):
        """평가액·손익·비중. 환산 불가한 종목은 합산에서 빼고 따로 보고한다.

        prices : {ticker: 현재가}  (종목 표기 통화 기준)
        fx     : {통화: 기준통화당 환율}  예) {"USD": 1350.0} → 1 USD = 1350 KRW
        """
        base = config.HOLDINGS_BASE_CURRENCY
        fx = dict(fx or {})
        fx.setdefault(base, 1.0)

        rows, total, unconverted, unpriced = [], 0.0, [], []
        for tk in self.tickers:
            h = self.items[tk]
            px = prices.get(tk)
            rate = fx.get(h["currency"])

            row = dict(h)
            row["price"] = px
            row["market_value"] = (px * h["quantity"]) if px else None
            row["cost_basis"] = (h["avg_cost"] * h["quantity"]) if h.get("avg_cost") else None
            row["pnl"] = (row["market_value"] - row["cost_basis"]
                          if row["market_value"] is not None and row["cost_basis"] is not None
                          else None)
            row["pnl_pct"] = (row["pnl"] / row["cost_basis"] * 100
                              if row["pnl"] is not None and row["cost_basis"] else None)
            row["fx_rate"] = rate
            row["market_value_base"] = (row["market_value"] * rate
                                        if row["market_value"] is not None and rate else None)

            if px is None:
                unpriced.append(tk)
            elif rate is None:
                unconverted.append(tk)
            else:
                total += row["market_value_base"]
            rows.append(row)

        for row in rows:
            mv = row.get("market_value_base")
            row["weight"] = (mv / total) if (mv is not None and total > 0) else None

        return {
            "base_currency": base,
            "total_value": round(total, 2),
            "rows": rows,
            # 조용히 빠뜨리지 않는다 — 화면에 경고로 띄우기 위한 목록이다
            "unpriced": unpriced,
            "unconverted": unconverted,
            "as_of": _now(),
        }
