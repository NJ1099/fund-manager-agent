"""
보유 종목 분석.

사용자가 실제로 산 종목에 대해 두 가지를 만든다.

1. **종목별 견해** — 봇이 워치리스트에 쓰는 것과 **같은 연구 데스크**(Kronos)를
   그대로 돌린다. 다른 계산기를 쓰면 "봇 화면의 QQQ 견해"와 "내 보유의 QQQ 견해"가
   달라져서 둘 다 못 믿게 된다.
2. **포트폴리오 진단** — 집중도·상관·통화 노출·손익. 개별 종목 예측보다 이쪽이
   실제로 더 쓸모 있다. 예측은 틀릴 수 있지만 "한 종목에 60%가 몰려 있다"는
   사실은 틀리지 않기 때문이다.

■ 이 모듈은 어떤 주문도, 어떤 목표비중도 만들지 않는다
읽고 설명할 뿐이다. 보유 종목이 봇의 매매 판단에 흘러 들어가지 않는다
(자세한 이유는 `core/holdings.py` 모듈 독스트링).

■ 견해가 없을 수 있다
Kronos 는 `LOOKBACK`(300) 봉이 필요하다. 상장한 지 얼마 안 된 종목, 데이터가 부실한
종목은 견해가 안 나온다. 그럴 때 **0 이나 '중립'으로 채우지 않고** 사유와 함께
빠뜨린다 — 지어낸 견해가 제일 위험하다.
"""
import logging
import math

import numpy as np
import pandas as pd

from . import config, data_desk, research_desk, symbol_search

log = logging.getLogger("holdings.analysis")

# 진단 기준선. 절대적 정답은 없고, '눈에 띄면 짚어주는' 문턱값이다.
CONCENTRATION_WARN = 0.35       # 한 종목이 이보다 크면 집중 경고
TOP3_WARN = 0.75
CORRELATION_WARN = 0.70         # 평균 쌍상관이 이보다 높으면 분산 효과가 약하다
HHI_WARN = 0.25                 # 허핀달 지수 (1/n 이 최소값)


def _fetch_bars(tickers, period="2y"):
    """분석 대상 종목의 봉. 실패한 종목은 조용히 빠지지 않고 목록으로 돌려준다."""
    if not tickers:
        return {}, list(tickers)
    bars = data_desk.fetch_bars(list(tickers), period=period)
    missing = [tk for tk in tickers if tk not in bars]
    return bars, missing


def views_for(tickers, bars=None, scorer=None):
    """보유 종목에 Kronos 견해를 매긴다.

    반환: (견해 리스트, {ticker: 제외 사유})
    """
    if not tickers:
        return [], {}

    if bars is None:
        bars, missing = _fetch_bars(tickers)
    else:
        missing = [tk for tk in tickers if tk not in bars]

    skipped = {tk: "가격 데이터를 받지 못했습니다" for tk in missing}

    usable = {}
    for tk in tickers:
        df = bars.get(tk)
        if df is None:
            continue
        if len(df) < config.LOOKBACK + 1:
            skipped[tk] = f"봉이 부족합니다 ({len(df)}개 < {config.LOOKBACK + 1}개)"
            continue
        usable[tk] = df

    if not usable:
        return [], skipped

    try:
        scores = research_desk.scan(usable, scorer=scorer)
    except Exception as e:
        log.error("견해 산출 실패: %s", e)
        for tk in usable:
            skipped[tk] = f"분석 실패: {e}"
        return [], skipped

    scored = {s["ticker"] for s in scores}
    for tk in usable:
        if tk not in scored:
            skipped[tk] = "분석 중 제외되었습니다"
    return scores, skipped


# ------------------------------------------------------------------ 진단
def concentration(weights):
    """집중도. weights = {ticker: 비중(0~1)}"""
    ws = {k: float(v) for k, v in weights.items() if v}
    if not ws:
        return None
    ordered = sorted(ws.values(), reverse=True)
    hhi = sum(w * w for w in ordered)
    top = ordered[0]
    top3 = sum(ordered[:3])

    flags = []
    if top >= CONCENTRATION_WARN:
        flags.append(f"단일 종목 비중 {top * 100:.1f}%")
    if top3 >= TOP3_WARN and len(ordered) > 3:
        flags.append(f"상위 3종목 {top3 * 100:.1f}%")
    if hhi >= HHI_WARN:
        flags.append(f"허핀달 지수 {hhi:.2f}")

    return {
        "n_positions": len(ordered),
        "top_weight_pct": round(top * 100, 2),
        "top3_weight_pct": round(top3 * 100, 2),
        "hhi": round(hhi, 4),
        # 유효 종목 수: 비중이 고르면 n, 한쪽에 쏠리면 1 에 가까워진다
        "effective_positions": round(1.0 / hhi, 2) if hhi > 0 else None,
        "flags": flags,
    }


def correlation(bars, tickers, window=None):
    """보유 종목 간 평균 쌍상관. 분산이 실제로 되고 있는지 본다.

    상관이 높으면 종목 수가 많아도 사실상 한 종목에 건 것과 비슷하다.
    """
    window = window or config.COV_WINDOW
    cols = [tk for tk in tickers if tk in bars]
    if len(cols) < 2:
        return None

    closes = pd.DataFrame({tk: bars[tk]["close"] for tk in cols}).dropna()
    if len(closes) < 30:
        return None
    rets = closes.pct_change().dropna().iloc[-window:]
    if len(rets) < 30:
        return None

    corr = rets.corr()
    vals, pairs = [], []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            try:
                c = float(corr.loc[a, b])
            except KeyError:
                continue
            if math.isnan(c):
                continue
            vals.append(c)
            pairs.append((a, b, c))
    if not vals:
        return None

    pairs.sort(key=lambda p: -p[2])
    avg = sum(vals) / len(vals)
    flags = []
    if avg >= CORRELATION_WARN:
        flags.append(f"평균 상관 {avg:.2f} — 종목 수만큼 분산되지 않습니다")

    return {
        "n_pairs": len(vals),
        "avg_correlation": round(avg, 3),
        "highest": [{"a": a, "b": b, "corr": round(c, 3)} for a, b, c in pairs[:3]],
        "lowest": [{"a": a, "b": b, "corr": round(c, 3)} for a, b, c in pairs[-3:]],
        "window_days": len(rets),
        "flags": flags,
    }


def portfolio_volatility(bars, weights, window=None):
    """비중 가중 포트폴리오의 연율 변동성. 상관까지 반영한 값이다."""
    window = window or config.COV_WINDOW
    cols = [tk for tk in weights if tk in bars and weights[tk]]
    if len(cols) < 1:
        return None

    closes = pd.DataFrame({tk: bars[tk]["close"] for tk in cols}).dropna()
    if len(closes) < 30:
        return None
    rets = closes.pct_change().dropna().iloc[-window:]
    if len(rets) < 30:
        return None

    w = np.array([weights[tk] for tk in cols], dtype=float)
    if w.sum() <= 0:
        return None
    w = w / w.sum()                      # 가격 결손 종목을 뺀 만큼 재정규화

    cov = rets.cov().values * 252
    var = float(w @ cov @ w)
    if var <= 0:
        return None

    individual = {tk: round(float(rets[tk].std() * math.sqrt(252) * 100), 2) for tk in cols}
    weighted_avg = sum(w[i] * individual[tk] for i, tk in enumerate(cols))
    port_vol = math.sqrt(var) * 100

    return {
        "portfolio_vol_annual_pct": round(port_vol, 2),
        # 가중평균보다 낮으면 그 차이가 분산 효과다
        "weighted_avg_vol_pct": round(weighted_avg, 2),
        "diversification_benefit_pct": round(weighted_avg - port_vol, 2),
        "by_ticker": individual,
        "window_days": len(rets),
    }


def currency_exposure(rows):
    """통화별 비중. 환헤지 없는 해외 비중이 얼마나 되는지 보는 용도."""
    out = {}
    for r in rows:
        mv = r.get("market_value_base")
        if mv is None:
            continue
        out[r["currency"]] = out.get(r["currency"], 0.0) + mv
    total = sum(out.values())
    if total <= 0:
        return None
    return {cur: round(v / total * 100, 2) for cur, v in sorted(out.items(), key=lambda kv: -kv[1])}


def pnl_summary(rows):
    """평단이 입력된 종목만으로 손익을 낸다. 없는 종목은 세지 않는다."""
    priced = [r for r in rows if r.get("pnl") is not None and r.get("fx_rate")]
    if not priced:
        return None
    total_cost = sum(r["cost_basis"] * r["fx_rate"] for r in priced)
    total_pnl = sum(r["pnl"] * r["fx_rate"] for r in priced)
    winners = [r["ticker"] for r in priced if r["pnl"] > 0]
    losers = [r["ticker"] for r in priced if r["pnl"] < 0]
    best = max(priced, key=lambda r: r["pnl_pct"])
    worst = min(priced, key=lambda r: r["pnl_pct"])
    return {
        "covered": len(priced),
        "uncovered": len(rows) - len(priced),      # 평단 미입력 종목 수
        "total_cost_base": round(total_cost, 2),
        "total_pnl_base": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost else None,
        "winners": len(winners), "losers": len(losers),
        "best": {"ticker": best["ticker"], "pnl_pct": round(best["pnl_pct"], 2)},
        "worst": {"ticker": worst["ticker"], "pnl_pct": round(worst["pnl_pct"], 2)},
    }


# ------------------------------------------------------------------ 코멘트
def build_notes(valuation, diag, views, skipped):
    """룰 기반 지적. LLM 없이 항상 나오고, 비용이 0이다.

    확정적 어조를 쓰지 않는다 — 예측력이 검증되지 않은 모델을 근거로 삼기 때문이다.
    """
    notes = []
    conc = diag.get("concentration")
    if conc:
        if conc["flags"]:
            notes.append(
                f"집중도가 높습니다: {' · '.join(conc['flags'])}. "
                f"{conc['n_positions']}종목을 갖고 있지만 유효 종목 수는 "
                f"{conc['effective_positions']}개 수준입니다.")
        elif conc["n_positions"] >= 3:
            notes.append(
                f"{conc['n_positions']}종목에 비교적 고르게 분산돼 있습니다 "
                f"(최대 비중 {conc['top_weight_pct']}%).")

    corr = diag.get("correlation")
    if corr:
        if corr["flags"]:
            top = corr["highest"][0]
            notes.append(
                f"보유 종목이 같이 움직입니다 (평균 상관 {corr['avg_correlation']}). "
                f"가장 높은 쌍은 {top['a']}–{top['b']} {top['corr']} 입니다. "
                "종목 수를 늘려도 위험이 줄지 않는 구간입니다.")
        elif corr["avg_correlation"] <= 0.3:
            notes.append(
                f"종목 간 상관이 낮아(평균 {corr['avg_correlation']}) 분산이 실제로 "
                "작동하고 있습니다.")

    vol = diag.get("volatility")
    if vol and vol["diversification_benefit_pct"] is not None:
        notes.append(
            f"포트폴리오 연변동성은 {vol['portfolio_vol_annual_pct']}% 입니다 "
            f"(종목별 가중평균 {vol['weighted_avg_vol_pct']}% 대비 "
            f"{vol['diversification_benefit_pct']}%p 낮음 = 분산 효과).")

    fx = diag.get("currency_exposure")
    if fx and len(fx) > 1:
        base = valuation["base_currency"]
        foreign = sum(v for c, v in fx.items() if c != base)
        if foreign >= 30:
            notes.append(
                f"해외통화 노출이 {foreign:.0f}% 입니다 "
                f"({' · '.join(f'{c} {v}%' for c, v in fx.items())}). "
                f"{base} 기준 평가액은 환율에도 함께 움직입니다.")

    pnl = diag.get("pnl")
    if pnl:
        if pnl["total_pnl_pct"] is not None:
            notes.append(
                f"평단이 입력된 {pnl['covered']}종목 기준 손익은 "
                f"{pnl['total_pnl_pct']:+.2f}% 입니다 "
                f"(수익 {pnl['winners']} · 손실 {pnl['losers']}, "
                f"최고 {pnl['best']['ticker']} {pnl['best']['pnl_pct']:+.1f}% / "
                f"최악 {pnl['worst']['ticker']} {pnl['worst']['pnl_pct']:+.1f}%).")
        if pnl["uncovered"]:
            notes.append(f"{pnl['uncovered']}종목은 평단이 없어 손익 계산에서 빠졌습니다.")

    if views:
        longs = [v for v in views if v["direction"] == "LONG"]
        avoids = [v for v in views if v["direction"] == "AVOID"]
        top = views[0]
        notes.append(
            f"모델 견해는 상승 {len(longs)} · 하락 {len(avoids)} 로 갈립니다. "
            f"주목도 1위는 {top['ticker']}({top['view_horizon_pct']:+.2f}%, "
            f"확신도 {top['confidence']}) 입니다. "
            "이 모델의 예측력은 검증되지 않았으므로 참고 이상으로 쓰지 마세요.")

        # 비중이 큰데 견해가 나쁜 종목은 따로 짚는다
        wmap = {r["ticker"]: r.get("weight") for r in valuation["rows"]}
        risky = [v for v in views
                 if v["direction"] == "AVOID" and (wmap.get(v["ticker"]) or 0) >= 0.20]
        if risky:
            names = ", ".join(f"{v['ticker']}({wmap[v['ticker']] * 100:.0f}%)" for v in risky)
            notes.append(f"비중이 큰데 모델 견해가 하락인 종목: {names}.")

    if valuation["unpriced"]:
        notes.append(f"시세를 받지 못해 평가에서 빠진 종목: {', '.join(valuation['unpriced'])}.")
    if valuation["unconverted"]:
        notes.append(f"환율을 받지 못해 합산에서 빠진 종목: {', '.join(valuation['unconverted'])}.")
    if skipped:
        notes.append("견해를 낼 수 없었던 종목: "
                     + ", ".join(f"{tk}({why})" for tk, why in skipped.items()) + ".")
    return notes


# ------------------------------------------------------------------ 진입점
def analyze(book, with_views=True, scorer=None, bars=None):
    """보유 장부 하나를 통째로 분석한다.

    book        : HoldingsBook
    with_views  : Kronos 견해까지 낼지 (종목당 1초 남짓 걸린다)
    scorer      : 견해 스코어러 주입 (테스트·캐시용)
    bars        : 미리 받아둔 봉 (없으면 직접 받는다)
    """
    tickers = book.tickers
    if not tickers:
        return {"empty": True, "notes": ["보유 종목이 없습니다. 먼저 종목을 추가하세요."]}

    prices = symbol_search.quote(tickers)
    fx = symbol_search.fx_rates({h["currency"] for h in book.to_list()})
    valuation = book.valuation(prices, fx)

    if bars is None:
        bars, _ = _fetch_bars(tickers)

    weights = {r["ticker"]: r["weight"] for r in valuation["rows"] if r.get("weight")}
    diag = {
        "concentration": concentration(weights),
        "correlation": correlation(bars, tickers),
        "volatility": portfolio_volatility(bars, weights),
        "currency_exposure": currency_exposure(valuation["rows"]),
        "pnl": pnl_summary(valuation["rows"]),
    }

    views, skipped = ([], {})
    if with_views:
        views, skipped = views_for(tickers, bars=bars, scorer=scorer)

    return {
        "empty": False,
        "as_of": valuation["as_of"],
        "valuation": valuation,
        "views": views,
        "skipped_views": skipped,
        "diagnostics": diag,
        "notes": build_notes(valuation, diag, views, skipped),
        "disclaimer": (
            "이 분석은 투자 조언이 아닙니다. 견해는 예측력이 검증되지 않은 모델의 "
            "출력이며, 어떤 매매도 자동으로 일어나지 않습니다."
        ),
    }
