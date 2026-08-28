#!/usr/bin/env python
"""
시장 브리핑 CLI — 지표 + 뉴스 + 주제별 코멘트/대응.

    python scripts/macro.py                 # 화면 출력
    python scripts/macro.py --save          # state/macro.json 저장 (대시보드가 읽는다)
    python scripts/macro.py --no-news       # 뉴스 없이 지표만 (빠르다)
    python scripts/macro.py --json out.json

LLM 을 부르지 않으므로 몇 번을 돌려도 비용은 0원이다.
"""
import argparse
import json
import logging
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

warnings.filterwarnings("ignore")
from core import config, macro_brief, macro_desk, news_desk, storage   # noqa: E402


def render(macro, news, brief):
    L = []
    W = 76
    L.append("=" * W)
    L.append(f"시장 브리핑 — 기준 {macro['asof']}")
    L.append("=" * W)
    L.append(brief["summary"])
    if macro.get("failed"):
        L.append(f"※ 시세를 못 받은 심볼: {', '.join(macro['failed'])}")
    if news:
        L.append(f"뉴스 {news['count']}건 · 소스 {news['sources_ok']}/{news['sources_total']}")
        for f in news.get("failed", []):
            L.append(f"  ※ {f['source']} 수집 실패: {f['error'][:60]}")
    L.append("")

    for b in brief["briefs"]:
        L.append("─" * W)
        L.append(f"■ {b['label']}   [{b['outlook']['bias']}]")
        L.append("─" * W)
        for f in b["facts"]:
            tag = f"  {' '.join('#' + t for t in f['tags'])}" if f["tags"] else ""
            L.append(f"  {f['label']:<14} {f['value']:>12}   {f['detail']}")
            L.append(f"  {'':<14} {f['trend']}{tag}")
        L.append("")
        if b["comment"]:
            L.append("  [코멘트]")
            for line in _wrap(b["comment"], W - 4):
                L.append(f"    {line}")
            L.append("")
        if b["outlook"]["watch"]:
            L.append("  [무엇을 지켜볼 것인가]")
            for w in b["outlook"]["watch"]:
                for i, line in enumerate(_wrap(w, W - 8)):
                    L.append(f"    {'· ' if i == 0 else '  '}{line}")
            L.append("")
        if b["actions"]:
            L.append("  [대응 후보]")
            for a in b["actions"]:
                L.append(f"    ▸ {a['what']}")
                for line in _wrap("근거: " + a["why"], W - 10):
                    L.append(f"      {line}")
                for line in _wrap("대가: " + a["risk"], W - 10):
                    L.append(f"      {line}")
                L.append("")
        if b.get("news"):
            L.append("  [관련 뉴스]")
            for n in b["news"]:
                L.append(f"    · [{n['source']}] {n['title'][:64]}")
            L.append("")

    L.append("=" * W)
    L.append("지표는 계산된 사실이고, 코멘트와 대응은 규칙에 따른 해석이다.")
    L.append("전망은 예측이 아니라 조건문으로 적혀 있다 — 투자 판단의 책임은 본인에게 있다.")
    L.append("=" * W)
    return "\n".join(L)


def _wrap(text, width):
    """한글 폭을 대충 2로 세는 단순 줄바꿈."""
    words, lines, cur, w = text.split(), [], [], 0
    for word in words:
        ww = sum(2 if ord(c) > 0x1100 else 1 for c in word) + 1
        if w + ww > width and cur:
            lines.append(" ".join(cur))
            cur, w = [word], ww
        else:
            cur.append(word)
            w += ww
    if cur:
        lines.append(" ".join(cur))
    return lines


def build(with_news=True, period="2y"):
    macro = macro_desk.collect(period=period)
    news = news_desk.collect() if with_news else None
    brief = macro_brief.build(macro, news)
    return macro, news, brief


def main():
    ap = argparse.ArgumentParser(description="시장 지표·뉴스 브리핑")
    ap.add_argument("--no-news", action="store_true", help="뉴스 수집을 건너뛴다")
    ap.add_argument("--period", default="2y", help="시세 조회 기간")
    ap.add_argument("--save", action="store_true",
                    help="state/macro.json 에 저장 (대시보드가 읽는다)")
    ap.add_argument("--json", default=None, help="JSON 저장 경로")
    ap.add_argument("-q", "--quiet", action="store_true", help="화면 출력 생략")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    macro, news, brief = build(with_news=not args.no_news, period=args.period)
    if not args.quiet:
        print(render(macro, news, brief))

    # 저장 스키마는 `core/macro_api.py` 와 같아야 한다 — 대시보드가 둘을 구분하지 않고
    # 읽으므로, 여기만 다른 모양으로 쓰면 '언제 받은 데이터인지'가 화면에서 사라진다.
    payload = {
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "macro": macro, "news": news, "brief": brief,
    }
    if args.json:
        Path(args.json).write_bytes(
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"\n저장: {args.json}")
    if args.save:
        p = config.STATE_DIR / "macro.json"
        storage.write_json(p, payload)
        print(f"\n저장: {p}")


if __name__ == "__main__":
    main()
