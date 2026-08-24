"""
스냅샷 빌더 테스트.

스냅샷은 **서버 없이 열리는 단일 HTML** 이다. 그런데 대시보드 원본에는 서버를
부르는 버튼이 셋 있다(사이클 실행 · 새로고침 · PM 논평 받기). 이것들이 스냅샷에
살아남으면 누른 사람에게 조용히 실패한다 — 특히 공개 배포본에서는 '($)' 가 붙은
유료 버튼이 눌리지 않는 채로 보이게 된다.

그리고 빌더는 `dashboard.html` 의 문자열을 앵커로 치환한다. 앵커가 어긋나면
폴링이 살아 있는 채로 스냅샷이 만들어져 **서버 없이 열면 아무것도 안 뜬다.**
그 조용한 실패를 막는 가드가 실제로 작동하는지 고정한다.
"""
import json

import pytest

import build_snapshot
from core import config


@pytest.fixture
def demo_state(tmp_path, monkeypatch):
    """대시보드가 읽는 최소 상태. 빌더는 값을 해석하지 않고 통째로 삽입한다."""
    state = {
        "cycle_no": 7,
        "as_of": "2026-08-24",
        "meta": {"pm_llm_available": True, "pm_llm_mode": "once"},
        "portfolio": {"equity": 1_000_000.0},
        "watchlist": [],
        "orders": [],
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "STATE_FILE", path)
    return state


def build(tmp_path):
    out = tmp_path / "snap.html"
    build_snapshot.build(out)
    return out.read_text(encoding="utf-8")


# ------------------------------------------------------------------ 정상 빌드
def test_상태가_문서에_박혀_들어간다(tmp_path, demo_state):
    html = build(tmp_path)
    assert 'id="snapshot-state"' in html
    assert '"cycle_no": 7' in html or '"cycle_no":7' in html


def test_폴링이_제거된다(tmp_path, demo_state):
    """폴링이 남으면 서버 없이 열었을 때 화면이 비어 있다."""
    html = build(tmp_path)
    assert "setInterval(load,15000)" not in html


def test_서버를_부르는_버튼이_모두_감춰진다(tmp_path, demo_state):
    """스냅샷에는 서버가 없다. 눌리지 않는 버튼을 남겨두면 안 된다."""
    html = build(tmp_path)
    for btn in ("runBtn", "refreshBtn", "pmBtn"):
        assert f"'{btn}'" in html, f"{btn} 을 감추는 코드가 없다"
    assert "SNAPSHOT._llm_available = false" in html


def test_부제에_스냅샷_표시가_붙는다(tmp_path, demo_state):
    html = build(tmp_path)
    assert "스냅샷 (사이클 #7" in html


def test_보유_섹션은_스냅샷에서_요청을_보내지_않는다(tmp_path, demo_state):
    """스냅샷에는 서버가 없다.

    감추기만 하면 초기화 코드가 그대로 돌아 /api/brokers 404 가 콘솔에 쌓인다.
    그리고 공개 스냅샷에 개인 보유 정보가 실릴 이유가 없다.
    """
    html = build(tmp_path)
    assert "window.__SNAPSHOT__ = true" in html

    dashboard = (config.WEB_DIR / "dashboard.html").read_text(encoding="utf-8")
    assert "if(window.__SNAPSHOT__)" in dashboard, "대시보드가 플래그를 확인하지 않는다"


# ---------------------------------------------------------- 앵커 어긋남 방어
def test_앵커를_못_찾으면_조용히_넘어가지_않고_실패한다(tmp_path, demo_state, monkeypatch):
    """가장 위험한 실패는 '깨진 스냅샷이 성공한 척 만들어지는' 것이다."""
    monkeypatch.setattr(build_snapshot, "POLL_ANCHOR", "존재하지-않는-앵커")
    with pytest.raises(RuntimeError, match="앵커를 찾지 못했습니다"):
        build_snapshot.build(tmp_path / "snap.html")


def test_실패한_빌드는_안내_문구를_준다(tmp_path, demo_state, monkeypatch):
    monkeypatch.setattr(build_snapshot, "ROOT_ANCHOR", "<div id='없음'>")
    with pytest.raises(RuntimeError, match="build_snapshot.py 의 앵커도 같이 고치세요"):
        build_snapshot.build(tmp_path / "snap.html")
