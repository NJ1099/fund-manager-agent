"""
리스크 게이트 · 회전율 게이트 테스트.

두 게이트는 '평소에는 아무것도 하지 않다가 한도에서만 작동'해야 한다.
평상시에 조용히 목표를 깎으면 성과가 이유 없이 나빠지고, 정작 한도에서
작동하지 않으면 존재 이유가 없다. 양쪽을 모두 고정한다.
"""
import pandas as pd
import pytest

from core import config
from core.portfolio_desk import apply_risk_gate, enforce_turnover


def w(**kwargs):
    return pd.Series(kwargs, dtype=float)


EMPTY = pd.Series(dtype=float)


# ------------------------------------------------------------ 회전율 게이트
def test_최초편입은_회전율_게이트에_걸리지_않는다():
    """현금에서 신규 편입하는 것은 교체가 아니다.

    양측 합으로 재던 시절에는 이 경우가 언제나 100%로 잡혀서, 아직 아무것도
    사지 않은 포트폴리오가 영원히 현금에 묶였다.
    """
    target = w(AAA=0.5, BBB=0.5)
    out, info = enforce_turnover(target, EMPTY)

    assert not info["scaled"]
    assert info["sell_turnover"] == pytest.approx(0.0)
    assert out.sum() == pytest.approx(1.0)


def test_한도_안의_조정은_그대로_통과한다():
    current = w(AAA=0.5, BBB=0.5)
    target = w(AAA=0.55, BBB=0.45)          # 매도측 5%
    out, info = enforce_turnover(target, current, limit=0.25)

    assert not info["scaled"]
    assert out["AAA"] == pytest.approx(0.55)


def test_한도를_넘는_교체는_비례_축소된다():
    current = w(AAA=1.0)
    target = w(BBB=1.0)                      # AAA 전량 매도 → 매도측 100%
    out, info = enforce_turnover(target, current, limit=0.25)

    assert info["scaled"]
    assert info["scale"] == pytest.approx(0.25)
    assert info["final_turnover"] == pytest.approx(0.25, abs=1e-6)
    assert out["AAA"] == pytest.approx(0.75)
    assert out["BBB"] == pytest.approx(0.25)


def test_축소해도_비중_합은_보존된다():
    current = w(AAA=0.6, BBB=0.4)
    target = w(CCC=1.0)
    out, _ = enforce_turnover(target, current, limit=0.2)
    assert out.sum() == pytest.approx(1.0)
    assert (out >= -1e-12).all()


def test_skip이면_게이트를_통과시킨다():
    """리스크 게이트가 노출을 줄이는 중이라면 회전율을 이유로 막아선 안 된다."""
    current = w(AAA=1.0)
    target = w(AAA=0.3)
    out, info = enforce_turnover(target, current, limit=0.05, skip=True)

    assert info["skipped"]
    assert not info["scaled"]
    assert out["AAA"] == pytest.approx(0.3)


# -------------------------------------------------------------- 리스크 게이트
def test_평상시에는_목표를_건드리지_않는다():
    target = w(AAA=0.6, BBB=0.4)
    out, info = apply_risk_gate(target, drawdown=0.03, last_cycle_return=0.004)

    assert not info["triggered"]
    assert info["exposure"] == 1.0
    pd.testing.assert_series_equal(out, target)


def test_낙폭_한도를_넘으면_노출을_줄인다():
    target = w(AAA=0.6, BBB=0.4)
    out, info = apply_risk_gate(target, drawdown=0.25, last_cycle_return=0.0)

    assert info["triggered"]
    assert "낙폭" in info["reason"]
    assert out.sum() == pytest.approx(config.DRAWDOWN_EXPOSURE)
    # 상대 구조(선호 순서)는 그대로여야 한다 — 크기만 줄인다
    assert out["AAA"] / out["BBB"] == pytest.approx(0.6 / 0.4)


def test_사이클_손실_한도를_넘으면_노출을_줄인다():
    target = w(AAA=1.0)
    out, info = apply_risk_gate(target, drawdown=0.0, last_cycle_return=-0.08)

    assert info["triggered"]
    assert "직전 사이클" in info["reason"]
    assert out.sum() == pytest.approx(config.DRAWDOWN_EXPOSURE)


def test_게이트를_끄면_아무것도_하지_않는다(monkeypatch):
    monkeypatch.setattr(config, "RISK_GATE_ENABLED", False)
    target = w(AAA=1.0)
    out, info = apply_risk_gate(target, drawdown=0.9, last_cycle_return=-0.5)

    assert not info["triggered"]
    pd.testing.assert_series_equal(out, target)


def test_직전수익이_없어도_안전하게_동작한다():
    """첫 사이클에는 직전 수익률이 없다 (None)."""
    target = w(AAA=1.0)
    out, info = apply_risk_gate(target, drawdown=None, last_cycle_return=None)
    assert not info["triggered"]
    pd.testing.assert_series_equal(out, target)
