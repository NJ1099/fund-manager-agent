#!/usr/bin/env python3
"""
백테스트 실행기.

  python scripts/backtest.py --start 2025-01-01 --end 2026-06-30 --step 5
  python scripts/backtest.py --dry-run              # 추론 몇 번 도는지만 먼저 본다
  python scripts/backtest.py --step 10 --no-cache   # 추론 난수를 새로 뽑아 재확인

첫 실행은 (종목 × 사이클)만큼 Kronos 추론이 필요해 오래 걸린다. 결과는
`state/infer_cache.jsonl` 에 쌓이므로, 추론 이후 단계의 파라미터(TOP_K,
MAX_TURNOVER, 게이트 한도 …)만 바꿔 다시 돌리면 거의 즉시 끝난다.

⚠️ 백테스트 성과는 상한이지 기대값이 아니다. 슬리피지가 없고, 워치리스트가
생존 편향을 갖는다. 자세한 한계는 `core/backtest.py` 모듈 독스트링에 있다.
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from core import backtest, config, data_desk, infer_cache, sweep  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bt")


def parse_args():
    ap = argparse.ArgumentParser(description="펀드매니저 파이프라인 백테스트")
    ap.add_argument("--start", help="시작일 YYYY-MM-DD (기본: 데이터가 허용하는 가장 이른 날)")
    ap.add_argument("--end", help="종료일 YYYY-MM-DD")
    ap.add_argument("--step", type=int, default=5,
                    help="리밸런싱 간격, 거래일 단위 (기본 5 = 주 1회)")
    ap.add_argument("--period", default="5y",
                    help="yfinance 다운로드 기간 (기본 5y). LOOKBACK 만큼의 앞 구간이 "
                         "컨텍스트로 소모되므로 원하는 백테스트 기간보다 넉넉해야 한다")
    ap.add_argument("--equity", type=float, default=config.INITIAL_EQUITY,
                    help="초기 자산")
    ap.add_argument("--no-cache", action="store_true",
                    help="추론 캐시를 쓰지 않는다 (같은 설정으로 견해 안정성을 볼 때)")
    ap.add_argument("--dry-run", action="store_true",
                    help="리밸런싱 날짜와 예상 추론 횟수만 출력하고 끝낸다")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE", dest="overrides",
                    help="설정 임시 변경 (예: --set TOP_K=3 --set MAX_WEIGHT=0.3). "
                         "안전장치(EXECUTION_MODE 등)는 바꿀 수 없다")
    ap.add_argument("--sweep", metavar="KEY=V1,V2,V3",
                    help="한 파라미터를 여러 값으로 돌려 비교표를 낸다 (예: --sweep TOP_K=3,5,7). "
                         "추론 캐시가 있으면 거의 즉시 끝난다")
    ap.add_argument("--out", help="결과 JSON 저장 경로 (기본 reports/ 아래 자동 생성)")
    ap.add_argument("--verbose", "-v", action="store_true", help="사이클 로그 전체 출력")
    return ap.parse_args()


def run_sweep(args, bars):
    """한 파라미터를 여러 값으로 돌려 비교표를 낸다.

    캐시는 값마다 새로 열지 않고 하나를 공유한다 — 추론은 파라미터와 무관하게
    (종목, 날짜)로만 결정되므로 첫 값에서 채운 캐시를 나머지가 그대로 쓴다.
    이게 스윕이 싸지는 이유다.
    """
    try:
        key, values = sweep.parse_sweep(args.sweep)
    except ValueError as e:
        print(f"스윕 오류: {e}")
        return 1

    cache = infer_cache.InferenceCache(enabled=not args.no_cache)
    scorer = cache.wrap()
    rows, results = [], {}

    try:
        for value in values:
            previous = sweep.apply({key: value})
            try:
                t0 = time.time()
                res = backtest.run(bars, start=args.start, end=args.end, step=args.step,
                                    scorer=scorer, initial_equity=args.equity)
                rows.append((value, res["performance"]))
                results[str(value)] = {"settings": res["settings"],
                                        "performance": res["performance"],
                                        "errors": res["errors"]}
                print(f"  {key}={value}: {res['settings']['completed_cycles']}사이클 "
                      f"{time.time() - t0:.1f}초 (캐시 {cache.hits}H/{cache.misses}M)",
                      flush=True)
            except Exception as e:
                print(f"  {key}={value}: 실패 — {e}")
            finally:
                sweep.restore(previous)
    finally:
        cache.close()

    if not rows:
        print("성공한 조합이 없습니다.")
        return 1

    print()
    print(sweep.format_table(key, rows))

    out = Path(args.out) if args.out else (BASE / "reports" / f"sweep_{key}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"key": key, "results": results},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"결과 저장     {out}")
    return 0


def main():
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    # 설정 오버라이드 — 잘못된 키는 여기서 즉시 실패시킨다.
    # 조용히 무시하면 "바꿨는데 결과가 같다"가 되어 원인을 찾기 어렵다.
    if args.overrides:
        try:
            applied = dict(sweep.parse_assignment(a) for a in args.overrides)
        except ValueError as e:
            print(f"설정 오류: {e}")
            return 1
        sweep.apply(applied)
        print("설정 변경: " + ", ".join(f"{k}={v}" for k, v in applied.items()))

    universe = list(dict.fromkeys(config.WATCHLIST + [config.BENCHMARK]))
    print(f"데이터 다운로드: {len(universe)}종목 · period={args.period} …")
    bars = data_desk.fetch_bars(universe, period=args.period)
    if not bars:
        print("데이터를 하나도 받지 못했습니다.")
        return 1

    dates = backtest.rebalance_dates(
        bars, args.start, args.end, args.step,
        tickers=[tk for tk in config.WATCHLIST if tk in bars])
    n_scan = len([tk for tk in config.WATCHLIST if tk in bars])

    if len(dates) == 0:
        span = backtest.common_dates(bars, [tk for tk in config.WATCHLIST if tk in bars])
        print("리밸런싱 기준일이 없습니다.")
        if len(span):
            print(f"  공통 거래일 {len(span)}일 ({str(span[0])[:10]} ~ {str(span[-1])[:10]}), "
                  f"앞 {config.LOOKBACK}일은 컨텍스트로 소모됩니다.")
            print(f"  --period 를 늘리거나 --start 를 {str(span[min(config.LOOKBACK, len(span) - 1)])[:10]} "
                  "이후로 잡으세요.")
        return 1

    print(f"기준일 {len(dates)}개: {str(dates[0])[:10]} ~ {str(dates[-1])[:10]} "
          f"(거래일 {args.step}일 간격)")
    print(f"예상 추론 {len(dates) * n_scan}회 (캐시 미적중 시)")

    if args.dry_run:
        for d in dates:
            print("  ", str(d)[:10])
        return 0

    if args.sweep:
        return run_sweep(args, bars)

    cache = infer_cache.InferenceCache(enabled=not args.no_cache)
    scorer = cache.wrap()

    t0 = time.time()

    def progress(i, total, record):
        el = time.time() - t0
        eta = el / i * (total - i)
        print(f"  [{i:>3}/{total}] {record['as_of']}  "
              f"자산 ${record['equity']:>12,.0f}  {record['period_return_pct']:+6.2f}%  "
              f"주문 {record['n_orders']:>2}건  "
              f"| 캐시 {cache.hits}H/{cache.misses}M  경과 {el:>5.0f}s  ETA {eta:>5.0f}s",
              flush=True)

    try:
        result = backtest.run(bars, start=args.start, end=args.end, step=args.step,
                              scorer=scorer, initial_equity=args.equity,
                              on_cycle=progress)
    finally:
        cache.close()

    result["cache"] = cache.stats

    print()
    print(backtest.format_report(result))
    print()
    if args.no_cache:
        print(f"추론 캐시     사용 안 함 (추론 {cache.misses}회)")
    else:
        st = cache.stats
        print(f"추론 캐시     적중 {st['hits']} / 신규 {st['misses']}"
              f" (적중률 {st['hit_rate_pct']}%) · 누적 {st['entries']}건")

    out = Path(args.out) if args.out else (
        BASE / "reports" / f"backtest_{result['settings']['start']}_"
                           f"{result['settings']['end']}_step{args.step}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"결과 저장     {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
