#!/usr/bin/env python3
"""
대시보드를 단일 HTML 로 굽는다. 세 가지 결과물이 나온다.

  python build_snapshot.py out.html              봇 화면 스냅샷 (공유·보관용)
  python build_snapshot.py out.html --remote     '내 종목' 이 살아 있는 배포본
  python build_snapshot.py out.html --remote --no-bot
                                                 봇 화면을 아예 빼고 '내 종목'만

`--no-bot` 이 공개 배포의 기본이다. 운영자의 모의 운용 상태를 공개할 이유가 없고,
방문자에게는 자기 종목을 넣어보는 화면이 전부여야 하기 때문이다.
"""
import json
import sys
from pathlib import Path

from core import config

STUB = """
// ---- 스냅샷 모드: 서버 없이 임베드된 상태를 그대로 렌더한다 ----
const SNAPSHOT = JSON.parse(document.getElementById('snapshot-state').textContent);
// 스냅샷에는 서버가 없다. 서버를 부르는 버튼은 전부 감춘다 —
// 남겨두면 눌렀을 때 조용히 실패한다. 특히 'PM 논평 받기 ($)' 는
// render() 가 상태를 보고 다시 켜므로 render 뒤에 숨겨야 한다.
SNAPSHOT._llm_available = false;
async function load(){
  render(SNAPSHOT);
  for (const id of ['runBtn','refreshBtn','pmBtn']) {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }
}
// '내 보유' 섹션의 동작 모드. 두 가지로 빌드된다.
//   기본(snapshot) : 서버가 전혀 없는 단일 HTML. 보유 섹션은 아무 요청도 보내지 않는다.
//   remote         : Vercel 처럼 검색·시세 중계 함수(/api/search, /api/market)만 있는 곳.
//                    보유 목록은 방문자 브라우저의 localStorage 에 저장되고,
//                    진단은 브라우저에서 계산한다. Kronos 견해는 나오지 않는다.
window.__MODE_FLAG__
window.__BASE_CCY__ = "__BASE_CCY__";
"""

# 봇 화면을 빼고 굽는 경우. render() 를 부를 상태 자체가 없으므로 load() 를 비운다.
STUB_NO_BOT = """
// ---- '내 종목' 전용 배포본 ----
// 봇 화면(모의 포트폴리오·성과·PM 논평)은 이 빌드에 실려 있지 않다.
// 운영자의 운용 상태를 공개할 이유가 없고, 방문자에게 필요한 것은 자기 종목을
// 넣어보는 화면뿐이기 때문이다.
async function load(){}
window.__MODE_FLAG__
window.__BASE_CCY__ = "__BASE_CCY__";
"""

# 대시보드에서 찾아 바꿀 앵커들. dashboard.html 을 고치다 이 문자열이 어긋나면
# 스냅샷이 조용히 깨진 채(폴링이 살아 있어 서버 없이는 아무것도 안 뜬다)
# 만들어지므로, 하나라도 못 찾으면 빌드를 실패시킨다.
POLL_ANCHOR = "load();\npolling=setInterval(load,15000);"
ROOT_ANCHOR = '<div id="root">'
SUB_ANCHOR = '<div class="sub">Kronos · skfolio · NautilusTrader · PM 감독</div>'
# 시장 브리핑을 심는 자리. 봇 화면을 뺀 빌드에서도 남아 있어야 하므로 탭 바를 쓴다.
TABS_ANCHOR = '<div class="tabs" id="tabs">'


def _replace_once(html, old, new, what):
    if old not in html:
        raise RuntimeError(
            f"스냅샷 빌드 실패: dashboard.html 에서 '{what}' 앵커를 찾지 못했습니다.\n"
            f"  찾던 문자열: {old[:60]!r}\n"
            "  dashboard.html 을 수정했다면 build_snapshot.py 의 앵커도 같이 고치세요."
        )
    return html.replace(old, new, 1)


def _strip_bot(html):
    """봇 화면과 그에 딸린 상단 컨트롤을 문서에서 들어낸다.

    감추는 게 아니라 **지운다**. 감추기만 하면 운영자의 운용 데이터가 파일 안에는
    남아서, 소스만 열어보면 다 보인다.
    """
    start = html.index('  <div class="tabpane" id="pane-bot">')
    end_anchor = '<div class="loading">에이전트 상태를 불러오는 중…</div></div>\n  </div>'
    end = html.index(end_anchor) + len(end_anchor)
    html = html[:start] + html[end:]

    # 봇 탭 버튼 — JS 로 숨기지 않고 아예 없앤다
    html = html.replace(
        '    <button data-tab="bot" id="botTab" style="display:none">봇 대시보드</button>\n', "")

    # 상단 컨트롤(사이클 실행·논평·신선도 배지)은 전부 봇 전용이다.
    # 여는 태그만 남기면 div 가 안 닫혀 뒤따르는 요소가 헤더 안으로 빨려 들어간다 —
    # 블록째 들어낸다.
    ctrl_start = html.index('    <div class="controls">')
    ctrl_end = html.index("</div>", html.index('<button id="refreshBtn">')) + len("</div>")
    while ctrl_end < len(html) and html[ctrl_end] in "\r\n":
        ctrl_end += 1
    html = html[:ctrl_start] + html[ctrl_end:]

    html = html.replace(
        '<div class="sub">Kronos · skfolio · NautilusTrader · PM 감독</div>',
        '<div class="sub">내 종목 진단 — 비중 · 손익 · 집중도 · 상관 · 변동성</div>')

    # 보유 섹션 안내는 봇 화면이 같이 있을 때를 전제로 쓰여 있다.
    # 봇을 들어낸 빌드에서는 '봇의 모의 포트폴리오와 별개' 같은 말이 뜬금없다.
    html = html.replace('<div class="title">내 보유 종목</div>',
                        '<div class="title">내 종목</div>')
    old_note = ('실제 계좌의 보유분입니다 —' + chr(10) +
                '          봇의 모의 포트폴리오와 별개이며, 여기 넣은 종목이 봇의 매매 판단에 쓰이지 않습니다')
    if old_note in html:
        html = html.replace(
            old_note,
            '가진 종목을 넣으면 실제 시세로 비중·손익과 분산 상태를 계산합니다 —' + chr(10) +
            '          목록은 이 브라우저에만 저장되고 서버로 전송되지 않습니다')
    return html


def _embed_macro(html, macro_path=None):
    """시장 브리핑을 문서에 구워 넣는다.

    보유 종목과 달리 **시장 데이터는 공개해도 되는 값**이다(운영자 개인정보가 아니다).
    그래서 배포본에도 실어서, 서버가 없는 곳에서도 지표·코멘트·대응을 볼 수 있게 한다.
    갱신 버튼은 대시보드 쪽에서 `window.__MACRO__` 를 보고 알아서 감춘다.

    파일이 없으면 아무것도 심지 않는다 — 그 경우 화면이 탭 자체를 없앤다.
    """
    path = macro_path or (config.STATE_DIR / "macro.json")
    if not path.exists():
        print("  (시장 브리핑 없음 — 탭이 빠집니다. `python scripts/macro.py --save` 로 만드세요)")
        return html
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("running", None)           # 실행 중 플래그는 정적 파일에서 뜻이 없다
    payload = json.dumps(data, ensure_ascii=False)
    print(f"  시장 브리핑 임베드: {len(payload):,} bytes "
          f"(기준 {data.get('macro', {}).get('asof')})")
    # 앵커로 탭 바를 쓴다. `<div id="root">` 는 --no-bot 빌드에서 `_strip_bot` 이
    # 통째로 들어내므로 여기서는 쓸 수 없다 (봇 화면 안에 들어 있다).
    return _replace_once(
        html, TABS_ANCHOR,
        '<script id="macro-data" type="application/json">' + payload + '</script>'
        + chr(10) + '  <script>window.__MACRO__ = JSON.parse('
        'document.getElementById("macro-data").textContent);</script>'
        + chr(10) + '  ' + TABS_ANCHOR,
        "탭 바(매크로 삽입 지점)",
    )


def _embed_scorecard(html, path=None):
    """모델 성적표를 문서에 구워 넣는다.

    **성적이 나쁘다고 빼지 않는다.** 이 프로젝트가 공개된 채로 유지되려면 예측력이
    검증되지 않았다는 사실이 화면에 남아 있어야 한다 — 소개 탭의 면책 문구가
    이 표를 근거로 삼는다.
    """
    path = path or (config.STATE_DIR / "scorecard.json")
    if not path.exists():
        print("  (모델 성적표 없음 — `python scripts/validate.py --save-state` 로 만드세요)")
        return html
    data = json.loads(path.read_text(encoding="utf-8"))
    # 화면에 안 쓰는 큰 배열은 뺀다 (기간별 IC 시계열은 표에 나오지 않는다)
    if isinstance(data.get("ic"), dict):
        data["ic"].pop("series", None)
    payload = json.dumps(data, ensure_ascii=False)
    print(f"  모델 성적표 임베드: {len(payload):,} bytes (표본 {data.get('n')}건)")
    return _replace_once(
        html, TABS_ANCHOR,
        '<script id="scorecard-data" type="application/json">' + payload + '</script>'
        + chr(10) + '  <script>window.__SCORECARD__ = JSON.parse('
        'document.getElementById("scorecard-data").textContent);</script>'
        + chr(10) + '  ' + TABS_ANCHOR,
        "탭 바(성적표 삽입 지점)",
    )


def build(out_path, remote=False, base_currency=None, include_bot=True):
    """대시보드를 단일 HTML 로 굽는다.

    remote=True     '내 종목' 섹션이 살아 있는 배포본. 방문자가 자기 종목을 넣어볼 수
                    있고, 그 데이터는 방문자 브라우저에만 남는다.
    include_bot=False
                    봇 화면(모의 포트폴리오·성과·PM 논평·주목 종목)을 결과물에서
                    통째로 뺀다. 상태 JSON 도 심지 않으므로 운영자의 운용 내역이
                    파일에 남지 않고, 페이지도 훨씬 가벼워진다.
    """
    html = (config.WEB_DIR / "dashboard.html").read_text(encoding="utf-8")
    mode_flag = "window.__REMOTE__ = true;" if remote else "window.__SNAPSHOT__ = true;"
    ccy = base_currency or config.HOLDINGS_BASE_CURRENCY

    if not include_bot:
        stub = (STUB_NO_BOT.replace("window.__MODE_FLAG__", mode_flag)
                .replace('"__BASE_CCY__"', '"' + ccy + '"'))
        html = _strip_bot(html)
        html = _replace_once(html, POLL_ANCHOR, stub + "\nload();", "폴링 시작 지점")
        html = _embed_macro(html)
        html = _embed_scorecard(html)
        Path(out_path).write_text(html, encoding="utf-8")
        print(f"저장: {out_path}  ({len(html):,} bytes, 봇 화면 없음)")
        return

    state = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    stub = STUB.replace("window.__MODE_FLAG__", mode_flag).replace(
        '"__BASE_CCY__"', '"' + ccy + '"')

    # 상태를 문서에 삽입
    html = _replace_once(
        html, ROOT_ANCHOR,
        '<script id="snapshot-state" type="application/json">'
        + json.dumps(state, ensure_ascii=False) + '</script>\n  <div id="root">',
        "상태 삽입 지점",
    )
    # 폴링/실행 버튼을 스냅샷 동작으로 교체
    html = _replace_once(html, POLL_ANCHOR, stub + "\nload();", "폴링 시작 지점")
    html = _embed_macro(html)
    html = _embed_scorecard(html)

    # 배지에 스냅샷 표시
    html = _replace_once(
        html, SUB_ANCHOR,
        f'<div class="sub">Kronos · skfolio · NautilusTrader · PM 감독 · '
        f'스냅샷 (사이클 #{state["cycle_no"]}, {state["as_of"]} 기준)</div>',
        "부제 배지",
    )

    Path(out_path).write_text(html, encoding="utf-8")
    print(f"저장: {out_path}  ({len(html):,} bytes, 사이클 #{state['cycle_no']})")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    build(args[0] if args else "agent_snapshot.html",
          remote="--remote" in sys.argv,
          include_bot="--no-bot" not in sys.argv)
