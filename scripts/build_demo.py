#!/usr/bin/env python3
"""
공개 배포본 빌더.

  python scripts/build_demo.py [출력경로]              '내 종목' 전용 (기본, 즉시)
  python scripts/build_demo.py [출력경로] --with-bot   봇 화면까지 든 스냅샷

기본 동작은 봇 화면 없이 '내 종목' 화면만 굽는 것이다. 운영자의 모의 운용 상태를
공개할 이유가 없고, 방문자에게 필요한 것은 자기 종목을 넣어보는 화면뿐이다.
`--with-bot` 은 봇 화면이 든 스냅샷이 필요할 때만 쓴다 (아래 설명 참고).

■ 왜 합성 데이터를 쓰지 않는가

데모 데이터를 손으로 지어내면 두 가지가 망가진다. 첫째, `state.json` 스키마가
꽤 넓어서 키를 하나만 빠뜨려도 대시보드가 조용히 깨진다. 둘째, 지어낸 숫자는
이 파이프라인이 실제로 무엇을 하는지 보여주지 못한다.

그래서 **데모 전용 상태 디렉토리에서 진짜 파이프라인을 돌린다.**
과거 구간을 백테스트로 리플레이해 이력을 쌓고, 그 장부를 이어받아 라이브
사이클을 한 번 실행한다. 나오는 것은 실제 시장 데이터로 만든 진짜 결과이며,
운영자의 개인 운용 상태(`state/`)와는 완전히 분리된 별도 디렉토리에 남는다.

■ LLM 은 절대 부르지 않는다

`PM_LLM_MODE=never` 를 강제한다. 데모를 만들 때마다 API 비용이 나가면 안 되고,
공개 데모에 유료 호출 결과가 박혀 있을 이유도 없다. 논평은 룰 기반으로 나온다.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# config 를 임포트하기 전에 LLM 을 꺼둔다 (모듈 로드 시점에 읽히는 값이다)
os.environ["PM_LLM_MODE"] = "never"

import logging  # noqa: E402

from core import backtest, config, data_desk, infer_cache, storage  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")

DEMO_DIR = BASE / "demo_state"
BACKTEST_STEP = 20          # 월 1회 리밸런싱
BACKTEST_MONTHS = 12        # 자산 곡선이 의미를 갖도록 넉넉히


def _redirect_state_to_demo():
    """config 의 상태 경로를 데모 디렉토리로 돌린다.

    운영자의 `state/` 를 절대 건드리지 않기 위한 조치다. bot.Agent 는
    config.STATE_FILE / HISTORY_FILE 만 보므로 이 두 개만 바꾸면 된다.
    """
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    DEMO_DIR.mkdir(parents=True)
    config.STATE_DIR = DEMO_DIR
    config.STATE_FILE = DEMO_DIR / "state.json"
    config.HISTORY_FILE = DEMO_DIR / "history.jsonl"


def _start_date(bars):
    """백테스트 시작일 — 데이터 마지막에서 BACKTEST_MONTHS 만큼 거슬러 올라간다."""
    idx = backtest.common_dates(bars, [tk for tk in config.WATCHLIST if tk in bars])
    if len(idx) == 0:
        raise RuntimeError("공통 거래일이 없습니다.")
    back = min(len(idx) - 1, BACKTEST_MONTHS * 21)
    return str(idx[-1 - back])[:10]


def build(out_path):
    t0 = time.time()
    print("데모 데이터 생성 — 운영자 state/ 는 건드리지 않습니다")
    _redirect_state_to_demo()

    universe = list(dict.fromkeys(config.WATCHLIST + [config.BENCHMARK]))
    print(f"  데이터 {len(universe)}종목 다운로드 …")
    bars = data_desk.fetch_bars(universe, period="5y")

    start = _start_date(bars)
    print(f"  백테스트 리플레이 {start} ~ (거래일 {BACKTEST_STEP}일 간격) …")
    cache = infer_cache.InferenceCache()
    try:
        result = backtest.run(bars, start=start, step=BACKTEST_STEP,
                              scorer=cache.wrap())
    finally:
        cache.close()

    hist = result["history"]
    print(f"  {len(hist)}사이클 · 누적 {result['performance']['cum_return_pct']:+.2f}% "
          f"(캐시 {cache.hits}H/{cache.misses}M)")

    # 이력을 데모 디렉토리에 기록 — 라이브 사이클이 여기에 이어 붙는다
    with config.HISTORY_FILE.open("w", encoding="utf-8") as f:
        for rec in hist:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 백테스트 종료 시점 장부를 state.json 으로 옮긴다.
    # bot.Agent._restore 가 이걸 읽어 포지션·현금·자산을 그대로 이어받는다.
    book = result["final_book"]
    storage.write_json(config.STATE_FILE, {
        "cycle_no": len(hist),
        "order_seq": book["seq"],
        "llm_calls": 0,
        "prices": book["last_prices"],
        "portfolio": {
            "cash": book["cash"],
            "positions": book["positions"],
            "costs_paid": book["costs_paid"],
            "equity": book["equity"],
            "peak_equity": book["peak_equity"],
            "start_equity": result["settings"]["initial_equity"],
            "period_return_pct": hist[-1]["period_return_pct"],
        },
    })

    # 라이브 사이클 1회 — 최신 시장 데이터로 실제 파이프라인을 돌린다.
    # (bot 임포트는 여기서 한다: config 경로를 먼저 돌려놓아야 하기 때문)
    print("  최신 데이터로 라이브 사이클 1회 실행 …")
    import bot                                     # noqa: E402
    agent = bot.Agent()
    cycle = agent.run_cycle()
    print(f"  사이클 #{cycle['cycle_no']} 완료 · 자산 ${cycle['equity']:,.0f}")

    # 공개 배포본 굽기.
    # remote=True       방문자가 '내 종목'에 자기 종목을 넣을 수 있다. 그 데이터는
    #                   방문자 브라우저에만 저장되고 서버로도 이 저장소로도 오지 않는다.
    # include_bot=False 봇 화면을 결과물에서 통째로 뺀다. 운영자의 모의 운용 상태를
    #                   공개할 이유가 없고, 방문자에게 필요한 것은 자기 종목 화면뿐이다.
    import build_snapshot                          # noqa: E402
    build_snapshot.build(out_path, remote=True, include_bot=False)

    # 데모임을 화면에서 분명히 밝힌다 — 남의 실계좌로 오해하면 안 된다
    html = Path(out_path).read_text(encoding="utf-8")
    banner = (
        '<div style="max-width:1180px;margin:18px auto 0;padding:12px 16px;'
        'border:1px solid #d8a13a;border-radius:10px;background:#2a2113;'
        'color:#e8c377;font-size:13px;line-height:1.7">'
        '<b>공개 데모 · 전 구간 모의투자</b><br>'
        '실제 시장 데이터로 파이프라인을 돌린 결과지만 브로커에 전송된 주문은 하나도 '
        '없습니다. 누군가의 실계좌가 아닙니다.<br>'
        '<b>여기 보이는 성과를 그대로 믿지 마세요.</b> 표본이 사이클 수십 개뿐이고, '
        '슬리피지가 반영되지 않았으며(종가 전량 체결 가정), 이 구간은 상승장이었습니다. '
        '다른 구간에서는 같은 설정으로 SPY 에 뒤졌습니다. 이 모델의 예측력은 검증되지 '
        '않았습니다 — 원저자의 독립 평가에서 무작위 걷기와 구분되지 않았습니다.<br>'
        '투자 조언이 아닙니다. 직접 돌려보려면 '
        '<a href="https://github.com/NJ1099/fund-manager-agent" '
        'style="color:#f0d19a">GitHub 저장소</a>를 참고하세요.'
        '</div>'
    )
    if '<div id="root">' not in html:
        raise RuntimeError("데모 배너 삽입 실패: '<div id=\"root\">' 앵커를 찾지 못했습니다.")
    html = html.replace('<div id="root">', banner + '<div id="root">', 1)
    Path(out_path).write_text(html, encoding="utf-8")

    print(f"완료 ({time.time() - t0:.0f}초) → {out_path}")
    print(f"데모 상태 디렉토리: {DEMO_DIR}  (gitignore 대상)")


def build_web_only(out_path):
    """공개 배포본만 굽는다 — 봇 데이터가 필요 없으므로 1초도 안 걸린다.

    배포본에는 봇 화면이 실리지 않으므로(운영자의 운용 상태를 공개할 이유가 없다),
    백테스트를 돌려 데모 데이터를 만들 이유도 없다.
    """
    import build_snapshot                          # noqa: E402
    build_snapshot.build(out_path, remote=True, include_bot=False)
    print(f"완료 → {out_path}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = args[0] if args else str(BASE / "public" / "index.html")
    if "--with-bot" in sys.argv:
        build(out)                                 # 봇 화면이 든 스냅샷까지 만든다
    else:
        build_web_only(out)
