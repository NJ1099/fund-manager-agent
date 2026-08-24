"""
성과 데스크 — 누적수익·변동성·샤프·최대낙폭·벤치마크 대비.

원문 교훈의 연장선: 예측이 맞았는지가 아니라 '위험 대비 무엇을 벌었는지'로
평가해야 한다. 그리고 벤치마크(SPY 매수 후 보유)를 이기지 못하면
이 전체 파이프라인은 존재 이유가 없다 — 그래서 항상 나란히 표시한다.

입력은 bot.py 가 쌓는 history 레코드 리스트다. 사이클이 2회 미만이면
대부분의 지표가 정의되지 않으므로 None 을 돌려준다 (0 으로 채우지 않는다 —
없는 값을 0 으로 보여주면 성과가 있는 것처럼 오해된다).
"""
import math
from datetime import date, datetime

RISK_FREE_ANNUAL = 0.0      # 단순화: 초과수익 기준선 0%


def _to_date(s):
    if isinstance(s, (date, datetime)):
        return s if isinstance(s, date) and not isinstance(s, datetime) else s.date()
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _periods_per_year(records):
    """사이클 간격에서 연율화 계수를 추정한다.

    같은 날짜에 여러 사이클이 찍힌 테스트 실행이면 연율화가 의미 없으므로
    None 을 반환하고, 호출부는 연율화 지표를 생략한다.
    """
    days = [d for d in (_to_date(r.get("as_of")) for r in records) if d]
    if len(days) < 3:
        return None
    spans = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0]
    if len(spans) < 2:
        return None
    spans.sort()
    median = spans[len(spans) // 2]
    return 365.25 / median if median > 0 else None


def _max_drawdown(series):
    """최대 낙폭 (양수 비율). 고점 대비 최대 하락."""
    peak, mdd = series[0], 0.0
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _stdev(xs):
    if len(xs) < 2:
        return None
    mu = sum(xs) / len(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def summarize(records):
    """history 레코드 리스트에서 성과 요약 dict 를 만든다.

    반환 키의 값이 None 이면 '아직 계산할 수 없음'이라는 뜻이다.
    """
    out = {
        "n_cycles": len(records),
        "cum_return_pct": None,
        "benchmark_cum_return_pct": None,
        "excess_return_pct": None,
        "max_drawdown_pct": None,
        "benchmark_max_drawdown_pct": None,
        "volatility_annual_pct": None,
        "sharpe": None,
        "win_rate_pct": None,
        "best_cycle_pct": None,
        "worst_cycle_pct": None,
        "avg_turnover_pct": None,
        "total_costs": None,
        "cost_drag_pct": None,
        "annualized": False,
    }
    if not records:
        return out

    equity = [float(r["equity"]) for r in records if r.get("equity") is not None]
    if len(equity) < 1:
        return out

    start_equity = float(records[0].get("start_equity") or equity[0])
    out["cum_return_pct"] = round((equity[-1] / start_equity - 1.0) * 100, 3)
    out["max_drawdown_pct"] = round(_max_drawdown([start_equity] + equity) * 100, 3)

    # 사이클 수익률 — 기록된 값을 그대로 쓴다 (equity 차분과 중복 계산하지 않는다)
    rets = [float(r["period_return_pct"]) / 100.0
            for r in records if r.get("period_return_pct") is not None]
    # 첫 사이클은 직전 자산이 없어 0 으로 기록되므로 통계에서 뺀다
    rets_eff = rets[1:] if len(rets) > 1 else []

    if rets_eff:
        out["win_rate_pct"] = round(sum(1 for r in rets_eff if r > 0) / len(rets_eff) * 100, 1)
        out["best_cycle_pct"] = round(max(rets_eff) * 100, 3)
        out["worst_cycle_pct"] = round(min(rets_eff) * 100, 3)

    ppy = _periods_per_year(records)
    sd = _stdev(rets_eff)
    if ppy and sd and sd > 0:
        mean = sum(rets_eff) / len(rets_eff)
        out["volatility_annual_pct"] = round(sd * math.sqrt(ppy) * 100, 2)
        excess = mean - RISK_FREE_ANNUAL / ppy
        out["sharpe"] = round(excess / sd * math.sqrt(ppy), 2)
        out["annualized"] = True

    # 벤치마크 — SPY 매수 후 보유
    spy = [float(r["benchmark_close"]) for r in records if r.get("benchmark_close")]
    if len(spy) >= 2:
        out["benchmark_cum_return_pct"] = round((spy[-1] / spy[0] - 1.0) * 100, 3)
        out["benchmark_max_drawdown_pct"] = round(_max_drawdown(spy) * 100, 3)
        if out["cum_return_pct"] is not None:
            out["excess_return_pct"] = round(
                out["cum_return_pct"] - out["benchmark_cum_return_pct"], 3)

    turns = [float(r["turnover"]) for r in records if r.get("turnover") is not None]
    if turns:
        out["avg_turnover_pct"] = round(sum(turns) / len(turns) * 100, 2)

    costs = [float(r["costs_paid"]) for r in records if r.get("costs_paid") is not None]
    if costs:
        out["total_costs"] = round(costs[-1], 2)      # 누적값이므로 마지막 것
        if start_equity > 0:
            out["cost_drag_pct"] = round(costs[-1] / start_equity * 100, 3)

    return out


def equity_curve(records, points=120):
    """대시보드 차트용 (기준일, 전략지수, 벤치마크지수) 시계열.

    둘 다 시작값 100 으로 정규화해서 같은 축에 겹쳐 그릴 수 있게 한다.
    """
    rows = [r for r in records if r.get("equity") is not None]
    if not rows:
        return []
    rows = rows[-points:]
    base_eq = float(rows[0].get("start_equity") or rows[0]["equity"])
    spy0 = next((float(r["benchmark_close"]) for r in rows if r.get("benchmark_close")), None)

    out = []
    for r in rows:
        item = {
            "as_of": r.get("as_of"),
            "cycle_no": r.get("cycle_no"),
            "strategy": round(float(r["equity"]) / base_eq * 100, 3) if base_eq else None,
            "benchmark": None,
        }
        if spy0 and r.get("benchmark_close"):
            item["benchmark"] = round(float(r["benchmark_close"]) / spy0 * 100, 3)
        out.append(item)
    return out
