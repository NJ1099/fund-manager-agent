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
    assert "window.__REMOTE__ = true" not in html

    dashboard = (config.WEB_DIR / "dashboard.html").read_text(encoding="utf-8")
    assert "window.__SNAPSHOT__ ? 'snapshot'" in dashboard, "대시보드가 모드를 판정하지 않는다"


def test_remote_빌드는_보유_섹션을_살려둔다(tmp_path, demo_state):
    """Vercel 배포본은 방문자가 자기 종목을 넣어볼 수 있어야 한다.

    검색·시세는 서버리스 중계 함수를 쓰고, 목록은 그 사람 브라우저에만 저장된다.
    """
    out = tmp_path / "remote.html"
    build_snapshot.build(out, remote=True)
    html = out.read_text(encoding="utf-8")

    assert "window.__REMOTE__ = true" in html
    assert "window.__SNAPSHOT__ = true" not in html


def test_기준통화가_빌드에_박힌다(tmp_path, demo_state):
    out = tmp_path / "remote.html"
    build_snapshot.build(out, remote=True, base_currency="USD")
    html = out.read_text(encoding="utf-8")

    assert 'window.__BASE_CCY__ = "USD"' in html
    # 치환이 변수명까지 먹어치우지 않아야 한다 (과거에 한 번 그랬다)
    assert "window.USD" not in html


# ------------------------------------------------- 봇 화면 없는 배포본 (--no-bot)
def test_봇_화면을_빼면_운용_데이터가_파일에_남지_않는다(tmp_path, demo_state):
    """감추는 것과 빼는 것은 다르다.

    감추기만 하면 운영자의 모의 운용 내역이 파일 안에 그대로 남아서, 소스만 열어보면
    다 보인다. 공개 배포본에 그게 실릴 이유가 없다.
    """
    out = tmp_path / "nobot.html"
    build_snapshot.build(out, remote=True, include_bot=False)
    html = out.read_text(encoding="utf-8")

    assert 'id="snapshot-state"' not in html
    assert str(demo_state["portfolio"]["equity"]) not in html
    assert 'id="pane-bot"' not in html


def test_봇_화면을_빼면_봇_탭도_사라진다(tmp_path, demo_state):
    """탭만 남으면 눌렀을 때 빈 화면이 나온다."""
    out = tmp_path / "nobot.html"
    build_snapshot.build(out, remote=True, include_bot=False)
    html = out.read_text(encoding="utf-8")

    assert 'data-tab="bot"' not in html
    assert 'data-tab="holdings"' in html          # 나머지 탭은 남아 있어야 한다
    assert 'id="pane-howto"' in html
    assert 'id="pane-about"' in html


def test_봇_컨트롤은_닫는_태그까지_함께_사라진다(tmp_path, demo_state):
    """여는 태그만 남기면 div 가 안 닫혀 뒤 요소가 헤더 안으로 빨려 들어간다.

    실제로 한 번 그렇게 레이아웃이 무너졌다.
    """
    out = tmp_path / "nobot.html"
    build_snapshot.build(out, remote=True, include_bot=False)
    html = out.read_text(encoding="utf-8")

    assert 'class="controls"' not in html
    assert 'id="runBtn"' not in html
    assert html.count("<body>") == html.count("</body>") == 1


def test_봇_없는_빌드도_보유_기능은_살아_있다(tmp_path, demo_state):
    out = tmp_path / "nobot.html"
    build_snapshot.build(out, remote=True, include_bot=False)
    html = out.read_text(encoding="utf-8")

    assert "window.__REMOTE__ = true" in html
    assert 'id="symQ"' in html                    # 검색창
    assert "/api/search" in html                  # 중계 함수 호출


def test_봇_없는_빌드는_상태파일이_없어도_만들어진다(tmp_path, monkeypatch):
    """공개 배포본은 운영자가 사이클을 한 번도 안 돌렸어도 나와야 한다."""
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "없는파일.json")
    out = tmp_path / "nobot.html"

    build_snapshot.build(out, remote=True, include_bot=False)
    assert out.exists()


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


# ------------------------------------------------ 시장 브리핑 · 모델 성적표 임베드
@pytest.fixture
def macro_state(tmp_path, monkeypatch):
    """시장 브리핑·성적표 파일을 임시 디렉토리에 놓는다.

    이 픽스처가 없으면 빌더가 실제 `state/` 를 읽어서, 테스트 결과가 개발 머신에
    무엇이 저장돼 있느냐에 따라 달라진다.
    """
    d = tmp_path / "state"
    d.mkdir()
    (d / "macro.json").write_text(json.dumps({
        "collected_at": "2026-08-28T06:00:00+00:00",
        "macro": {"asof": "2026-08-28", "count": 27, "assets": {}, "cross": []},
        "news": {"count": 5},
        "brief": {"summary": "테스트 요약", "briefs": []},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "scorecard.json").write_text(json.dumps({
        "n": 1400, "sufficient": True, "verdict": "테스트 결론",
        "ic": {"mean": -0.003, "series": [{"asof": "x", "ic": 0.1}]},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "STATE_DIR", d)
    return d


def test_시장_브리핑이_배포본에_실린다(tmp_path, demo_state, macro_state):
    """시장 데이터는 운영자 개인정보가 아니므로 배포본에 실어도 된다 —
    서버가 없는 곳에서도 지표와 코멘트를 볼 수 있게 하는 것이 목적이다."""
    html = build(tmp_path)
    assert 'id="macro-data"' in html
    assert "window.__MACRO__" in html
    assert "테스트 요약" in html


def test_봇을_뺀_빌드에도_시장_브리핑이_실린다(tmp_path, demo_state, macro_state):
    """회귀 고정 — 삽입 앵커로 `<div id="root">` 를 쓰면 안 된다.
    그건 봇 화면 안에 있어서 `--no-bot` 빌드에서 통째로 사라지고,
    빌드가 '앵커를 못 찾았다'로 실패한다."""
    out = tmp_path / "nobot.html"
    build_snapshot.build(out, remote=True, include_bot=False)
    html = out.read_text(encoding="utf-8")
    assert 'id="macro-data"' in html
    assert 'id="scorecard-data"' in html


def test_모델_성적표가_배포본에_실린다(tmp_path, demo_state, macro_state):
    """성적이 나쁘다고 빼지 않는다 — 소개 탭의 면책 문구가 이 표를 근거로 삼는다."""
    html = build(tmp_path)
    assert 'id="scorecard-data"' in html
    assert "테스트 결론" in html


def test_성적표의_IC_시계열은_배포본에서_빠진다(tmp_path, demo_state, macro_state):
    """화면에 쓰지 않는 큰 배열까지 실으면 배포본만 무거워진다."""
    html = build(tmp_path)
    assert '"series"' not in html


def test_브리핑_파일이_없어도_빌드는_성공한다(tmp_path, demo_state, monkeypatch):
    """파일이 없는 것은 오류가 아니다 — 화면이 탭을 없애는 쪽으로 처리한다."""
    empty = tmp_path / "empty_state"
    empty.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", empty)
    html = build(tmp_path)
    # 판정은 데이터 태그로 한다. `window.__MACRO__` 라는 문자열 자체는 대시보드
    # 스크립트가 늘 참조하므로, 그걸로는 임베드 여부를 가릴 수 없다.
    assert 'id="macro-data"' not in html
    assert 'id="scorecard-data"' not in html
    assert 'id="tabs"' in html          # 문서 자체는 멀쩡하다
