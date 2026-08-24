"""
파라미터 오버라이드 · 스윕 테스트.

여기서 지키는 것은 주로 **안전장치**다. 백테스트 편의를 위해 config 를 임의로
덮어쓸 수 있게 하면 `EXECUTION_MODE` 까지 바꿀 수 있게 되는데, 그건 이 프로젝트가
가진 유일한 실거래 방어선이다. 화이트리스트가 실제로 막는지 고정한다.

그리고 오타를 조용히 무시하지 않는지도 본다 — `--set TOPK=3` 이 무시되면
"바꿨는데 결과가 같다"가 되고, 원인을 찾는 데 한참 걸린다.
"""
import pytest

from core import config, sweep


# ------------------------------------------------------------------ 안전장치
@pytest.mark.parametrize("key", sorted(sweep.FORBIDDEN))
def test_안전장치_설정은_바꿀_수_없다(key):
    """EXECUTION_MODE 를 백테스트에서 바꿀 수 있으면 실거래 방어선이 뚫린다."""
    with pytest.raises(ValueError, match="바꿀 수 없습니다"):
        sweep.parse_assignment(f"{key}=paper")


def test_모르는_설정은_조용히_무시되지_않고_실패한다():
    with pytest.raises(ValueError, match="조정 가능한 설정이 아닙니다"):
        sweep.parse_assignment("TOPK=3")          # 오타


def test_형식이_틀리면_실패한다():
    with pytest.raises(ValueError, match="KEY=VALUE"):
        sweep.parse_assignment("TOP_K")


# ------------------------------------------------------------------ 파싱
def test_값은_설정의_타입으로_변환된다():
    assert sweep.parse_assignment("TOP_K=3") == ("TOP_K", 3)
    assert sweep.parse_assignment("MAX_WEIGHT=0.3") == ("MAX_WEIGHT", 0.3)


def test_소문자_키도_받는다():
    assert sweep.parse_assignment("top_k=4") == ("TOP_K", 4)


def test_참거짓_설정은_문자열로도_쓸_수_있다():
    assert sweep.parse_assignment("RISK_GATE_ENABLED=false") == ("RISK_GATE_ENABLED", False)
    assert sweep.parse_assignment("RISK_GATE_ENABLED=on") == ("RISK_GATE_ENABLED", True)


def test_참거짓이_아닌_값은_거부한다():
    with pytest.raises(ValueError, match="참/거짓"):
        sweep.parse_assignment("RISK_GATE_ENABLED=maybe")


def test_스윕은_값_목록을_파싱한다():
    assert sweep.parse_sweep("TOP_K=3,5,7") == ("TOP_K", [3, 5, 7])


def test_스윕에도_화이트리스트가_적용된다():
    with pytest.raises(ValueError, match="바꿀 수 없습니다"):
        sweep.parse_sweep("EXECUTION_MODE=live,paper")


def test_스윕_값이_비면_실패한다():
    with pytest.raises(ValueError, match="스윕할 값이 없습니다"):
        sweep.parse_sweep("TOP_K=")


# ------------------------------------------------------------------ 적용·복원
def test_적용하고_되돌리면_원래대로_돌아온다():
    """스윕이 값마다 restore 하지 않으면 다음 조합이 오염된 설정으로 돈다."""
    original = config.TOP_K
    previous = sweep.apply({"TOP_K": original + 3})
    assert config.TOP_K == original + 3

    sweep.restore(previous)
    assert config.TOP_K == original


def test_여러_설정을_한꺼번에_적용한다():
    before = (config.TOP_K, config.MAX_WEIGHT)
    previous = sweep.apply({"TOP_K": 2, "MAX_WEIGHT": 0.25})
    try:
        assert (config.TOP_K, config.MAX_WEIGHT) == (2, 0.25)
    finally:
        sweep.restore(previous)
    assert (config.TOP_K, config.MAX_WEIGHT) == before


# ------------------------------------------------------------------ 표 출력
def test_비교표는_계산_불가능한_값을_0으로_속이지_않는다():
    """None 을 0 으로 찍으면 '성과가 없다'로 오독된다 (performance.py 와 같은 원칙)."""
    perf = {"cum_return_pct": 1.0, "excess_return_pct": None, "sharpe": None,
            "max_drawdown_pct": 2.0, "avg_turnover_pct": None,
            "win_rate_pct": None, "total_costs": None}
    table = sweep.format_table("TOP_K", [(3, perf)])
    assert "—" in table
    assert "0.00%" not in table
