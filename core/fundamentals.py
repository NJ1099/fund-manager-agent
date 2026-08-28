"""
펀더멘털 수집 — 애널리스트 컨센서스 · 밸류에이션 · 수익성 · 재무 건전성.

`core/verdict.py` 가 판정의 재료로 쓴다. yfinance 의 `Ticker.info` 하나에서
필요한 것이 거의 다 나온다.

■ 실측으로 알아낸 것 (2026-08-28)

| 대상 | 나오는 항목 |
|---|---|
| 미국 개별주 (AAPL) | 17/17 — 전부 |
| 한국 개별주 (005930.KS) | 15/17 — `trailingPE`·`priceToBook` 만 결측 |
| 코스닥 개별주 (247540.KQ) | 14/17 |
| **ETF (133690.KS)** | **0/17** |
| 미국 ETF (SPY) | 3/17 — PER·PBR·배당만 |

**ETF 는 펀더멘털이 없다.** 당연하다 — ETF 는 회사가 아니라 바구니라서 ROE 도
부채비율도 없다. 이걸 "데이터 없음"이 아니라 **"해당 없음"** 으로 구분해서 보고한다.
없는 값을 0 으로 채우면 "부채가 없는 우량 종목"으로 잘못 읽힌다.

■ 애널리스트 목표가를 어떻게 다룰 것인가

`targetMeanPrice` 는 이 프로젝트에서 유일하게 남은 **예측**이다. 그리고 예측인 이상
Kronos 와 같은 의심을 받아야 한다. 알려진 성질:

- **낙관 편향이 크다.** 증권사 목표가는 압도적으로 상승 방향이고, 매도 의견은 드물다.
- 그래도 Kronos 와 다른 점이 하나 있다 — **사람이 근거를 갖고 낸 숫자**이고,
  몇 명이 냈는지(`numberOfAnalystOpinions`)를 알 수 있어 신뢰도를 가늠할 수 있다.

그래서 여기서는 목표가를 **버리지도 믿지도 않고 '예측' 라벨을 붙여 그대로 전달**한다.
판정에서의 가중치도 낮게 둔다(`verdict.py` 참고). 편향을 화면에서 명시한다.

■ 캐시

`info` 는 종목당 1~2초 걸리는 네트워크 호출이다. 보유 10종목이면 20초다.
같은 날 같은 종목을 다시 물을 이유가 없으므로 **날짜 단위로 메모리에 캐시**한다.
장중에 재무제표가 바뀌지 않으므로 하루 캐시는 안전하다.
"""
import logging
from datetime import date

log = logging.getLogger("fundamentals")

# 가져올 항목 — yfinance 키 그대로. 없으면 None 으로 남긴다.
FIELDS = [
    "shortName", "longName", "sector", "industry", "currency", "quoteType",
    "marketCap",
    # 애널리스트 (예측)
    "targetMeanPrice", "targetHighPrice", "targetLowPrice",
    "recommendationKey", "numberOfAnalystOpinions",
    # 밸류에이션
    "trailingPE", "forwardPE", "priceToBook", "dividendYield",
    # 수익성 · 성장
    "returnOnEquity", "profitMargins", "revenueGrowth", "earningsGrowth",
    # 재무 건전성 · 위험
    "debtToEquity", "beta", "freeCashflow",
]

# 이 항목들은 ETF 에 애초에 존재하지 않는다 — '결측'이 아니라 '해당 없음'이다.
NOT_APPLICABLE_FOR_FUNDS = {
    "returnOnEquity", "profitMargins", "revenueGrowth", "earningsGrowth",
    "debtToEquity", "freeCashflow", "targetMeanPrice", "targetHighPrice",
    "targetLowPrice", "recommendationKey", "numberOfAnalystOpinions",
    "trailingPE", "forwardPE",
}

FUND_TYPES = {"ETF", "MUTUALFUND", "INDEX"}

_cache = {}          # (ticker, 날짜) → dict


def _is_fund(info):
    """ETF·펀드인가. `quoteType` 이 없으면 이름으로 추정한다(국내 ETF 는 비어 있을 때가 있다)."""
    qt = (info.get("quoteType") or "").upper()
    if qt:
        return qt in FUND_TYPES
    name = f"{info.get('shortName') or ''} {info.get('longName') or ''}".upper()
    return any(k in name for k in ("ETF", "KODEX", "TIGER", "KBSTAR", "ARIRANG",
                                   "PLUS ", "SOL ", "ACE "))


def fetch_one(ticker):
    """한 종목의 펀더멘털. 실패하면 예외를 올리지 않고 사유를 담아 돌려준다.

    한 종목이 안 나온다고 화면 전체를 실패시킬 이유가 없다 — 다만 **조용히 빈
    값으로 두지는 않는다.** `error` 나 `not_applicable` 로 이유를 남긴다.
    """
    key = (ticker, date.today().isoformat())
    if key in _cache:
        return dict(_cache[key])

    out = {"ticker": ticker, "fields": {}, "missing": [], "not_applicable": [],
           "is_fund": False, "error": None}
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        log.warning("%s 펀더멘털 조회 실패: %s", ticker, e)
        return out

    if not info:
        out["error"] = "응답이 비어 있습니다"
        return out

    out["is_fund"] = _is_fund(info)
    for f in FIELDS:
        v = info.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            # ETF 에 없는 항목은 '결측'이 아니라 '해당 없음'이다.
            if out["is_fund"] and f in NOT_APPLICABLE_FOR_FUNDS:
                out["not_applicable"].append(f)
            elif f not in ("longName", "sector", "industry"):
                out["missing"].append(f)
            continue
        out["fields"][f] = v

    _cache[key] = dict(out)
    return out


def fetch(tickers):
    """여러 종목. 실패한 종목도 결과에 남긴다(사유 포함)."""
    return {tk: fetch_one(tk) for tk in tickers}


def clear_cache():
    _cache.clear()


# ------------------------------------------------------------------ 파생값
def upside_pct(fields, price):
    """애널리스트 평균 목표가 대비 상승여력(%). 없으면 None.

    ⚠️ 이 값은 **예측**이다. 증권사 목표가는 낙관 편향이 크고 매도 의견이 드물다.
    화면에서 반드시 '예측' 라벨과 함께 보여줄 것.
    """
    t = fields.get("targetMeanPrice")
    if t is None or not price:
        return None
    return (float(t) / float(price) - 1.0) * 100.0


def pct(v):
    """yfinance 는 비율을 0~1 로 주기도 하고 %로 주기도 한다.

    `returnOnEquity` 0.308 은 30.8%지만 `dividendYield` 0.56 은 (한국 종목에서)
    이미 0.56% 다. 실측 기준으로 필드마다 다르므로 **여기서 일괄 변환하지 않는다** —
    호출부가 필드 성격을 알고 쓴다. 이 함수는 0~1 비율만 %로 바꾼다.
    """
    return None if v is None else float(v) * 100.0
