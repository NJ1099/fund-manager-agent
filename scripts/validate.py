#!/usr/bin/env python
"""
모델 성적표 CLI — 추론 캐시에 쌓인 과거 예측을 실제 결과와 대조한다.

    python scripts/validate.py                  # 캐시 전체로 성적표
    python scripts/validate.py --horizon 5      # 예측 지평(거래일) 지정
    python scripts/validate.py --min-samples 50 # 표본 문턱 조정
    python scripts/validate.py --json out.json  # 결과 저장

추론은 한 번도 하지 않는다. 캐시에 없는 것은 세지 않는다.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config, validation      # noqa: E402


def _pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


def render(g):
    """성적표를 사람이 읽을 수 있게 출력한다."""
    L = []
    L.append("=" * 68)
    L.append("모델 성적표 — 견해가 실제로 맞았는가")
    L.append("=" * 68)

    if g.get("n", 0) == 0:
        L.append(g.get("note", "표본이 없습니다."))
        return "\n".join(L)

    r = g.get("realize", {})
    L.append(f"표본 {g['n']}건 · 종목 {len(g['tickers'])}개 · 스캔일 {g.get('dates')}일 "
             f"· 지평 {r.get('horizon')}거래일")
    if g.get("period"):
        L.append(f"구간 {g['period'][0]} ~ {g['period'][1]}")
    if r.get("pending"):
        L.append(f"아직 결과가 나오지 않은 예측 {r['pending']}건 (제외)")
    if r.get("no_price"):
        L.append(f"가격을 못 구한 예측 {r['no_price']}건 (제외)")
    if g.get("cache", {}).get("stale"):
        L.append(f"추론 설정이 달라 무시한 캐시 {g['cache']['stale']}건")
    L.append("")

    if not g.get("sufficient"):
        L.append(f"※ 표본이 {g['min_samples']}건 미만이라 지표를 내지 않습니다.")
        L.append(f"   {g.get('verdict', '')}")
        return "\n".join(L)

    d = g["direction"]
    if not d.get("insufficient"):
        L.append("[1] 방향 적중률 — 오를지 내릴지나 맞히는가")
        L.append(f"    적중 {d['hits']}/{d['n']} = {_pct(d['rate'])} "
                 f"(95% 구간 {_pct(d['ci95'][0])}~{_pct(d['ci95'][1])})")
        L.append(f"    기준선: 늘 '상승'으로 찍기 = {_pct(d['naive_always_up'])}")
        L.append(f"    → {'기준선을 넘었다' if d['beats_naive'] else '기준선을 넘지 못했다'}")
        L.append("")

    ic = g["ic"]
    L.append("[2] IC (순위상관) — 종목 간 상대 순위를 맞히는가")
    if ic.get("insufficient"):
        L.append(f"    기간 {ic['periods']}개 — 부족 (5개 이상 필요)")
    else:
        L.append(f"    평균 {ic['mean']:+.4f} · 표준편차 {ic['std']:.4f} · "
                 f"IR {ic['ir']} · t={ic['t_stat']}")
        L.append(f"    IC>0 인 기간 비율 {_pct(ic['positive_share'])} ({ic['periods']}기간)")
        sig = ic["t_stat"] is not None and abs(ic["t_stat"]) >= 2.0
        L.append(f"    → {'통계적으로 유의' if sig else '유의하지 않음 (t<2)'}")
    L.append("")

    bc = g["by_confidence"]
    L.append("[3] 확신도 구간별 적중률 ★ 이 시스템의 핵심 가정")
    L.append(f"    {'구간':<6}{'범위':<14}{'표본':>5}  {'적중률':>7}  {'평균 실제':>9}")
    for b in bc["bins"]:
        mark = "" if b["enough"] else "  (표본 부족)"
        L.append(f"    {b['label']:<6}{str(b['range']):<14}{b['n']:>5}  "
                 f"{_pct(b['hit_rate']):>7}  {b['mean_actual_pct']:>8.2f}%{mark}")
    if bc["conf_vs_hit_corr"] is not None:
        L.append(f"    확신도-적중 상관: {bc['conf_vs_hit_corr']:+.4f}")
    if bc["monotonic"] is not None:
        L.append(f"    확신도가 오를수록 적중률도 오르는가: "
                 f"{'예' if bc['monotonic'] else '아니오'}")
    L.append("")

    c = g["calibration"]
    L.append("[4] 팬차트 커버리지 — 5~95 구간이 정직한가 (목표 90%)")
    if c.get("insufficient"):
        L.append(f"    표본 {c['n']}건 — 부족")
    else:
        L.append(f"    실제가 구간 안: {_pct(c['coverage'])} "
                 f"(아래로 벗어남 {c['below_p05']}, 위로 {c['above_p95']})")
        L.append(f"    평균 구간 폭 {c['mean_width_pct']:.2f}%")
        L.append(f"    → {c['verdict']}")
    L.append("")

    b = g["brier"]
    L.append("[5] Brier 점수 — 상승 확률 예측이 상수보다 나은가")
    if b.get("insufficient"):
        L.append(f"    표본 {b['n']}건 — 부족")
    else:
        L.append(f"    Brier {b['score']:.4f} vs 기준선 {b['baseline']:.4f} "
                 f"· 스킬 {b['skill']:+.4f}")
        L.append(f"    → {'기준선보다 낫다' if b['better_than_baseline'] else '기준선보다 낫지 않다'}")
    L.append("")

    m = g["magnitude"]
    L.append("[6] 예측 폭 — 모델이 과감한가")
    L.append(f"    평균 예측 {m['mean_pred_pct']:+.2f}% vs 평균 실제 {m['mean_actual_pct']:+.2f}%")
    L.append(f"    절대값 평균: 예측 {m['pred_abs_mean']:.2f}% / 실제 {m['actual_abs_mean']:.2f}%")
    L.append(f"    MAE {m['mae_pct']:.2f}%p · 순위상관 {m['corr']}")
    L.append("")

    cp = g.get("confidence_proxy") or {}
    if not cp.get("insufficient"):
        L.append("[7] 확신도는 무엇을 재고 있나")
        L.append(f"    확신도 vs 실현변동성  : {cp['vs_realized_vol']:+.4f}")
        L.append(f"    확신도 vs 실제 변동폭 : {cp['vs_actual_abs_move']:+.4f}")
        L.append(f"    확신도 vs 예측 크기   : {cp['vs_pred_abs_size']:+.4f}")
        L.append(f"    → {cp['note']}")
        L.append("")

    L.append("[8] 종목별 적중률")
    for p in g["by_ticker"]:
        mark = "" if p["enough"] else "  (표본 부족)"
        L.append(f"    {p['ticker']:<6}{p['n']:>4}건  {_pct(p['hit_rate']):>7}  "
                 f"예측 {p['mean_pred_pct']:+6.2f}% / 실제 {p['mean_actual_pct']:+6.2f}%{mark}")
    L.append("")
    L.append("-" * 68)
    L.append(f"결론: {g['verdict']}")
    L.append("-" * 68)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Kronos 견해 적중률 성적표")
    ap.add_argument("--horizon", type=int, default=None,
                    help=f"예측 지평 거래일 (기본 config.PRED_LEN={config.PRED_LEN})")
    ap.add_argument("--min-samples", type=int, default=validation.MIN_SAMPLES,
                    help="이보다 적으면 지표를 내지 않는다")
    ap.add_argument("--cache", default=None, help="추론 캐시 경로")
    ap.add_argument("--json", default=None, help="성적표 JSON 저장 경로")
    ap.add_argument("--save-state", action="store_true",
                    help="state/scorecard.json 으로 저장 (대시보드가 읽는다)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    g = validation.report(cache_path=Path(args.cache) if args.cache else None,
                          horizon=args.horizon, min_samples=args.min_samples)
    print(render(g))

    if args.json:
        Path(args.json).write_bytes(
            json.dumps(g, ensure_ascii=False, indent=2).encode("utf-8"))
        print(f"\n저장: {args.json}")
    if args.save_state:
        from core import storage
        p = config.STATE_DIR / "scorecard.json"
        storage.write_json(p, g)
        print(f"\n저장: {p}")


if __name__ == "__main__":
    main()
