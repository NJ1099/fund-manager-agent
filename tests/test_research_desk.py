"""
연구 데스크 중 모델이 필요 없는 순수 함수 테스트.

Kronos 추론 자체는 여기서 다루지 않는다(모델 로딩에 수십 초가 걸리고,
샘플링이라 결과가 매번 다르다). 대신 '경로 분산을 확신도로 바꾸는' 규칙과
모멘텀·변동성 계산처럼 결정론적인 부분만 고정한다.
"""
import numpy as np
import pandas as pd
import pytest

from core import research_desk as rd


def score(ticker, dispersion, view=1.0):
    return {"ticker": ticker, "dispersion": dispersion, "view_horizon_pct": view}


# ------------------------------------------------------- 분산 → 확신도
def test_분산이_작을수록_확신도가_높다():
    scores = [score("A", 0.01), score("B", 0.05), score("C", 0.10)]
    rd.dispersion_to_confidence(scores)

    conf = {s["ticker"]: s["confidence"] for s in scores}
    assert conf["A"] > conf["B"] > conf["C"]


def test_확신도는_정해진_범위를_벗어나지_않는다():
    scores = [score("A", 0.0), score("B", 1e9)]
    rd.dispersion_to_confidence(scores)
    for s in scores:
        assert 0.10 <= s["confidence"] <= 0.90


def test_모든_분산이_같으면_중립값을_준다():
    """상대 비교가 불가능한 상황에서 누군가에게 높은 확신도를 주면 안 된다."""
    scores = [score(t, 0.03) for t in "ABC"]
    rd.dispersion_to_confidence(scores)
    assert all(s["confidence"] == pytest.approx(0.5) for s in scores)


def test_종목이_하나뿐이어도_죽지_않는다():
    scores = [score("A", 0.02)]
    rd.dispersion_to_confidence(scores)
    assert 0.10 <= scores[0]["confidence"] <= 0.90


# ------------------------------------------------------- 모멘텀 · 변동성
def test_모멘텀은_기간수익률이다():
    s = pd.Series([100.0] * 10 + [110.0])
    assert rd._momentum(s, 10) == pytest.approx(0.10)


def test_구간보다_짧은_시계열은_0을_돌려준다():
    s = pd.Series([100.0, 101.0])
    assert rd._momentum(s, 20) == 0.0


def test_실현변동성은_연율화된_표준편차다():
    rng = np.random.default_rng(42)
    daily = rng.normal(0, 0.01, 300)
    close = pd.Series(100 * np.cumprod(1 + daily))

    vol = rd._realized_vol(close)
    # 일간 1% 변동 → 연율 약 16% 부근
    assert 0.08 < vol < 0.30


def test_표본이_부족하면_변동성은_0이다():
    assert rd._realized_vol(pd.Series([100.0, 101.0, 102.0])) == 0.0
