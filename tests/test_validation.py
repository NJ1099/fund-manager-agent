"""
모델 성적표 테스트 — 네트워크·모델을 쓰지 않는다.

여기서 고정하는 것은 **채점이 정직한가**이다. 성적표가 좋게 나오게 만드는 실수는
전부 조용하다: 미래를 미리 보거나, 표본이 적은데 숫자를 내거나, 결과가 안 나온
예측을 슬쩍 빼거나. 그래서 그 세 가지를 명시적으로 고정한다.
"""
import numpy as np
import pandas as pd
import pytest

from core import validation as V


def bars(prices, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(prices))
    return pd.DataFrame({"close": prices}, index=idx)


def pred(ticker, asof, view_pct, dispersion=0.01, up=0.6, last=100.0, fan=None):
    """캐시에 들어 있는 모양의 예측 레코드."""
    return {
        "ticker": ticker, "asof": asof, "view_horizon_pct": view_pct,
        "dispersion": dispersion, "up_path_ratio": up, "last_close": last,
        "realized_vol_pct": 15.0,
        "fan": fan or {"p05": [last * 0.97] * 5, "p50": [last] * 5,
                       "p95": [last * 1.03] * 5},
    }


# ------------------------------------------------------- 실현: 미래를 어떻게 잡나
def test_기준일_이후_지평만큼_뒤의_종가와_대조한다():
    # 100 에서 시작해 매일 1씩 오른다. 5거래일 뒤는 105 → +5%
    px = bars(list(np.arange(100.0, 120.0)))
    rows, meta = V.realize({"2026-01-01": [pred("AAA", "2026-01-01", 3.0, last=100.0)]},
                            {"AAA": px}, horizon=5)
    assert len(rows) == 1
    assert rows[0]["actual_pct"] == pytest.approx(5.0, abs=1e-6)
    assert rows[0]["hit"] is True          # 예측 +3%, 실제 +5% → 방향 일치


def test_아직_결과가_없는_예측은_버리지_않고_pending_으로_센다():
    """조용히 빼면 '표본이 왜 이것밖에 없지'를 영영 알 수 없다."""
    px = bars([100.0, 101.0, 102.0])       # 3봉뿐 — 5거래일 뒤가 없다
    rows, meta = V.realize({"2026-01-01": [pred("AAA", "2026-01-01", 1.0)]},
                            {"AAA": px}, horizon=5)
    assert rows == []
    assert meta["pending"] == 1


def test_가격이_없는_종목은_no_price_로_보고한다():
    px = bars(list(np.arange(100.0, 120.0)))
    rows, meta = V.realize({"2026-01-01": [pred("ZZZ", "2026-01-01", 1.0)]},
                            {"AAA": px}, horizon=5)
    assert rows == []
    assert meta["no_price"] == 1


def test_확신도는_저장값이_아니라_그날_종목집합으로_다시_계산한다():
    """확신도는 종목 간 상대 정규화라, 그날 함께 스캔된 집합이 있어야 뜻이 같다."""
    px = bars(list(np.arange(100.0, 130.0)))
    day = [pred("AAA", "2026-01-01", 1.0, dispersion=0.001),   # 분산 최소 → 확신도 최고
           pred("BBB", "2026-01-01", 1.0, dispersion=0.050)]   # 분산 최대 → 확신도 최저
    for p in day:
        p["confidence"] = 0.5             # 저장돼 있던 값 (무시돼야 한다)
    rows, _ = V.realize({"2026-01-01": day}, {"AAA": px, "BBB": px}, horizon=5)
    conf = {r["ticker"]: r["confidence"] for r in rows}
    assert conf["AAA"] > conf["BBB"]
    assert conf["AAA"] != 0.5


# ------------------------------------------------------------------ 채점 규율
def test_표본이_적으면_지표를_내지_않는다():
    """20개 표본의 적중률 60%는 정보가 아니라 잡음이다."""
    rows = [{"asof": "2026-01-01", "ticker": "A", "pred_pct": 1.0, "actual_pct": 1.0,
             "confidence": 0.5, "dispersion": 0.01, "up_path_ratio": 0.6,
             "realized_vol_pct": 10.0, "base_close": 100.0, "actual_close": 101.0,
             "p05": 97.0, "p95": 103.0, "hit": True} for _ in range(10)]
    g = V.grade(rows, min_samples=30)
    assert g["sufficient"] is False
    assert g["direction"].get("insufficient") is True
    assert "표본 부족" in g["verdict"]


def test_방향_적중률의_기준선은_50퍼센트가_아니라_실제_상승비율이다():
    """상승장에서는 '늘 상승'으로 찍기만 해도 높은 적중률이 나온다."""
    rows = []
    for i in range(40):
        actual = 2.0 if i < 32 else -2.0        # 80%가 상승
        rows.append({"asof": f"2026-01-{i % 20 + 1:02d}", "ticker": f"T{i}",
                     "pred_pct": 1.0, "actual_pct": actual, "confidence": 0.5,
                     "dispersion": 0.01, "up_path_ratio": 0.6, "realized_vol_pct": 10.0,
                     "base_close": 100.0, "actual_close": 100 + actual,
                     "p05": 97.0, "p95": 103.0, "hit": actual > 0})
    g = V.grade(rows, min_samples=30)
    d = g["direction"]
    assert d["rate"] == pytest.approx(0.80)
    assert d["naive_always_up"] == pytest.approx(0.80)
    # 늘 상승으로 찍은 것과 같은 성적이므로 기준선을 '넘은' 것이 아니다
    assert d["beats_naive"] is False


def test_확신도가_높을수록_덜_맞으면_역전을_문장으로_말한다():
    """이 프로젝트의 실제 관측을 회귀로 고정한다."""
    rows = []
    for i in range(60):
        high_conf = i < 30
        hit = (i % 10) < (3 if high_conf else 8)      # 확신도 높은 쪽이 덜 맞는다
        rows.append({"asof": f"2026-01-{i % 20 + 1:02d}", "ticker": f"T{i}",
                     "pred_pct": 1.0, "actual_pct": 1.0 if hit else -1.0,
                     "confidence": 0.85 if high_conf else 0.20,
                     "dispersion": 0.01, "up_path_ratio": 0.6, "realized_vol_pct": 10.0,
                     "base_close": 100.0, "actual_close": 101.0,
                     "p05": 97.0, "p95": 103.0, "hit": hit})
    g = V.grade(rows, min_samples=30)
    assert g["by_confidence"]["conf_vs_hit_corr"] < 0
    assert g["by_confidence"]["monotonic"] is False
    assert "역전" in g["verdict"]


def test_확신도가_변동성의_대리변수인지_진단한다():
    """확신도가 예측 신뢰도가 아니라 '이 종목이 조용하다'를 재는 경우를 잡아낸다."""
    rows = []
    for i in range(40):
        conf = 0.9 - i * 0.02
        rows.append({"asof": f"2026-01-{i % 20 + 1:02d}", "ticker": f"T{i}",
                     "pred_pct": 1.0, "actual_pct": 1.0, "confidence": conf,
                     "dispersion": 0.01, "up_path_ratio": 0.6,
                     "realized_vol_pct": 5.0 + i,          # 확신도와 정확히 역방향
                     "base_close": 100.0, "actual_close": 101.0,
                     "p05": 97.0, "p95": 103.0, "hit": True})
    g = V.grade(rows, min_samples=30)
    cp = g["confidence_proxy"]
    assert cp["vs_realized_vol"] < -0.9
    assert cp["is_vol_proxy"] is True


def test_팬차트_커버리지는_구간이_좁으면_그렇게_말한다():
    rows = []
    for i in range(40):
        inside = i < 20                                # 절반만 구간 안
        actual_close = 100.0 if inside else 130.0
        rows.append({"asof": f"2026-01-{i % 20 + 1:02d}", "ticker": f"T{i}",
                     "pred_pct": 1.0, "actual_pct": 1.0, "confidence": 0.5,
                     "dispersion": 0.01, "up_path_ratio": 0.6, "realized_vol_pct": 10.0,
                     "base_close": 100.0, "actual_close": actual_close,
                     "p05": 97.0, "p95": 103.0, "hit": True})
    g = V.grade(rows, min_samples=30)
    assert g["calibration"]["coverage"] == pytest.approx(0.5)
    assert "좁다" in g["calibration"]["verdict"]


def test_ic_는_같은_날짜_안에서_종목_순위로_잰다():
    """포트폴리오는 '어느 종목이 더 오를까'로 만들어진다 — 절대 예측보다 순위가 중요하다."""
    rows = []
    for d in range(10):
        for j, (p, a) in enumerate([(3.0, 3.0), (2.0, 2.0), (1.0, 1.0), (0.0, 0.0)]):
            rows.append({"asof": f"2026-02-{d + 1:02d}", "ticker": f"T{j}",
                         "pred_pct": p, "actual_pct": a, "confidence": 0.5,
                         "dispersion": 0.01, "up_path_ratio": 0.6, "realized_vol_pct": 10.0,
                         "base_close": 100.0, "actual_close": 100 + a,
                         "p05": 97.0, "p95": 103.0, "hit": True})
    g = V.grade(rows, min_samples=30)
    assert g["ic"]["mean"] == pytest.approx(1.0)      # 순위가 완벽히 일치
    assert g["ic"]["periods"] == 10


def test_스피어만은_상수_입력에_None_을_돌려준다():
    """0 을 돌려주면 '상관이 없다'로 잘못 읽힌다 — 계산 불가와 무상관은 다르다."""
    assert V._spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None
    assert V._spearman([1, 2], [1, 2]) is None       # 표본 부족


def test_캐시_지문이_다른_예측은_섞지_않는다(tmp_path):
    """추론 설정이 다르면 같은 종목·같은 날짜라도 다른 견해다."""
    import json
    p = tmp_path / "cache.jsonl"
    lines = [
        {"fp": "설정A", "key": "AAA@2026-01-01", "score": pred("AAA", "2026-01-01", 1.0)},
        {"fp": "설정B", "key": "BBB@2026-01-01", "score": pred("BBB", "2026-01-01", 1.0)},
    ]
    p.write_bytes("\n".join(json.dumps(x, ensure_ascii=False) for x in lines).encode("utf-8"))
    by_date, meta = V.load_predictions(p, fingerprint="설정A")
    assert [s["ticker"] for s in by_date["2026-01-01"]] == ["AAA"]
    assert meta["stale"] == 1
