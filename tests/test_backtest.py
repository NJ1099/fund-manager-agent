"""
백테스트 하네스 테스트.

여기서 지키려는 것은 두 가지다.

**① 미래를 보지 않는다.** 백테스트에서 가장 치명적인 버그는 조용한 룩어헤드다 —
결과가 그럴듯하게 좋아질 뿐 아무 에러도 나지 않으므로, 테스트가 없으면 영영
발견되지 않는다. 그래서 "스코어러에 들어간 봉의 마지막 날짜 ≤ 기준일"을
직접 기록해서 검증한다.

**② 라이브와 같은 로직을 돈다.** 하네스가 전략을 복제하기 시작하면 백테스트는
실제로 돌아가는 전략이 아닌 것을 측정하게 된다. `core/cycle.py::plan` 을
경유하는지를 고정한다.

Kronos 는 부르지 않는다 (모델 로딩 수십 초 + 샘플링이라 매번 다름).
`scorer` 주입 자리에 결정론적인 가짜를 넣는다 — 이 주입 훅은 백테스트의
추론 캐시가 쓰는 자리와 정확히 같다.
"""
import numpy as np
import pandas as pd
import pytest

from core import backtest, config, cycle as cycle_core, infer_cache

LOOKBACK_TEST = 60          # 테스트에서만 짧게 (기본 300봉은 합성에 시간이 걸린다)


# ------------------------------------------------------------------ 픽스처
def make_bars(tickers=("AAA", "BBB", "CCC", "SPY"), n=400, seed=7):
    """결정론적 합성 OHLCV. 종목마다 다른 드리프트를 줘서 선별이 의미를 갖게 한다."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    out = {}
    for i, tk in enumerate(tickers):
        drift = 0.0004 * (i + 1)
        rets = rng.normal(drift, 0.008, n)
        close = 100.0 * np.cumprod(1.0 + rets)
        out[tk] = pd.DataFrame({
            "open": close * 0.999,
            "high": close * 1.004,
            "low": close * 0.996,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        }, index=idx)
    return out


def fake_scorer(seen=None):
    """score_ticker 와 같은 모양을 돌려주는 결정론적 스코어러.

    seen 리스트를 주면 (종목, 받은 봉의 마지막 날짜, 기준일)을 기록한다 —
    룩어헤드 검증에 쓴다.
    """
    def scorer(ticker, bars, asof=None):
        if seen is not None:
            seen.append((ticker, bars.index[-1], asof))
        close = bars["close"]
        mom = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) > 21 else 0.0
        # 종목마다 고정된 분산 → 확신도 순위가 결정론적으로 정해진다
        disp = 0.01 + 0.005 * (sum(ord(c) for c in ticker) % 5)
        return {
            "ticker": ticker,
            "last_close": round(float(close.iloc[-1]), 2),
            "view_daily": mom / 100.0,
            "view_horizon_pct": round(mom * 100, 3),
            "dispersion": disp,
            "up_path_ratio": 0.6 if mom > 0 else 0.4,
            "fan": {"p05": [], "p50": [], "p95": []},
            "mom_20d_pct": round(mom * 100, 2),
            "mom_60d_pct": 0.0,
            "realized_vol_pct": 12.0,
            "infer_sec": 0.0,
            "asof": str(asof or bars.index[-1])[:10],
        }
    return scorer


@pytest.fixture
def short_lookback(monkeypatch):
    monkeypatch.setattr(config, "LOOKBACK", LOOKBACK_TEST)
    return LOOKBACK_TEST


# ------------------------------------------------------- 기준일 · 봉 자르기
def test_기준일은_룩백_구간을_건너뛴다(short_lookback):
    """앞쪽 LOOKBACK 봉은 모델 컨텍스트로 소모되므로 기준일이 될 수 없다."""
    bars = make_bars(n=200)
    dates = backtest.rebalance_dates(bars, step=5)
    common = backtest.common_dates(bars)

    # score_ticker 가 LOOKBACK+1 봉을 요구한다 (lb 봉 + 여유 1봉).
    # 따라서 첫 기준일은 common[LOOKBACK] 이지 common[LOOKBACK-1] 이 아니다.
    assert dates[0] == common[short_lookback]
    assert len(dates) == len(common[short_lookback:][::5])


def test_기준일_간격은_거래일_기준이다(short_lookback):
    bars = make_bars(n=200)
    common = backtest.common_dates(bars)
    dates = backtest.rebalance_dates(bars, step=7)

    positions = [common.get_loc(d) for d in dates]
    assert all(b - a == 7 for a, b in zip(positions, positions[1:]))


def test_데이터가_룩백보다_짧으면_기준일이_없다(short_lookback):
    bars = make_bars(n=LOOKBACK_TEST - 5)
    assert len(backtest.rebalance_dates(bars, step=5)) == 0


def test_공통_거래일은_교집합이다():
    """종목마다 상장일이 다르면 유니버스가 날짜별로 들쭉날쭉해진다."""
    bars = make_bars(tickers=("AAA", "BBB"), n=100)
    bars["BBB"] = bars["BBB"].iloc[20:]

    common = backtest.common_dates(bars)
    assert len(common) == 80
    assert common[0] == bars["BBB"].index[0]


def test_봉_자르기는_기준일_이후를_남기지_않는다(short_lookback):
    bars = make_bars(n=200)
    as_of = backtest.common_dates(bars)[120]

    sliced = backtest.slice_bars(bars, as_of)
    assert sliced
    for df in sliced.values():
        assert df.index[-1] == as_of


def test_봉이_모자란_종목은_아예_빠진다(short_lookback):
    bars = make_bars(tickers=("AAA", "BBB"), n=200)
    bars["BBB"] = bars["BBB"].iloc[150:]          # 최근 50봉만 존재

    sliced = backtest.slice_bars(bars, backtest.common_dates(bars)[-1])
    assert "AAA" in sliced and "BBB" not in sliced


# ----------------------------------------------------------- 룩어헤드 차단
def test_스코어러는_기준일_이후_데이터를_보지_못한다(short_lookback):
    """이 테스트를 지우지 말 것.

    룩어헤드는 에러 없이 성과만 좋아지므로, 이 검증이 없으면 조용히 통과한다.
    """
    bars = make_bars(n=200)
    seen = []
    result = backtest.run(bars, step=20, scorer=fake_scorer(seen),
                          watchlist=["AAA", "BBB", "CCC"], benchmark="SPY")

    assert seen, "스코어러가 한 번도 불리지 않았다"
    for ticker, last_bar, asof in seen:
        assert last_bar <= asof, f"{ticker}: {last_bar} 봉이 기준일 {asof} 을 넘었다"

    # 사이클 기준일도 데이터 마지막 날을 넘지 않는다
    last_date = backtest.common_dates(bars)[-1]
    assert all(pd.Timestamp(c["as_of"]) <= last_date for c in result["cycles"])


def test_사이클_기준일은_단조증가한다(short_lookback):
    bars = make_bars(n=200)
    result = backtest.run(bars, step=15, scorer=fake_scorer(),
                          watchlist=["AAA", "BBB", "CCC"], benchmark="SPY")
    dates = [pd.Timestamp(c["as_of"]) for c in result["cycles"]]
    assert dates == sorted(dates)


# ------------------------------------------------------------- 리플레이 전체
@pytest.fixture
def result(short_lookback):
    bars = make_bars(n=200)
    return backtest.run(bars, step=20, scorer=fake_scorer(),
                        watchlist=["AAA", "BBB", "CCC"], benchmark="SPY",
                        initial_equity=100_000.0)


def test_리플레이가_사이클을_실제로_돈다(result):
    assert result["settings"]["completed_cycles"] >= 3
    assert not result["errors"]
    assert len(result["history"]) == result["settings"]["completed_cycles"]


def test_장부_불변식은_백테스트에서도_지켜진다(result):
    """레버리지가 생기면 성과가 통째로 거짓이 된다.

    execution_desk 의 불변식이 리플레이 전 구간에서 유지되는지 본다.
    """
    for c in result["cycles"]:
        exposure = sum(c["weights"].values())
        assert exposure <= 1.0 + 1e-6, f"{c['as_of']}: 총 노출 {exposure:.3f}"
        assert c["cash_weight"] >= -1e-9


def test_성과는_대시보드와_같은_계산기를_쓴다(result):
    """백테스트 전용 지표 계산을 따로 만들면 두 숫자를 비교할 수 없게 된다."""
    from core import performance
    assert result["performance"] == performance.summarize(result["history"])


def test_자산곡선이_이력과_길이가_같다(result):
    assert len(result["equity_curve"]) == len(result["history"])
    assert result["equity_curve"][0]["strategy"] == pytest.approx(100.0, abs=1.0)


def test_거래비용은_누적되며_줄지_않는다(result):
    costs = [c["costs_paid"] for c in result["cycles"]]
    assert all(b >= a for a, b in zip(costs, costs[1:]))
    assert costs[-1] > 0, "거래가 있었는데 비용이 0이면 성과가 낙관 편향된다"


def test_기준일이_없으면_명확히_실패한다(short_lookback):
    """조용히 빈 결과를 돌려주면 '성과 0%'로 오독된다."""
    bars = make_bars(n=200)
    with pytest.raises(ValueError, match="기준일"):
        backtest.run(bars, start="2099-01-01", scorer=fake_scorer(),
                     watchlist=["AAA", "BBB", "CCC"])


def test_사이클_실패는_기록되고_전체를_죽이지_않는다(short_lookback, monkeypatch):
    bars = make_bars(n=200)
    calls = {"n": 0}
    real_plan = cycle_core.plan

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("일시적 최적화 실패")
        return real_plan(*a, **kw)

    monkeypatch.setattr(cycle_core, "plan", flaky)
    result = backtest.run(bars, step=20, scorer=fake_scorer(),
                          watchlist=["AAA", "BBB", "CCC"], benchmark="SPY")

    assert len(result["errors"]) == 1
    assert "일시적 최적화 실패" in result["errors"][0]["error"]
    assert result["settings"]["completed_cycles"] == result["settings"]["planned_cycles"] - 1


# ------------------------------------------------------------------ 선별 규칙
def test_양의_견해가_있으면_그중에서_고른다():
    scores = [
        {"ticker": "A", "view_horizon_pct": 2.0},
        {"ticker": "B", "view_horizon_pct": 1.0},
        {"ticker": "C", "view_horizon_pct": -3.0},
    ]
    picks, fallback = cycle_core.select_picks(scores, top_k=5)
    assert picks == ["A", "B"]
    assert not fallback


def test_전부_음의_견해면_확신도_상위로_최소분산을_유지한다():
    """현금 100%로 도망가는 것은 검증되지 않은 모델을 과신하는 것이다."""
    scores = [{"ticker": t, "view_horizon_pct": -1.0} for t in "ABCDE"]
    picks, fallback = cycle_core.select_picks(scores, top_k=4)
    assert fallback
    assert len(picks) >= 2


# -------------------------------------------------------------- 추론 캐시
def test_캐시는_같은_종목_같은_날짜를_두_번_추론하지_않는다(tmp_path):
    calls = []
    cache = infer_cache.InferenceCache(path=tmp_path / "c.jsonl")
    scorer = cache.wrap(fake_scorer(calls))

    bars = make_bars(tickers=("AAA",), n=100)["AAA"]
    a = scorer("AAA", bars, asof=bars.index[-1])
    b = scorer("AAA", bars, asof=bars.index[-1])
    cache.close()

    assert len(calls) == 1
    assert a == b
    assert cache.hits == 1 and cache.misses == 1


def test_캐시는_반환값_사본을_준다_원본이_오염되지_않게(tmp_path):
    """scan() 이 결과 dict 에 confidence·rank 를 써넣는다.

    사본을 안 주면 다음 사이클에서 이전 사이클의 순위가 딸려 나온다.
    """
    cache = infer_cache.InferenceCache(path=tmp_path / "c.jsonl")
    scorer = cache.wrap(fake_scorer())
    bars = make_bars(tickers=("AAA",), n=100)["AAA"]

    first = scorer("AAA", bars, asof=bars.index[-1])
    first["confidence"] = 0.9
    second = scorer("AAA", bars, asof=bars.index[-1])
    cache.close()

    assert "confidence" not in second


def test_추론_설정이_바뀌면_캐시가_무효화된다(tmp_path, monkeypatch):
    """이걸 빠뜨리면 '파라미터를 바꿨는데 결과가 같다'는 침묵 버그가 된다."""
    path = tmp_path / "c.jsonl"
    bars = make_bars(tickers=("AAA",), n=100)["AAA"]

    c1 = infer_cache.InferenceCache(path=path)
    c1.wrap(fake_scorer())("AAA", bars, asof=bars.index[-1])
    c1.close()

    monkeypatch.setattr(config, "SAMPLE_COUNT", config.SAMPLE_COUNT + 1)
    c2 = infer_cache.InferenceCache(path=path)
    assert c2.entries == {}, "추론 파라미터가 달라졌는데 옛 캐시를 재사용했다"


def test_캐시를_끄면_매번_새로_추론한다(tmp_path):
    calls = []
    cache = infer_cache.InferenceCache(path=tmp_path / "c.jsonl", enabled=False)
    scorer = cache.wrap(fake_scorer(calls))
    bars = make_bars(tickers=("AAA",), n=100)["AAA"]

    scorer("AAA", bars, asof=bars.index[-1])
    scorer("AAA", bars, asof=bars.index[-1])
    cache.close()

    assert len(calls) == 2
    assert not (tmp_path / "c.jsonl").exists()


def test_캐시는_재시작해도_파일에서_살아난다(tmp_path):
    calls = []
    bars = make_bars(tickers=("AAA",), n=100)["AAA"]

    c1 = infer_cache.InferenceCache(path=tmp_path / "c.jsonl")
    c1.wrap(fake_scorer(calls))("AAA", bars, asof=bars.index[-1])
    c1.close()

    c2 = infer_cache.InferenceCache(path=tmp_path / "c.jsonl")
    c2.wrap(fake_scorer(calls))("AAA", bars, asof=bars.index[-1])
    c2.close()

    assert len(calls) == 1, "재시작 후 캐시를 못 읽어 다시 추론했다"
    assert c2.hits == 1


def test_캐시가_적중하면_백테스트가_추론을_건너뛴다(short_lookback, tmp_path):
    """파라미터 스윕이 실제로 싸지는지 — 이 하네스의 존재 이유다."""
    bars = make_bars(n=200)
    calls = []

    c1 = infer_cache.InferenceCache(path=tmp_path / "c.jsonl")
    backtest.run(bars, step=20, scorer=c1.wrap(fake_scorer(calls)),
                 watchlist=["AAA", "BBB", "CCC"], benchmark="SPY")
    c1.close()
    first_round = len(calls)

    c2 = infer_cache.InferenceCache(path=tmp_path / "c.jsonl")
    backtest.run(bars, step=20, scorer=c2.wrap(fake_scorer(calls)),
                 watchlist=["AAA", "BBB", "CCC"], benchmark="SPY")
    c2.close()

    assert first_round > 0
    assert len(calls) == first_round, "두 번째 실행에서 추론이 다시 일어났다"
    assert c2.misses == 0
