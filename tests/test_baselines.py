"""
대조군 스코어러 테스트 — 네트워크·모델을 쓰지 않는다.

대조군의 값어치는 **파이프라인을 하나도 바꾸지 않는 것**에서 나온다. 견해만 갈아끼우고
선별·최적화·게이트는 그대로 통과해야, 나온 성과 차이를 견해 탓으로 돌릴 수 있다.
여기서 고정하는 것은 그 성질이다.
"""
import numpy as np
import pandas as pd
import pytest

from core import baselines, config, cycle, research_desk


def bars(n=400, start=100.0, seed=1):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(0.0004, 0.01, n))
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.004, "low": close * 0.996,
        "close": close, "volume": np.full(n, 1e6),
    }, index=idx)


def universe(tickers=("SPY", "QQQ", "IWM", "TLT", "GLD"), n=400):
    return {tk: bars(n, 100 + i * 10, seed=i + 1) for i, tk in enumerate(tickers)}


# ---------------------------------------------------------------- 모양 일치
def test_대조군_스코어는_Kronos_와_같은_키를_돌려준다():
    """대시보드·이력·성적표가 같은 키를 읽는다. 하나라도 빠지면 조용히 깨진다."""
    need = {"ticker", "last_close", "view_daily", "view_horizon_pct", "dispersion",
            "up_path_ratio", "fan", "mom_20d_pct", "mom_60d_pct", "realized_vol_pct",
            "infer_sec", "asof"}
    for name in ("flat", "random"):
        s = baselines.get(name)("SPY", bars(), asof=pd.Timestamp("2026-01-15"))
        assert need <= set(s), f"{name}: 누락 {need - set(s)}"
        assert set(s["fan"]) == {"p05", "p50", "p95"}
        assert len(s["fan"]["p50"]) == config.PRED_LEN


def test_대조군은_합성임을_표시한다():
    """표시하지 않으면 캐시나 성적표에 섞였을 때 구분할 방법이 없다."""
    for name in ("flat", "random"):
        assert baselines.get(name)("SPY", bars())["synthetic"] is True


def test_과거_사실_지표는_실제로_계산한다():
    """모멘텀·실현변동성은 예측이 아니라 사실이다. 견해만 갈아끼우는 것이 목적이므로
    이 값들까지 가짜로 만들면 화면과 이력이 거짓말을 하게 된다."""
    b = bars()
    s = baselines.get("flat")("SPY", b)
    close = b["close"]
    expected_mom20 = (close.iloc[-1] / close.iloc[-21] - 1) * 100
    assert s["mom_20d_pct"] == pytest.approx(expected_mom20, abs=0.01)
    assert s["realized_vol_pct"] > 0


# ---------------------------------------------------------------- 재현성
def test_난수_대조군은_재실행하면_같은_결과를_준다():
    """전역 난수를 쓰면 두 번 돌렸을 때 성과가 달라져서, 무엇 때문에 달라졌는지
    구분할 수 없게 된다."""
    a = baselines.get("random")("SPY", bars(), asof=pd.Timestamp("2026-01-15"))
    b = baselines.get("random")("SPY", bars(), asof=pd.Timestamp("2026-01-15"))
    assert a["view_horizon_pct"] == b["view_horizon_pct"]
    assert a["dispersion"] == b["dispersion"]


def test_시드를_바꾸면_다른_견해가_나온다():
    """난수 대조군을 여러 번 돌려 분포를 봐야 '난수보다 나은가'에 답할 수 있다."""
    a = baselines.get("random", salt=0)("SPY", bars(), asof=pd.Timestamp("2026-01-15"))
    b = baselines.get("random", salt=1)("SPY", bars(), asof=pd.Timestamp("2026-01-15"))
    assert a["view_horizon_pct"] != b["view_horizon_pct"]


def test_종목마다_날짜마다_다른_견해가_나온다():
    r = baselines.get("random")
    day = pd.Timestamp("2026-01-15")
    assert (r("SPY", bars(), asof=day)["view_horizon_pct"]
            != r("QQQ", bars(), asof=day)["view_horizon_pct"])
    assert (r("SPY", bars(), asof=day)["view_horizon_pct"]
            != r("SPY", bars(), asof=pd.Timestamp("2026-01-22"))["view_horizon_pct"])


# ------------------------------------------------------- 파이프라인 통과 성질
def test_무견해는_전_종목에_같은_확신도를_준다():
    """견해가 같으면 상대 정규화가 무의미해지고, 남는 것은 최적화기의 결정뿐이다."""
    scores = research_desk.scan(universe(), scorer=baselines.get("flat"))
    assert len({s["confidence"] for s in scores}) == 1
    assert len({s["view_horizon_pct"] for s in scores}) == 1


def test_무견해가_대체편입_경로로_새지_않는다():
    """견해를 0 으로 두면 `select_picks` 가 '양의 견해 부족'으로 판단해 다른 코드
    경로를 탄다. 그러면 대조군이 재려던 정상 경로를 재지 못한다."""
    scores = research_desk.scan(universe(), scorer=baselines.get("flat"))
    picks, fallback = cycle.select_picks(scores)
    assert fallback is False
    assert len(picks) == min(config.TOP_K, len(scores))


def test_난수_견해도_정상_선별_경로를_탄다():
    scores = research_desk.scan(universe(), scorer=baselines.get("random"))
    picks, fallback = cycle.select_picks(scores)
    assert len(picks) >= 2


def test_알_수_없는_대조군은_조용히_넘어가지_않고_실패한다():
    """오타를 무시하면 'kronos 로 돌았는데 대조군인 줄 아는' 최악의 결과가 된다."""
    with pytest.raises(ValueError, match="알 수 없는 대조군"):
        baselines.get("kronoss")


def test_kronos_는_None_을_돌려준다():
    """기본 경로 — 캐시로 감싼 진짜 스코어러가 쓰인다."""
    assert baselines.get("kronos") is None
    assert baselines.get(None) is None
