#!/usr/bin/env python3
"""
현재 state.json 을 대시보드에 박아 넣은 단일 HTML 파일을 만든다.
서버 없이 브라우저로 바로 열어볼 수 있고, 공유·보관용 스냅샷으로 쓴다.

  python build_snapshot.py [출력경로]
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
// '내 보유' 섹션은 서버 API(/api/holdings 등)로 살아 움직인다. 스냅샷에는 서버가
// 없으므로 이 플래그로 초기화 자체를 막는다 — 감추기만 하면 요청은 그대로 나가서
// 콘솔에 404 가 쌓인다. 무엇보다 공개 스냅샷에 개인 보유 정보가 실릴 이유가 없다.
window.__SNAPSHOT__ = true;
"""

# 대시보드에서 찾아 바꿀 앵커들. dashboard.html 을 고치다 이 문자열이 어긋나면
# 스냅샷이 조용히 깨진 채(폴링이 살아 있어 서버 없이는 아무것도 안 뜬다)
# 만들어지므로, 하나라도 못 찾으면 빌드를 실패시킨다.
POLL_ANCHOR = "load();\npolling=setInterval(load,15000);"
ROOT_ANCHOR = '<div id="root">'
SUB_ANCHOR = '<div class="sub">Kronos · skfolio · NautilusTrader · PM 감독</div>'


def _replace_once(html, old, new, what):
    if old not in html:
        raise RuntimeError(
            f"스냅샷 빌드 실패: dashboard.html 에서 '{what}' 앵커를 찾지 못했습니다.\n"
            f"  찾던 문자열: {old[:60]!r}\n"
            "  dashboard.html 을 수정했다면 build_snapshot.py 의 앵커도 같이 고치세요."
        )
    return html.replace(old, new, 1)


def build(out_path):
    state = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    html = (config.WEB_DIR / "dashboard.html").read_text(encoding="utf-8")

    # 상태를 문서에 삽입
    html = _replace_once(
        html, ROOT_ANCHOR,
        '<script id="snapshot-state" type="application/json">'
        + json.dumps(state, ensure_ascii=False) + '</script>\n  <div id="root">',
        "상태 삽입 지점",
    )
    # 폴링/실행 버튼을 스냅샷 동작으로 교체
    html = _replace_once(html, POLL_ANCHOR, STUB + "\nload();", "폴링 시작 지점")
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
    build(sys.argv[1] if len(sys.argv) > 1 else "agent_snapshot.html")
