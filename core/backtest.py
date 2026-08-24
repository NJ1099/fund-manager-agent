"""
백테스트 하네스 — 과거 데이터를 날짜별로 리플레이해 파이프라인 전체를 돌린다.

이게 없으면 성과를 알기 위해 하루 한 사이클씩 몇 달을 기다려야 한다.
`TOP_K`·`MAX_TURNOVER`·`SAMPLE_COUNT` 같은 값이 전부 검증 없이 기본값으로
박혀 있는 상태에서 라이브 성과만 보고 있으면, 나쁜 파라미터를 몇 달 뒤에나
알게 된다.

■ 이 하네스가 지키는 두 가지 규율

**① 전략 로직을 복제하지 않는다.**
선별·최적화·게이트는 `core/cycle.py::plan` 을 부른다. 라이브(`bot.py`)가 부르는
바로 그 함수다. 하네스가 로직을 복제하는 순간 두 코드가 갈라지고, 백테스트는
'실제로 돌아가는 전략'이 아니라 '백테스트에만 있는 전략'을 측정하게 된다.
그런 성과 숫자는 아무것도 보장하지 않는다.

**② 미래를 보지 않는다.**
매 시점 `slice_bars` 로 그 날짜까지만 잘라서 넘긴다. 이게 유일한 방어선이므로
`tests/test_backtest.py` 가 "스코어러에 들어온 봉의 마지막 날짜 ≤ 기준일"을
고정하고 있다. 이 테스트를 지우지 말 것.

■ 알면서 감수하는 한계 (성과를 읽을 때 반드시 같이 읽을 것)

- **생존 편향** — 워치리스트가 '지금 존재하는' ETF 10종이다. 과거 시점에 이 10종을
  고를 수 있었다는 보장은 없다. 대형 ETF라 상장폐지 편향은 작지만 0은 아니다.
- **슬리피지 없음** — 집행이 종가에 전량 체결된다고 본다. 실제보다 유리하다.
  회전율이 높을수록 이 낙관 편향이 커진다.
- **수정주가** — yfinance `auto_adjust=True` 기준이라 과거 가격이 배당·분할로
  소급 조정돼 있다. 당시 실제 호가와는 다르다.
- **추론 난수** — Kronos 는 샘플링이라 캐시 없이 다시 돌리면 결과가 달라진다.
  같은 설정의 재현이 필요하면 캐시를 켠 채로 돌릴 것.
"""
import logging
import time

import pandas as pd

from . import config, cycle as cycle_core, execution_desk, performance

log = logging.getLogger("backtest")


def common_dates(bars, tickers=None):
    """지정 종목이 **모두** 봉을 가진 거래일 인덱스.

    종목마다 상장일이 다르면 앞부분에서 유니버스가 들쭉날쭉해진다. 교집합으로
    잡아야 "어떤 날은 3종목, 어떤 날은 10종목"인 백테스트가 되지 않는다.
    """
    tickers = [tk for tk in (tickers or bars.keys()) if tk in bars]
    if not tickers:
        return pd.DatetimeIndex([])
    idx = bars[tickers[0]].index
    for tk in tickers[1:]:
        idx = idx.intersection(bars[tk].index)
    return idx.sort_values()


def rebalance_dates(bars, start=None, end=None, step=5, tickers=None, min_bars=None):
    """리밸런싱 기준일 목록.

    step 은 **거래일** 단위다 (5 = 주 1회). 앞쪽 `LOOKBACK` 봉은 Kronos 컨텍스트로
    소모되므로 기준일이 될 수 없다 — 그만큼 잘라내고 시작한다.
    """
    min_bars = (config.LOOKBACK + 1) if min_bars is None else min_bars
    idx = common_dates(bars, tickers)
    if len(idx) < min_bars:
        return pd.DatetimeIndex([])

    usable = idx[min_bars - 1:]
    if start is not None:
        usable = usable[usable >= pd.Timestamp(start)]
    if end is not None:
        usable = usable[usable <= pd.Timestamp(end)]
    return usable[::max(1, int(step))]


def slice_bars(bars, as_of, min_bars=None):
    """기준일까지만 남긴 봉. 미래를 보지 않게 하는 유일한 지점이다.

    데이터가 모자란 종목은 아예 제외한다 — 넘겨봐야 `score_ticker` 가
    '데이터 부족'으로 떨어뜨리고, 그 경고가 매 사이클 로그를 채운다.
    """
    min_bars = (config.LOOKBACK + 1) if min_bars is None else min_bars
    ts = pd.Timestamp(as_of)
    out = {}
    for tk, df in bars.items():
        cut = df.loc[:ts]
        if len(cut) >= min_bars:
            out[tk] = cut
    return out


def run(bars, start=None, end=None, step=5, scorer=None, initial_equity=None,
        watchlist=None, benchmark=None, on_cycle=None, stop_on_error=False):
    """날짜별 리플레이. 라이브와 같은 이력 레코드를 쌓고 같은 계산기로 요약한다.

    bars           : {ticker: OHLCV DataFrame} — 워치리스트 + 벤치마크 전체 기간
    start / end    : 기준일 범위 (None 이면 데이터가 허용하는 전 구간)
    step           : 리밸런싱 간격, 거래일 (5 = 주 1회)
    scorer         : 종목 스코어러 (캐시로 감싼 것을 주면 재실행이 거의 공짜)
    on_cycle       : 진행 콜백 fn(i, total, record)
    stop_on_error  : True 면 첫 사이클 실패에서 중단. 기본은 기록하고 계속한다.

    반환 dict: settings · cycles · history · performance · equity_curve · errors
    """
    watchlist = watchlist or config.WATCHLIST
    benchmark = benchmark or config.BENCHMARK
    equity0 = float(initial_equity if initial_equity is not None else config.INITIAL_EQUITY)

    dates = rebalance_dates(bars, start, end, step,
                            tickers=[tk for tk in watchlist if tk in bars])
    if len(dates) == 0:
        raise ValueError(
            "리밸런싱 기준일이 하나도 없습니다. 데이터 기간이 LOOKBACK"
            f"({config.LOOKBACK}일)보다 짧거나 --start/--end 범위가 비어 있습니다."
        )

    desk = execution_desk.ExecutionDesk(equity=equity0)
    prev_equity = peak_equity = equity0
    last_cycle_return = None

    history, cycles, errors = [], [], []
    last_prices = {}
    t0 = time.time()

    for i, dt in enumerate(dates, start=1):
        sliced = slice_bars(bars, dt)
        scan_ready = [tk for tk in watchlist if tk in sliced]
        if len(scan_ready) < 2:
            errors.append({"as_of": str(dt)[:10], "error": "스캔 가능 종목 2개 미만"})
            continue

        try:
            plan = cycle_core.plan(sliced, desk, prev_equity, peak_equity,
                                   last_cycle_return, watchlist=watchlist,
                                   scorer=scorer)
        except Exception as e:                     # 한 시점 실패로 전체를 버리지 않는다
            log.warning("%s 사이클 실패: %s", str(dt)[:10], e)
            errors.append({"as_of": str(dt)[:10], "error": str(e)[:300]})
            if stop_on_error:
                raise
            continue

        prices = plan["prices"]
        last_prices = prices

        # 거래 전 자산 = 새 가격이 반영된 직후. 사이클 수익률은 여기서 잰다.
        # 거래 후로 재면 리밸런싱 비용이 시장 수익률에 섞여 들어간다.
        equity_open = desk.equity(prices)
        period_ret = (equity_open / prev_equity - 1.0) if prev_equity else 0.0

        orders, final_w = desk.rebalance(plan["target_weights"], prices, plan["as_of"])
        equity_close = desk.equity(prices)
        peak_equity = max(peak_equity, equity_close)
        last_cycle_return = period_ret
        prev_equity = equity_close

        record = cycle_core.history_record(
            cycle_no=len(history) + 1,
            as_of=plan["as_of"],
            exec_desk=desk,
            equity=equity_close,
            start_equity=equity0,
            period_return_pct=period_ret,
            n_orders=len(orders),
            weights=final_w,
            top_pick=plan["scores"][0]["ticker"],
            benchmark_close=prices.get(benchmark),
            prices=prices,
        )
        history.append(record)

        gates = plan["meta"]
        cycles.append({
            **record,
            "picks": plan["picks"],
            "target_weights": {k: round(float(v), 6)
                               for k, v in plan["target_weights"].items() if float(v) > 0},
            "risk_gate": gates.get("risk_gate", {}).get("triggered", False),
            "turnover_scaled": gates.get("turnover_gate", {}).get("scaled", False),
            "optimizer_fallback": gates.get("fallback_used", False),
            "pick_fallback": gates.get("pick_fallback", False),
            "degraded": list(desk.degraded),
        })

        if on_cycle:
            on_cycle(i, len(dates), record)

    if not history:
        raise RuntimeError(f"완료된 사이클이 없습니다 (실패 {len(errors)}건). "
                           "첫 오류: " + (errors[0]["error"] if errors else "n/a"))

    return {
        "settings": {
            "start": str(dates[0])[:10],
            "end": str(dates[-1])[:10],
            "step_trading_days": step,
            "planned_cycles": len(dates),
            "completed_cycles": len(history),
            "initial_equity": equity0,
            "watchlist": list(watchlist),
            "benchmark": benchmark,
            "top_k": config.TOP_K,
            "max_turnover": config.MAX_TURNOVER,
            "turnover_hard_limit": config.TURNOVER_HARD_LIMIT,
            "tx_cost_bps": config.TX_COST_BPS * 10000,
            "max_weight": config.MAX_WEIGHT,
            "kronos_model": config.KRONOS_MODEL,
            "sample_count": config.SAMPLE_COUNT,
            "pred_len": config.PRED_LEN,
            "lookback": config.LOOKBACK,
            "elapsed_sec": round(time.time() - t0, 1),
        },
        "cycles": cycles,
        "history": history,
        # 백테스트 종료 시점의 장부. 이걸로 라이브 상태를 이어받을 수 있다
        # (scripts/build_demo.py 가 데모 데이터를 만들 때 쓴다).
        "final_book": {
            **desk.snapshot(last_prices),
            "seq": desk.seq,
            "equity": round(prev_equity, 2),
            "peak_equity": round(peak_equity, 2),
            "last_prices": dict(desk.last_prices),
        },
        # 대시보드와 **같은 계산기**를 쓴다. 백테스트 전용 지표 계산을 만들지 말 것 —
        # 두 숫자가 다른 정의를 갖는 순간 비교가 불가능해진다.
        "performance": performance.summarize(history),
        "equity_curve": performance.equity_curve(history, points=len(history)),
        "errors": errors,
    }


def format_report(result):
    """콘솔용 요약 텍스트."""
    s, p = result["settings"], result["performance"]

    def num(v, nd=2, sign=False, suffix="%"):
        """None 은 0 이 아니라 '—' 로 찍는다 — 없는 값을 0 으로 보이면
        '성과가 없다'로 오독된다 (performance.py 와 같은 원칙)."""
        if v is None:
            return "—"
        return f"{v:+.{nd}f}{suffix}" if sign else f"{v:.{nd}f}{suffix}"

    L = [
        f"기간          {s['start']} ~ {s['end']}  (거래일 {s['step_trading_days']}일 간격)",
        f"사이클        {s['completed_cycles']}/{s['planned_cycles']} 완료"
        + (f"  ·  실패 {len(result['errors'])}건" if result["errors"] else ""),
        f"유니버스      {len(s['watchlist'])}종목 · 상위 {s['top_k']} 편입 · "
        f"단일상한 {s['max_weight'] * 100:.0f}%",
        f"모델          {s['kronos_model']} · 경로 {s['sample_count']} · "
        f"예측 {s['pred_len']}일 · 룩백 {s['lookback']}일",
        f"소요          {s['elapsed_sec']}초",
        "",
        f"누적수익      {num(p['cum_return_pct'], sign=True)}",
        f"벤치마크      {num(p['benchmark_cum_return_pct'], sign=True)}"
        f"   ({s['benchmark']} 매수 후 보유)",
        f"초과수익      {num(p['excess_return_pct'], sign=True)}",
        f"최대낙폭      {num(p['max_drawdown_pct'])}"
        f"   (벤치마크 {num(p['benchmark_max_drawdown_pct'])})",
        f"연변동성      {num(p['volatility_annual_pct'])}",
        f"샤프          {num(p['sharpe'], suffix='')}"
        + ("" if p["annualized"] else "   (연율화 불가 — 사이클 간격이 불규칙)"),
        f"승률          {num(p['win_rate_pct'], nd=1)}"
        f"   최고 {num(p['best_cycle_pct'], sign=True)} / 최악 {num(p['worst_cycle_pct'], sign=True)}",
        f"평균 회전율   {num(p['avg_turnover_pct'])}"
        f"   누적비용 ${p['total_costs'] or 0:,.0f} (원금의 {p['cost_drag_pct'] or 0:.2f}%)",
        "",
    ]

    gates = sum(1 for c in result["cycles"] if c["risk_gate"])
    scaled = sum(1 for c in result["cycles"] if c["turnover_scaled"])
    fb = sum(1 for c in result["cycles"] if c["optimizer_fallback"])
    L.append(f"게이트        리스크 발동 {gates}회 · 회전율 축소 {scaled}회 · 최적화 후퇴 {fb}회")

    if result["errors"]:
        L.append("")
        L.append("실패한 사이클 (조용히 넘기지 않는다):")
        for e in result["errors"][:5]:
            L.append(f"  {e['as_of']}  {e['error'][:110]}")
        if len(result["errors"]) > 5:
            L.append(f"  … 외 {len(result['errors']) - 5}건")
    return "\n".join(L)
