"""
성과 데스크 테스트.

핵심 규칙: 계산할 수 없는 값은 0 이 아니라 None 이어야 한다.
샤프가 없는데 0 으로 표시하면 '위험 대비 수익이 없다'로 읽히지만,
실제로는 '아직 잴 수 없다'는 전혀 다른 상태다.
"""
import pytest

from core import performance


def rec(cycle, day, equity, ret, spy=None, turnover=0.0, costs=0.0):
    return {
        "cycle_no": cycle,
        "as_of": f"2026-01-{day:02d}",
        "equity": equity,
        "start_equity": 1000.0,
        "period_return_pct": ret,
        "benchmark_close": spy,
        "turnover": turnover,
        "costs_paid": costs,
    }


# ------------------------------------------------------------------ 빈 입력
def test_이력이_없으면_전부_None():
    out = performance.summarize([])
    assert out["n_cycles"] == 0
    assert out["cum_return_pct"] is None
    assert out["sharpe"] is None
    assert performance.equity_curve([]) == []


def test_사이클이_하나면_통계는_계산하지_않는다():
    out = performance.summarize([rec(1, 2, 1000.0, 0.0)])
    assert out["cum_return_pct"] == pytest.approx(0.0)
    assert out["sharpe"] is None
    assert out["win_rate_pct"] is None
    assert out["annualized"] is False


# ------------------------------------------------------------------ 수익률
def test_누적수익은_최초원금_대비로_잰다():
    recs = [rec(1, 2, 1000.0, 0.0), rec(2, 3, 1100.0, 10.0), rec(3, 6, 1210.0, 10.0)]
    out = performance.summarize(recs)
    assert out["cum_return_pct"] == pytest.approx(21.0)


def test_최대낙폭은_고점_대비로_잰다():
    recs = [rec(1, 2, 1000.0, 0.0), rec(2, 3, 1200.0, 20.0),
            rec(3, 6, 900.0, -25.0), rec(4, 7, 1000.0, 11.1)]
    out = performance.summarize(recs)
    # 고점 1200 → 저점 900 = -25%
    assert out["max_drawdown_pct"] == pytest.approx(25.0, abs=0.01)


def test_승률은_첫사이클을_제외하고_센다():
    """첫 사이클은 직전 자산이 없어 0% 로 기록되므로 통계에서 빼야 한다."""
    recs = [rec(1, 2, 1000.0, 0.0), rec(2, 3, 1100.0, 10.0),
            rec(3, 6, 1050.0, -4.5), rec(4, 7, 1150.0, 9.5)]
    out = performance.summarize(recs)
    assert out["win_rate_pct"] == pytest.approx(66.7, abs=0.1)   # 3회 중 2회
    assert out["best_cycle_pct"] == pytest.approx(10.0)
    assert out["worst_cycle_pct"] == pytest.approx(-4.5)


# ------------------------------------------------------------------ 벤치마크
def test_벤치마크_대비_초과수익을_계산한다():
    recs = [rec(1, 2, 1000.0, 0.0, spy=100.0),
            rec(2, 3, 1100.0, 10.0, spy=105.0),
            rec(3, 6, 1150.0, 4.5, spy=110.0)]
    out = performance.summarize(recs)
    assert out["cum_return_pct"] == pytest.approx(15.0)
    assert out["benchmark_cum_return_pct"] == pytest.approx(10.0)
    assert out["excess_return_pct"] == pytest.approx(5.0)


def test_벤치마크가_없으면_초과수익도_없다():
    recs = [rec(1, 2, 1000.0, 0.0), rec(2, 3, 1100.0, 10.0)]
    out = performance.summarize(recs)
    assert out["benchmark_cum_return_pct"] is None
    assert out["excess_return_pct"] is None


# ------------------------------------------------------------------ 연율화
def test_같은날_반복실행은_연율화하지_않는다():
    """테스트로 같은 날 여러 번 돌린 이력에 연율화 지표를 붙이면 거짓말이 된다."""
    recs = [rec(i, 2, 1000.0 + i, 0.1) for i in range(1, 6)]
    out = performance.summarize(recs)
    assert out["annualized"] is False
    assert out["sharpe"] is None
    assert out["volatility_annual_pct"] is None


def test_날짜가_벌어지면_연율화한다():
    recs = [rec(i, i, 1000.0 * (1.01 ** i), 1.0 if i % 2 else -0.5)
            for i in range(1, 9)]
    out = performance.summarize(recs)
    assert out["annualized"] is True
    assert out["sharpe"] is not None
    assert out["volatility_annual_pct"] > 0


# ------------------------------------------------------------------ 비용·차트
def test_누적비용은_마지막_값을_쓴다():
    recs = [rec(1, 2, 1000.0, 0.0, costs=10.0), rec(2, 3, 1000.0, 0.0, costs=25.0)]
    out = performance.summarize(recs)
    assert out["total_costs"] == pytest.approx(25.0)
    assert out["cost_drag_pct"] == pytest.approx(2.5)


def test_자산곡선은_둘다_100에서_시작한다():
    recs = [rec(1, 2, 1000.0, 0.0, spy=400.0), rec(2, 3, 1100.0, 10.0, spy=440.0)]
    curve = performance.equity_curve(recs)
    assert curve[0]["strategy"] == pytest.approx(100.0)
    assert curve[0]["benchmark"] == pytest.approx(100.0)
    assert curve[1]["strategy"] == pytest.approx(110.0)
    assert curve[1]["benchmark"] == pytest.approx(110.0)


def test_자산곡선은_최근_구간만_잘라낸다():
    recs = [rec(i, (i % 28) + 1, 1000.0 + i, 0.1) for i in range(1, 200)]
    curve = performance.equity_curve(recs, points=50)
    assert len(curve) == 50
