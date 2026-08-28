"""
견해 적중률 추적 — 모델 성적표.

■ 왜 이 파일이 있나

이 시스템 전체가 하나의 가정 위에 서 있다: **"확신도가 높으면 더 크게 베팅해도 된다."**
`research_desk.dispersion_to_confidence` 가 확신도를 매기고, 그 확신도가 주목도
순위를 정하고, 순위가 편입 종목을 정한다. 그런데 그 가정이 맞는지 확인하는 코드가
지금까지 없었다.

원문 저자의 독립 검증에서 Kronos 는 무작위 걷기와 사실상 구분되지 않았다
(Brier 0.189 vs 0.188). 우리 데이터에서도 같은지 봐야 한다. **같다는 결론이 나와도
그것은 실패가 아니다** — 확신도가 베팅 크기의 근거가 못 된다는 사실을 아는 것이,
근거 없이 베팅 크기를 키우는 것보다 낫다.

■ 표본은 어디서 오나 (추론 비용 0)

라이브로는 표본 하나 쌓는 데 5거래일이 걸린다. 그런데 `state/infer_cache.jsonl` 에
이미 (종목 × 과거 날짜) 예측이 들어 있다 — 백테스트가 채워둔 것이다. 각 예측의
기준일에서 `PRED_LEN` 거래일 뒤 실제 종가와 대조하면 **추론을 한 번도 하지 않고**
수백 개 표본이 나온다.

■ 무엇을 재나

| 지표 | 답하는 질문 |
|---|---|
| 방향 적중률 | 상승/하락 방향이나 맞히는가 (기준선: 실제 상승 비율) |
| IC (순위상관) | 종목 간 **상대 순위**를 맞히는가 — 포트폴리오에는 이쪽이 더 중요 |
| 확신도 구간별 적중률 | **확신도 상위 구간이 하위 구간보다 실제로 더 맞는가** ← 핵심 |
| 팬차트 커버리지 | 실제 가격이 5–95 구간에 90% 들어오는가 (불확실성 추정의 정직성) |
| Brier / 스킬 점수 | 상승 확률 예측이 "늘 base rate" 보다 나은가 |

세 번째가 이 파일의 존재 이유다. 확신도와 적중률에 관계가 없다면
`dispersion_to_confidence` 는 베팅 크기를 정할 근거가 못 된다.

■ 규율

- **표본이 적으면 숫자를 내지 않는다.** 20개 표본의 적중률 60%는 정보가 아니라 잡음이다.
  `MIN_SAMPLES` 미만이면 값 대신 `insufficient` 를 돌려준다. 화면도 그대로 표시한다.
- **아직 결과가 나오지 않은 예측은 조용히 버리지 않는다.** `pending` 으로 세어서 보고한다.
  조용히 빼면 "표본이 왜 이것밖에 없지"를 영영 알 수 없다.
- **확신도는 저장된 값을 쓰지 않고 다시 계산한다.** 확신도는 그날 스캔한 종목들 사이의
  상대 정규화라서, 그날 함께 스캔된 종목 집합이 있어야 의미가 같아진다.
"""
import json
import logging
from collections import defaultdict

import numpy as np

from . import config, research_desk

log = logging.getLogger("validation")

# 이보다 적으면 지표를 내지 않는다. 방향 적중률의 표준오차가 표본 30개에서 ±9%p라
# 그 아래로는 어떤 값이 나와도 "동전 던지기와 다르다"고 말할 수 없다.
MIN_SAMPLES = 30

# 확신도 구간 — dispersion_to_confidence 의 출력 범위(0.10~0.90)를 셋으로 자른다.
CONF_BINS = [(0.10, 0.45, "낮음"), (0.45, 0.70, "중간"), (0.70, 0.91, "높음")]


# ----------------------------------------------------------------- 예측 읽기
def load_predictions(cache_path=None, fingerprint=None):
    """추론 캐시에서 (기준일 → [스코어]) 를 읽는다.

    지문이 다른 항목은 다른 추론 설정으로 만든 것이라 섞으면 안 된다.
    기본값은 현재 설정의 지문이다.
    """
    from .infer_cache import DEFAULT_PATH, fingerprint as current_fp

    path = cache_path or DEFAULT_PATH
    fp = fingerprint or current_fp()
    by_date = defaultdict(list)
    stale = 0

    if not path.exists():
        return {}, {"stale": 0, "total": 0, "path": str(path)}

    total = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if rec.get("fp") != fp:
                stale += 1
                continue
            score = rec["score"]
            asof = score.get("asof") or rec["key"].split("@")[-1]
            by_date[asof].append(dict(score))

    return dict(by_date), {"stale": stale, "total": total, "path": str(path)}


# ----------------------------------------------------------------- 실현
def _forward_return(bars, asof, horizon):
    """기준일 종가 대비 horizon 거래일 뒤 종가의 수익률과 그 종가.

    아직 미래가 오지 않았으면 (None, None). 기준일이 봉에 없으면 그 이전
    마지막 거래일을 쓴다(휴장일에 스캔한 기록이 있을 수 있다).
    """
    idx = bars.index
    stamp = np.datetime64(str(asof)[:10])
    pos = int(np.searchsorted(idx.values, stamp, side="right")) - 1
    if pos < 0:
        return None, None, None
    target = pos + horizon
    if target >= len(idx):
        return None, None, None          # 아직 결과가 없는 예측
    base = float(bars["close"].iloc[pos])
    fwd = float(bars["close"].iloc[target])
    if base <= 0:
        return None, None, None
    return fwd / base - 1.0, base, fwd


def realize(by_date, bars_by_ticker, horizon=None):
    """예측마다 실제 결과를 붙인다.

    확신도는 저장값이 아니라 **그날 함께 스캔된 종목 집합으로 다시 계산**한다.
    확신도가 종목 간 상대 정규화이기 때문이다 — 다른 날의 종목과 섞으면 의미가 달라진다.
    """
    horizon = config.PRED_LEN if horizon is None else horizon
    rows, pending, missing = [], 0, 0

    for asof in sorted(by_date):
        day = [dict(s) for s in by_date[asof] if s.get("ticker") in bars_by_ticker]
        missing += len(by_date[asof]) - len(day)
        if not day:
            continue

        # 그날의 종목 집합으로 확신도 재계산 (원 스캔과 같은 함수)
        research_desk.dispersion_to_confidence(day)

        for s in day:
            bars = bars_by_ticker[s["ticker"]]
            actual, base, fwd = _forward_return(bars, asof, horizon)
            if actual is None:
                pending += 1
                continue
            pred_pct = float(s["view_horizon_pct"])
            rows.append({
                "asof": asof,
                "ticker": s["ticker"],
                "pred_pct": pred_pct,
                "actual_pct": actual * 100.0,
                "confidence": float(s["confidence"]),
                "dispersion": float(s.get("dispersion", 0.0)),
                "up_path_ratio": float(s.get("up_path_ratio", 0.5)),
                "realized_vol_pct": float(s.get("realized_vol_pct", 0.0)),
                "base_close": base,
                "actual_close": fwd,
                # 팬차트의 마지막 스텝 = horizon 시점의 5·95 분위
                "p05": (s.get("fan") or {}).get("p05", [None])[-1],
                "p95": (s.get("fan") or {}).get("p95", [None])[-1],
                "hit": (pred_pct > 0) == (actual > 0),
            })

    return rows, {"pending": pending, "no_price": missing, "horizon": horizon}


# ----------------------------------------------------------------- 통계 도구
def _rank(a):
    """평균 순위(동점은 평균) — scipy 없이 쓰는 rankdata."""
    a = np.asarray(a, dtype=float)
    order = a.argsort()
    ranks = np.empty(len(a), dtype=float)
    ranks[order] = np.arange(1, len(a) + 1, dtype=float)
    # 동점 처리: 같은 값끼리 순위를 평균으로 바꾼다
    uniq, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    if (counts > 1).any():
        sums = np.zeros(len(uniq))
        np.add.at(sums, inv, ranks)
        ranks = (sums / counts)[inv]
    return ranks


def _spearman(x, y):
    """순위상관. 표본이 3 미만이거나 한쪽이 상수면 None."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if len(x) < 3:
        return None
    rx, ry = _rank(x), _rank(y)
    sx, sy = rx.std(), ry.std()
    if sx < 1e-12 or sy < 1e-12:
        return None
    return float(((rx - rx.mean()) * (ry - ry.mean())).mean() / (sx * sy))


def _wilson(hits, n, z=1.96):
    """이항 비율의 신뢰구간(Wilson). 정규근사는 작은 표본에서 구간이 0 밖으로 나간다."""
    if n == 0:
        return None, None
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return float((c - m) / d), float((c + m) / d)


# ----------------------------------------------------------------- 채점
def grade(rows, min_samples=MIN_SAMPLES):
    """실현된 예측 목록에서 성적표를 만든다.

    표본이 부족한 항목은 값을 만들지 않고 그 사실을 돌려준다 —
    20개 표본의 적중률 60%는 정보가 아니라 잡음이다.
    """
    n = len(rows)
    out = {
        "n": n,
        "min_samples": min_samples,
        "sufficient": n >= min_samples,
        "tickers": sorted({r["ticker"] for r in rows}),
        "period": [rows[0]["asof"], rows[-1]["asof"]] if rows else None,
    }
    if n == 0:
        out["note"] = "대조할 예측이 없습니다."
        return out

    pred = np.array([r["pred_pct"] for r in rows])
    actual = np.array([r["actual_pct"] for r in rows])
    hits = np.array([r["hit"] for r in rows], dtype=bool)

    # --- 1) 방향 적중률 -------------------------------------------------
    # 기준선은 50%가 아니라 **실제 상승 비율**이다. 상승장에서는 "늘 상승"이라고
    # 찍기만 해도 70%가 나온다. 모델이 그걸 넘었는지가 질문이다.
    base_rate = float((actual > 0).mean())
    always_up = float((pred > 0).mean())
    lo, hi = _wilson(int(hits.sum()), n)
    out["direction"] = {
        "n": n,
        "hits": int(hits.sum()),
        "rate": round(float(hits.mean()), 4),
        "ci95": [round(lo, 4), round(hi, 4)],
        "base_rate_up": round(base_rate, 4),
        # 늘 "상승"으로 찍었을 때의 적중률 = 실제 상승 비율
        "naive_always_up": round(base_rate, 4),
        "pred_up_share": round(always_up, 4),
        "beats_naive": bool(hits.mean() > max(base_rate, 1 - base_rate)),
    } if n >= min_samples else {"insufficient": True, "n": n}

    # --- 2) IC (날짜별 순위상관) ---------------------------------------
    # 포트폴리오는 "이 종목이 오를까"가 아니라 "어느 종목이 더 오를까"로 만들어진다.
    # 그래서 절대 예측보다 종목 간 순위가 맞는지가 중요하다.
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["asof"]].append(r)
    ics = []
    for asof, day in sorted(by_day.items()):
        if len(day) < 3:
            continue
        ic = _spearman([d["pred_pct"] for d in day], [d["actual_pct"] for d in day])
        if ic is not None:
            ics.append({"asof": asof, "ic": round(ic, 4), "n": len(day)})
    if len(ics) >= 5:
        vals = np.array([i["ic"] for i in ics])
        sd = float(vals.std(ddof=1)) if len(vals) > 1 else 0.0
        # IR = 평균 IC / IC 표준편차. t = IR × √기간수
        out["ic"] = {
            "periods": len(ics),
            "mean": round(float(vals.mean()), 4),
            "std": round(sd, 4),
            "ir": round(float(vals.mean() / sd), 3) if sd > 1e-12 else None,
            "t_stat": round(float(vals.mean() / sd * np.sqrt(len(vals))), 2) if sd > 1e-12 else None,
            "positive_share": round(float((vals > 0).mean()), 3),
            "series": ics,
        }
    else:
        out["ic"] = {"insufficient": True, "periods": len(ics)}

    # --- 3) 확신도 구간별 적중률 (이 파일의 핵심) ------------------------
    bins = []
    for lo_c, hi_c, label in CONF_BINS:
        sel = [r for r in rows if lo_c <= r["confidence"] < hi_c]
        if not sel:
            continue
        h = np.array([r["hit"] for r in sel], dtype=bool)
        a = np.array([r["actual_pct"] for r in sel])
        wl, wh = _wilson(int(h.sum()), len(sel))
        bins.append({
            "label": label,
            "range": [lo_c, round(hi_c, 2)],
            "n": len(sel),
            "hit_rate": round(float(h.mean()), 4),
            "ci95": [round(wl, 4), round(wh, 4)],
            "mean_actual_pct": round(float(a.mean()), 3),
            "enough": len(sel) >= min_samples,
        })
    out["by_confidence"] = {
        "bins": bins,
        # 확신도와 적중의 상관 — 이게 0 근처면 확신도는 베팅 크기의 근거가 못 된다
        "conf_vs_hit_corr": (round(_spearman([r["confidence"] for r in rows],
                                              [1.0 if r["hit"] else 0.0 for r in rows]) or 0.0, 4)
                             if n >= min_samples else None),
        "monotonic": _is_monotonic([b for b in bins if b["enough"]]),
    }

    # --- 4) 팬차트 커버리지 ---------------------------------------------
    # p05~p95 는 90%를 담아야 정직한 구간이다. 훨씬 낮으면 모델이 불확실성을
    # 과소평가하는 것이고(구간이 너무 좁다), 훨씬 높으면 구간이 무의미하게 넓다.
    cov = [r for r in rows if r["p05"] is not None and r["p95"] is not None]
    if len(cov) >= min_samples:
        inside = [r for r in cov if r["p05"] <= r["actual_close"] <= r["p95"]]
        below = sum(1 for r in cov if r["actual_close"] < r["p05"])
        width = np.array([(r["p95"] - r["p05"]) / r["base_close"] * 100 for r in cov])
        out["calibration"] = {
            "n": len(cov),
            "coverage": round(len(inside) / len(cov), 4),
            "target": 0.90,
            "below_p05": below,
            "above_p95": len(cov) - len(inside) - below,
            "mean_width_pct": round(float(width.mean()), 2),
            "verdict": _coverage_verdict(len(inside) / len(cov)),
        }
    else:
        out["calibration"] = {"insufficient": True, "n": len(cov)}

    # --- 5) Brier / 스킬 점수 -------------------------------------------
    # up_path_ratio(상승 경로 비율)를 상승 확률로 읽는다. 기준선은 "늘 base rate로
    # 찍기". 스킬 점수가 0 이하면 확률 예측이 상수 예측보다 나을 것이 없다.
    if n >= min_samples:
        p = np.array([r["up_path_ratio"] for r in rows])
        y = (actual > 0).astype(float)
        brier = float(np.mean((p - y) ** 2))
        ref = float(np.mean((base_rate - y) ** 2))
        out["brier"] = {
            "n": n,
            "score": round(brier, 4),
            "baseline": round(ref, 4),
            "skill": round((ref - brier) / ref, 4) if ref > 1e-12 else None,
            "better_than_baseline": bool(brier < ref),
        }
    else:
        out["brier"] = {"insufficient": True, "n": n}

    # --- 6) 예측 크기의 편향 --------------------------------------------
    # 예측 폭이 실제 변동폭보다 크면 모델이 과감한 것이고, 그 자체가
    # 견해를 그대로 베팅 크기로 옮기면 안 되는 이유가 된다.
    out["magnitude"] = {
        "mean_pred_pct": round(float(pred.mean()), 3),
        "mean_actual_pct": round(float(actual.mean()), 3),
        "pred_abs_mean": round(float(np.abs(pred).mean()), 3),
        "actual_abs_mean": round(float(np.abs(actual).mean()), 3),
        "mae_pct": round(float(np.abs(pred - actual).mean()), 3),
        "corr": round(_spearman(pred, actual) or 0.0, 4) if n >= min_samples else None,
    }

    # --- 7) 종목별 -------------------------------------------------------
    per = []
    for tk in out["tickers"]:
        sel = [r for r in rows if r["ticker"] == tk]
        h = np.array([r["hit"] for r in sel], dtype=bool)
        per.append({
            "ticker": tk,
            "n": len(sel),
            "hit_rate": round(float(h.mean()), 4),
            "mean_pred_pct": round(float(np.mean([r["pred_pct"] for r in sel])), 3),
            "mean_actual_pct": round(float(np.mean([r["actual_pct"] for r in sel])), 3),
            "enough": len(sel) >= min_samples,
        })
    out["by_ticker"] = sorted(per, key=lambda d: -d["hit_rate"])

    # --- 8) 확신도는 대체 무엇을 재고 있나 -------------------------------
    # 확신도가 적중률을 못 예측한다면, 그것이 실제로 무엇과 연동돼 있는지가
    # 다음 질문이다. `dispersion_to_confidence` 는 경로 분산의 역순 정규화인데,
    # 경로 분산은 그 종목의 **변동성**을 거의 그대로 따라간다. 그렇다면 확신도는
    # "이 예측이 믿을 만하다"가 아니라 "이 종목이 조용하다"를 재는 것이 된다.
    # 조용한 종목은 방향이 애매해서 방향 맞히기가 오히려 어렵다 — 관측된 역전의
    # 가장 그럴듯한 설명이다. 상관을 실제로 재서 확인한다.
    if n >= min_samples:
        conf = [r["confidence"] for r in rows]
        vol = [r["realized_vol_pct"] for r in rows]
        c_vol = _spearman(conf, vol) if any(v > 0 for v in vol) else None
        c_move = _spearman(conf, [abs(r["actual_pct"]) for r in rows])
        c_size = _spearman(conf, [abs(r["pred_pct"]) for r in rows])
        out["confidence_proxy"] = {
            "vs_realized_vol": round(c_vol, 4) if c_vol is not None else None,
            "vs_actual_abs_move": round(c_move, 4) if c_move is not None else None,
            "vs_pred_abs_size": round(c_size, 4) if c_size is not None else None,
            "is_vol_proxy": bool(c_vol is not None and c_vol < -0.5),
            "note": ("확신도가 저변동성의 대리 변수다 — 예측 신뢰도가 아니라 "
                     "'이 종목이 조용하다'를 재고 있다"
                     if (c_vol is not None and c_vol < -0.5) else
                     "확신도와 변동성의 연동은 강하지 않다"),
        }
    else:
        out["confidence_proxy"] = {"insufficient": True}

    out["verdict"] = _verdict(out)
    return out


def _is_monotonic(bins):
    """확신도 구간이 올라갈수록 적중률도 올라가는가. 구간이 2개 미만이면 판정 보류."""
    if len(bins) < 2:
        return None
    rates = [b["hit_rate"] for b in sorted(bins, key=lambda b: b["range"][0])]
    return all(b >= a for a, b in zip(rates, rates[1:]))


def _coverage_verdict(cov):
    if cov < 0.70:
        return "구간이 너무 좁다 — 모델이 불확실성을 과소평가한다"
    if cov < 0.85:
        return "구간이 다소 좁다"
    if cov <= 0.95:
        return "정직한 구간"
    return "구간이 너무 넓다 — 정보량이 적다"


def _verdict(g):
    """성적표 한 줄 결론. 애매하면 애매하다고 말한다."""
    if not g.get("sufficient"):
        return f"표본 부족 ({g['n']}/{g['min_samples']}) — 판정하지 않는다."

    parts = []
    d = g.get("direction") or {}
    if not d.get("insufficient"):
        parts.append(
            f"방향 적중 {d['rate']:.1%} (늘 상승으로 찍었을 때 {d['naive_always_up']:.1%})"
            + ("— 기준선을 넘었다" if d["beats_naive"] else " — 기준선을 넘지 못했다")
        )
    ic = g.get("ic") or {}
    if not ic.get("insufficient") and ic.get("t_stat") is not None:
        sig = abs(ic["t_stat"]) >= 2.0
        parts.append(f"IC 평균 {ic['mean']:+.3f} (t={ic['t_stat']:+.2f})"
                     + (" — 통계적으로 유의" if sig else " — 유의하지 않음"))
    bc = g.get("by_confidence") or {}
    if bc.get("conf_vs_hit_corr") is not None:
        c = bc["conf_vs_hit_corr"]
        if abs(c) < 0.05:
            parts.append("확신도와 적중률에 관계가 보이지 않는다 — 확신도를 베팅 크기의 근거로 쓰기 어렵다")
        elif c > 0:
            parts.append(f"확신도가 높을수록 더 맞는 경향 (상관 {c:+.3f})")
        else:
            parts.append(f"확신도가 높을수록 덜 맞는 역전 (상관 {c:+.3f}) — 확신도 정의를 다시 봐야 한다")
    return " · ".join(parts)


# ----------------------------------------------------------------- 진입점
def report(bars_by_ticker=None, cache_path=None, horizon=None, min_samples=MIN_SAMPLES):
    """캐시를 읽어 성적표를 만든다. bars 를 주지 않으면 yfinance 로 받아온다."""
    by_date, meta = load_predictions(cache_path)
    if not by_date:
        return {"n": 0, "sufficient": False,
                "note": "추론 캐시가 비어 있습니다. 백테스트를 한 번 돌리면 표본이 생깁니다.",
                "cache": meta}

    tickers = sorted({s["ticker"] for day in by_date.values() for s in day})
    if bars_by_ticker is None:
        from . import data_desk
        bars_by_ticker = data_desk.fetch_bars(tickers, period="3y")

    rows, rmeta = realize(by_date, bars_by_ticker, horizon=horizon)
    g = grade(rows, min_samples=min_samples)
    g["cache"] = meta
    g["realize"] = rmeta
    g["dates"] = len(by_date)
    return g
