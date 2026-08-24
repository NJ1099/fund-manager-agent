"""
사이클 코어 — '무엇을 살지' 결정하는 한 사이클의 알맹이.

라이브(`bot.py`)와 백테스트(`core/backtest.py`)가 **같은 함수**를 부른다.
이게 이 모듈이 존재하는 유일한 이유다.

백테스트 하네스를 만들 때 가장 흔한 실패는 하네스가 파이프라인 로직을 복제하는
것이다. 복제하는 순간 두 코드가 갈라지고, 백테스트는 '실제로 돌아가는 전략'이
아니라 '백테스트에만 있는 전략'을 측정하게 된다. 그 성과는 아무것도 보장하지
않는다. 그래서 선별·최적화·게이트를 여기 한 곳에 두고 양쪽이 공유한다.

이 모듈이 하지 않는 것:
  - 데이터 획득 (라이브는 yfinance, 백테스트는 잘라둔 과거 봉)
  - 상태 저장 (라이브는 state.json, 백테스트는 메모리)
  - PM 논평 (LLM 비용이 드는 경로라 백테스트에서는 부르지 않는다)
"""
import logging

from . import config, data_desk, portfolio_desk, research_desk

log = logging.getLogger("cycle")


def select_picks(scores, top_k=None):
    """스캔 결과에서 실제 편입 후보를 고른다.

    양(+)의 견해 중 주목도 상위 K개가 원칙이다. 전부 음의 견해인 국면에서는
    현금 100%로 도망가는 대신 확신도 상위로 최소한의 분산을 유지한다 —
    이 모델의 예측력이 검증되지 않았으므로, 견해가 나쁘다는 이유만으로
    시장에서 완전히 빠지는 것은 모델을 과신하는 것이다.
    """
    top_k = config.TOP_K if top_k is None else top_k
    candidates = [s for s in scores if s["view_horizon_pct"] > 0][:top_k]
    fallback = len(candidates) < 2
    if fallback:
        candidates = scores[:max(2, top_k // 2)]
        log.warning("양의 견해가 부족 — 확신도 상위로 대체 편입")
    return [s["ticker"] for s in candidates], fallback


def plan(bars, exec_desk, prev_equity, peak_equity, last_cycle_return,
         watchlist=None, scorer=None):
    """한 사이클의 결정 부분을 전부 수행하고 집행 직전 상태를 돌려준다.

    bars              : {ticker: OHLCV DataFrame} — 이미 as_of 시점까지 잘린 것
    exec_desk         : 현재 장부 (읽기만 한다. 여기서 주문을 내지 않는다)
    prev_equity       : 직전 사이클 마감 자산
    peak_equity       : 낙폭 기준 고점
    last_cycle_return : 직전 사이클 수익률 (비율)
    watchlist         : 스캔 대상 종목 (기본 config.WATCHLIST)
    scorer            : 종목 스코어러 주입 — 백테스트의 추론 캐시가 이 자리를 쓴다

    반환 dict: as_of · prices · scores · picks · target_weights · current_weights · meta
    """
    watchlist = watchlist or config.WATCHLIST

    as_of = max(df.index[-1] for df in bars.values())
    prices = data_desk.latest_prices(bars, list(bars.keys()))

    # 1) 연구 — 워치리스트 전체 스캔 (벤치마크 전용 종목은 스캔 대상이 아니다)
    scan_bars = {tk: df for tk, df in bars.items() if tk in watchlist}
    scores = research_desk.scan(scan_bars, asof=as_of, scorer=scorer)

    # 2) 선별
    picks, pick_fallback = select_picks(scores)

    # 3) 옵티마이저 유니버스 = 선별 ∪ 현재 보유.
    # 보유 종목을 빼면 그 종목을 청산하는 회전율이 제약 계산에서 통째로 빠져서,
    # 실제로는 한도를 훨씬 넘는 리밸런싱이 제약을 통과해버린다 (과거 회귀 버그).
    held = [tk for tk, sh in exec_desk.positions.items() if sh > 0]
    universe = list(dict.fromkeys(picks + [tk for tk in held if tk in scan_bars]))

    by_ticker = {s["ticker"]: s for s in scores}
    views = {tk: by_ticker[tk]["view_daily"] for tk in universe if tk in by_ticker}
    confs = {tk: by_ticker[tk]["confidence"] for tk in universe if tk in by_ticker}
    universe = [tk for tk in universe if tk in views]

    rets = data_desk.returns_frame(bars, universe, window=config.COV_WINDOW)
    current_w = exec_desk.weights(prices)
    prev_w = None
    if not current_w.empty:
        prev_w = current_w.reindex(universe).fillna(0.0).values

    target_w, meta = portfolio_desk.optimize(rets, views, confs, previous_weights=prev_w)
    meta["pick_fallback"] = pick_fallback

    # 4) 리스크 게이트 — 손실 한도에 걸리면 전체 노출을 줄인다
    drawdown = max(0.0, 1.0 - prev_equity / peak_equity) if peak_equity else 0.0
    target_w, risk_info = portfolio_desk.apply_risk_gate(
        target_w, drawdown, last_cycle_return)

    # 5) 회전율 게이트 — 전체 유니버스 기준으로 실제 회전율을 다시 잰다.
    # 리스크 게이트가 노출을 줄이는 중이면 건너뛴다 — 손실 한도에 따른 축소를
    # 회전율을 이유로 미뤄서는 안 되기 때문이다.
    target_w, turn_info = portfolio_desk.enforce_turnover(
        target_w, current_w, skip=risk_info["triggered"])
    meta["risk_gate"] = risk_info
    meta["turnover_gate"] = turn_info

    return {
        "as_of": as_of,
        "prices": prices,
        "scores": scores,
        "picks": picks,
        "universe": universe,
        "target_weights": target_w,
        "current_weights": current_w,
        "meta": meta,
    }


def history_record(cycle_no, as_of, exec_desk, equity, start_equity,
                   period_return_pct, n_orders, weights, top_pick,
                   benchmark_close, prices):
    """성과 계산이 먹는 이력 레코드. 라이브·백테스트가 같은 모양을 쓴다.

    `performance.summarize()` 가 이 키들을 읽으므로, 백테스트가 다른 모양을
    만들면 지표 계산이 조용히 달라진다. 그래서 생성자를 여기 하나로 둔다.
    """
    return {
        "cycle_no": cycle_no,
        "as_of": as_of.strftime("%Y-%m-%d") if hasattr(as_of, "strftime") else str(as_of)[:10],
        "equity": round(float(equity), 2),
        "start_equity": float(start_equity),
        "period_return_pct": round(float(period_return_pct) * 100, 3),
        "n_orders": int(n_orders),
        "weights": {k: round(float(v), 6) for k, v in weights.items()},
        "top_pick": top_pick,
        "benchmark_close": benchmark_close,
        "turnover": exec_desk.turnover_sell,      # 실질 교체량 기준
        "turnover_gross": exec_desk.turnover,
        "costs_paid": exec_desk.costs_paid,
        "cash_weight": exec_desk.cash_weight(prices),
    }
