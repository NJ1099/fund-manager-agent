"""대시보드가 외부 문자열을 HTML 로 실행하지 않는지. 소스만 본다."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_보유_화면은_외부_문자열을_이스케이프한다():
    for name in ("web/dashboard.html", "public/index.html"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "esc(h.ticker)" in text, name
        assert "esc(h.name)" in text, name
        assert "X-Requested-With" in text, name
        assert "usableNum" in text, name
        assert "safeUrl(" in text, name


def test_배포본은_로컬_POST_표식을_갖고_있다():
    """정적 배포본의 검색 호출도 같은 헬퍼를 탄다. 표식이 빠지면 로컬 서버와 갈라진다."""
    text = (ROOT / "public/index.html").read_text(encoding="utf-8")
    assert "FMA_HEADERS" in text
