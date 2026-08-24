"""
PM 데스크 테스트 — 특히 LLM 호출 정책.

여기서 지키려는 것은 두 가지다.
  1) 돈이 나가는 호출이 정책대로만 일어날 것 (기본은 최초 1회)
  2) LLM 을 부르지 않아도 논평은 항상 나올 것 (룰 기반 폴백은 무료·결정론적)

실제 API 는 절대 부르지 않는다 — `_llm_opinion` 을 가짜로 갈아끼운다.
"""
import pytest

from core import config, pm_desk


def make_cycle(orders=None, degraded=None, fallback=False,
               risk_triggered=False, turnover_scaled=False, ret=0.1):
    return {
        "as_of": "2026-01-02",
        "watchlist": [
            {"ticker": "AAA", "rank": 1, "confidence": 0.7, "view_horizon_pct": 1.2,
             "up_path_ratio": 0.8, "mom_20d_pct": 1.0, "realized_vol_pct": 12.0},
            {"ticker": "BBB", "rank": 2, "confidence": 0.3, "view_horizon_pct": -0.5,
             "up_path_ratio": 0.2, "mom_20d_pct": -1.0, "realized_vol_pct": 20.0},
        ],
        "target_weights": {"AAA": 0.6},
        "previous_weights": {},
        "orders": orders or [],
        "equity": 100_000.0,
        "period_return_pct": ret,
        "degraded": degraded or [],
        "portfolio_meta": {
            "fallback_used": fallback,
            "bl_shift_bp": {},
            "risk_gate": {"triggered": risk_triggered, "reason": "테스트",
                          "exposure": 0.5, "drawdown_pct": 25.0},
            "turnover_gate": {"scaled": turnover_scaled, "scale": 0.25,
                              "sell_turnover": 1.0, "limit": 0.25},
        },
    }


@pytest.fixture
def llm_on(monkeypatch):
    """API 키가 있고 호출은 가짜로 처리되는 상태."""
    monkeypatch.setattr(config, "PM_ENABLED", True)
    calls = []
    monkeypatch.setattr(pm_desk, "_llm_opinion",
                        lambda cycle: calls.append(1) or "LLM 논평입니다.")
    return calls


# ------------------------------------------------------------ 호출 정책
def test_키가_없으면_절대_호출하지_않는다(monkeypatch):
    monkeypatch.setattr(config, "PM_ENABLED", False)
    monkeypatch.setattr(config, "PM_LLM_MODE", "always")
    ok, reason = pm_desk.should_call_llm(make_cycle(), llm_calls=0)
    assert ok is False
    assert "키" in reason


def test_기본정책_once는_최초_1회만_호출한다(monkeypatch, llm_on):
    monkeypatch.setattr(config, "PM_LLM_MODE", "once")
    cycle = make_cycle()

    _, src1, used1 = pm_desk.opinion(cycle, llm_calls=0)
    assert used1 is True and src1.startswith("llm:")

    # 두 번째부터는 호출하지 않는다
    _, src2, used2 = pm_desk.opinion(cycle, llm_calls=1)
    assert used2 is False and src2.startswith("rules")
    assert len(llm_on) == 1


def test_never는_키가_있어도_호출하지_않는다(monkeypatch, llm_on):
    monkeypatch.setattr(config, "PM_LLM_MODE", "never")
    _, src, used = pm_desk.opinion(make_cycle(), llm_calls=0)
    assert used is False and src.startswith("rules")
    assert llm_on == []


def test_always는_매번_호출한다(monkeypatch, llm_on):
    monkeypatch.setattr(config, "PM_LLM_MODE", "always")
    for n in range(3):
        _, _, used = pm_desk.opinion(make_cycle(), llm_calls=n)
        assert used is True
    assert len(llm_on) == 3


def test_on_change는_변화가_없으면_호출하지_않는다(monkeypatch, llm_on):
    monkeypatch.setattr(config, "PM_LLM_MODE", "on_change")
    quiet = make_cycle()                       # 게이트도 손실도 없음
    _, src, used = pm_desk.opinion(quiet, llm_calls=1)
    assert used is False
    assert "변경 없음" in src
    assert llm_on == []


def test_on_change는_일상적_주문만으로는_호출하지_않는다(monkeypatch, llm_on):
    """거의 매 사이클 주문이 나오므로, 주문을 기준으로 삼으면 always 와 같아진다."""
    monkeypatch.setattr(config, "PM_LLM_MODE", "on_change")
    routine = make_cycle(orders=[{"ticker": "AAA", "side": "BUY", "notional": 1000}])
    _, _, used = pm_desk.opinion(routine, llm_calls=1)
    assert used is False
    assert llm_on == []


@pytest.mark.parametrize("kwargs", [
    {"degraded": ["BBB"]},
    {"fallback": True},
    {"risk_triggered": True},
    {"turnover_scaled": True},
    {"ret": -5.0},                             # 큰 손실
])
def test_on_change는_이상징후가_있으면_호출한다(monkeypatch, llm_on, kwargs):
    monkeypatch.setattr(config, "PM_LLM_MODE", "on_change")
    _, _, used = pm_desk.opinion(make_cycle(**kwargs), llm_calls=1)
    assert used is True
    assert len(llm_on) == 1


def test_변화판정은_조용한_사이클을_구분한다():
    assert pm_desk.material_change(make_cycle()) is False
    assert pm_desk.material_change(make_cycle(risk_triggered=True)) is True
    assert pm_desk.material_change(make_cycle(ret=-5.0)) is True
    assert pm_desk.material_change(make_cycle(ret=-0.5)) is False


# ------------------------------------------------------------ 폴백 · 안전성
def test_LLM이_실패해도_논평은_나온다(monkeypatch):
    monkeypatch.setattr(config, "PM_ENABLED", True)
    monkeypatch.setattr(config, "PM_LLM_MODE", "always")

    def boom(cycle):
        raise RuntimeError("네트워크 없음")
    monkeypatch.setattr(pm_desk, "_llm_opinion", boom)

    text, src, used = pm_desk.opinion(make_cycle(), llm_calls=0)
    assert used is False
    assert src.startswith("rules")
    assert len(text) > 20


def test_LLM이_빈응답이면_룰_기반으로_간다(monkeypatch):
    monkeypatch.setattr(config, "PM_ENABLED", True)
    monkeypatch.setattr(config, "PM_LLM_MODE", "always")
    monkeypatch.setattr(pm_desk, "_llm_opinion", lambda cycle: "")

    _, src, used = pm_desk.opinion(make_cycle(), llm_calls=0)
    assert used is False and src.startswith("rules")


def test_룰_기반_논평은_같은_입력에_같은_출력을_준다(monkeypatch):
    monkeypatch.setattr(config, "PM_ENABLED", False)
    a, _, _ = pm_desk.opinion(make_cycle(), llm_calls=0)
    b, _, _ = pm_desk.opinion(make_cycle(), llm_calls=0)
    assert a == b


def test_게이트가_걸리면_논평이_그것을_언급한다(monkeypatch):
    monkeypatch.setattr(config, "PM_ENABLED", False)
    text, _, _ = pm_desk.opinion(
        make_cycle(risk_triggered=True, turnover_scaled=True), llm_calls=0)
    assert "리스크 게이트" in text
    assert "갈아엎어야" in text


def test_저장된_state로_논평을_다시_만들_수_있다(monkeypatch):
    """대시보드의 'PM 논평 받기' 버튼이 쓰는 경로.

    사이클을 다시 돌리지 않으므로, 화면의 숫자와 논평이 같은 사이클을 가리켜야 한다.
    """
    monkeypatch.setattr(config, "PM_ENABLED", False)
    cycle = make_cycle(orders=[{"ticker": "AAA", "side": "BUY", "notional": 1000}])

    state = {
        "cycle_no": 7,
        "as_of": cycle["as_of"],
        "watchlist": cycle["watchlist"],
        "picks": ["AAA"],
        "orders": cycle["orders"],
        "portfolio": {
            "equity": 123_456.0,
            "period_return_pct": 1.5,
            "weights": {"AAA": 0.6},
            "target_weights": cycle["target_weights"],
            "previous_weights": {"AAA": 0.4},
            "cash_weight": 0.4,
            "meta": cycle["portfolio_meta"],
            "degraded": [],
        },
    }
    rebuilt = pm_desk.cycle_from_state(state)
    assert rebuilt["cycle_no"] == 7
    assert rebuilt["equity"] == 123_456.0
    assert rebuilt["previous_weights"] == {"AAA": 0.4}

    # 되살린 cycle 로 논평과 종목 코멘트가 실제로 만들어져야 한다
    text, _, _ = pm_desk.opinion(rebuilt, llm_calls=0)
    assert len(text) > 20
    assert pm_desk.watch_notes(rebuilt)

    # LLM 페이로드도 예외 없이 구성돼야 한다 (필드 누락이 여기서 드러난다)
    payload = pm_desk._build_payload(rebuilt)
    assert payload["as_of"] == cycle["as_of"]
    assert payload["equity"] == 123_456.0


def test_종목_코멘트는_실제_체결비중을_기준으로_쓴다():
    cycle = make_cycle()
    cycle["final_weights"] = {"AAA": 0.0, "BBB": 0.5}   # 목표와 다른 실제 결과
    notes = pm_desk.watch_notes(cycle)
    # AAA 는 목표 60% 였지만 실제로는 미편입이므로 '보유' 코멘트가 붙으면 안 된다
    assert "핵심 보유" not in notes["AAA"]
