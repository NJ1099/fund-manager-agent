"""
대조군 스코어러 — 예측이 실제로 기여하는지 재기 위한 것.

■ 왜 필요한가

`core/validation.py` 로 채점했더니 Kronos 견해의 IC 가 -0.003(t=-0.08)이었다.
종목 간 순위에 정보가 없다는 뜻이다. 그렇다면 백테스트가 내는 성과는 어디서 온 것인가?
남는 후보는 **최적화기(skfolio BL + CVaR)와 게이트**뿐이다.

이 파일은 그 질문에 답하기 위해 **견해를 갈아끼울 수 있는 가짜 스코어러**를 만든다.
`research_desk.scan(scorer=…)` 훅이 이미 있어서 파이프라인은 한 줄도 바뀌지 않는다 —
전략 로직을 복제하지 않는다는 규율(`CLAUDE.md` 백테스트 규율 ①)을 그대로 지킨다.

| 대조군 | 견해 | 답하는 질문 |
|---|---|---|
| `flat` | 전 종목 동일 | 최적화기만으로 얼마나 나오는가 |
| `random` | 시드 고정 난수 | Kronos 가 난수보다 나은가 |

**"flat 이 Kronos 와 비슷하거나 낫다"면** 이 프로젝트는 "예측 모델을 붙인 포트폴리오
최적화기"가 아니라 그냥 "포트폴리오 최적화기"다. 그 사실을 아는 것이 모델을 키우거나
지평을 늘리는 것보다 먼저다 — 아니면 **없는 알파를 튜닝하게 된다.**

■ 이 스코어러들이 지키는 것

**① 추론을 하지 않는다.** Kronos 를 부르지 않으므로 즉시 끝난다. 캐시도 쓰지 않는다.

**② 미래를 보지 않는다.** 넘겨받은 `bars` 는 이미 기준일까지 잘려 있고(`slice_bars`),
여기서는 그 마지막 구간만 읽는다. 룩어헤드 차단 테스트가 이 스코어러에도 적용된다.

**③ 재실행하면 같은 결과가 나온다.** 난수 시드를 `(ticker, asof)` 로 만든다.
전역 난수를 쓰면 같은 설정으로 두 번 돌렸을 때 성과가 달라져서, 무엇 때문에 달라졌는지
구분할 수 없게 된다.

**④ 스코어 dict 의 모양을 Kronos 와 똑같이 맞춘다.** 대시보드·이력·성적표가 같은 키를
읽기 때문이다. 다만 `synthetic: True` 를 넣어 **이 결과가 진짜 추론이 아님을 표시**한다 —
표시하지 않으면 추론 캐시나 성적표에 섞였을 때 구분할 방법이 없다.
"""
import hashlib

import numpy as np

from . import config

# flat 대조군이 쓰는 견해 크기(%). 0 으로 두면 `cycle.select_picks` 가
# "양의 견해가 부족"으로 판단해 대체 편입 경로를 타버린다 — 그러면 대조군이
# 재려던 것(선별+최적화의 정상 경로)과 다른 코드를 재게 된다.
FLAT_VIEW_PCT = 0.10

# random 대조군의 견해 표준편차(%). Kronos 의 실측 예측 폭에 맞췄다
# (성적표 [6] 예측 절대값 평균 1.11%, 표준편차는 대략 그 1.3배).
RANDOM_VIEW_STD_PCT = 1.4


def _seed(ticker, asof, salt=0):
    """(종목, 기준일, salt) → 결정론적 시드. 재실행 시 같은 값이 나와야 한다.

    `salt` 는 **난수 대조군을 여러 번 돌리기 위한 것**이다. 난수 견해로 한 번 돌린
    결과 하나는 아무것도 말해주지 않는다 — 운 좋은 시드였을 수 있다. salt 를 바꿔
    여러 번 돌리고 그 분포와 Kronos 를 비교해야 "Kronos 가 난수보다 나은가"에
    답할 수 있다.
    """
    h = hashlib.sha256(f"{ticker}@{str(asof)[:10]}#{salt}".encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def _common(ticker, bars, asof, view_pct, dispersion, up_ratio):
    """Kronos 스코어와 같은 모양의 dict 를 만든다.

    모멘텀·실현변동성은 **실제로 계산한다** — 이 값들은 예측이 아니라 과거 사실이고,
    화면과 이력이 그대로 쓴다. 견해만 갈아끼우는 것이 이 대조군의 목적이다.
    """
    lb = config.LOOKBACK
    window = bars.iloc[-lb:] if len(bars) > lb else bars
    close = window["close"]
    last = float(close.iloc[-1])

    def mom(n):
        return 0.0 if len(close) <= n else float(close.iloc[-1] / close.iloc[-1 - n] - 1.0)

    r = close.pct_change().dropna()
    vol = float(r.iloc[-20:].std() * np.sqrt(252)) if len(r) >= 20 else 0.0

    # 팬차트는 견해와 분산으로부터 만든다. 실제 경로 샘플이 아니므로 정직하게
    # 대칭 구간으로 둔다 — 없는 비대칭을 지어내면 캘리브레이션 채점이 오염된다.
    horizon = config.PRED_LEN
    steps = np.arange(1, horizon + 1) / horizon
    mid = last * (1.0 + view_pct / 100.0 * steps)
    band = last * dispersion * np.sqrt(steps) * 1.645       # 90% 구간 근사

    return {
        "ticker": ticker,
        "last_close": round(last, 2),
        "view_daily": (view_pct / 100.0) / horizon,
        "view_horizon_pct": round(float(view_pct), 3),
        "dispersion": float(dispersion),
        "up_path_ratio": round(float(up_ratio), 3),
        "fan": {
            "p05": [round(float(v), 2) for v in mid - band],
            "p50": [round(float(v), 2) for v in mid],
            "p95": [round(float(v), 2) for v in mid + band],
        },
        "mom_20d_pct": round(mom(20) * 100, 2),
        "mom_60d_pct": round(mom(60) * 100, 2),
        "realized_vol_pct": round(vol * 100, 2),
        "infer_sec": 0.0,
        "asof": (asof or window.index[-1]).strftime("%Y-%m-%d")
        if hasattr(asof or window.index[-1], "strftime") else str(asof)[:10],
        # 진짜 추론이 아님을 표시한다. 이게 없으면 캐시·성적표에 섞였을 때
        # 구분할 방법이 없다.
        "synthetic": True,
    }


def flat_scorer(view_pct=FLAT_VIEW_PCT, dispersion=0.01):
    """전 종목에 같은 견해를 준다 — '예측 없음' 대조군.

    견해가 모두 같으므로 `dispersion_to_confidence` 는 전 종목에 0.5 를 주고,
    BL 사전분포에 들어가는 상대 정보가 사라진다. 종목 선별은 워치리스트 순서가
    되고(안정 정렬), 성과는 사실상 **최적화기와 게이트만의 결과**다.

    `TOP_K` 를 워치리스트 크기로 올려서 돌리면 선별 자체를 없앨 수 있다 —
    '순수 최적화기' 성과를 보고 싶으면 그렇게 한다.
    """
    def scorer(ticker, bars, asof=None):
        return _common(ticker, bars, asof, view_pct, dispersion, up_ratio=0.5)
    return scorer


def random_scorer(std_pct=RANDOM_VIEW_STD_PCT, dispersion_lo=0.005,
                  dispersion_hi=0.03, salt=0):
    """시드 고정 난수 견해 — 'Kronos 가 난수보다 나은가' 대조군.

    견해도 분산도 무작위이므로 확신도 순위까지 무작위가 된다. Kronos 가 이것보다
    낫지 않다면, 추론에 쓰는 12~16초는 값을 못 하고 있는 것이다.
    """
    def scorer(ticker, bars, asof=None):
        stamp = asof if asof is not None else bars.index[-1]
        rng = np.random.default_rng(_seed(ticker, stamp, salt))
        view = float(rng.normal(0.0, std_pct))
        disp = float(rng.uniform(dispersion_lo, dispersion_hi))
        # 상승 경로 비율도 견해와 같은 방향으로 만들어 둔다 (성적표의 Brier 채점용)
        up = float(np.clip(0.5 + view / (4 * std_pct), 0.05, 0.95))
        return _common(ticker, bars, stamp, view, disp, up_ratio=up)
    return scorer


SCORERS = {
    "flat": flat_scorer,
    "random": random_scorer,
}


def get(name, salt=0):
    """이름으로 대조군 스코어러를 만든다. 'kronos' 는 None (기본 경로).

    salt 는 random 대조군에만 쓰인다 (여러 시드로 반복 실행하기 위한 것).
    """
    if name in (None, "", "kronos"):
        return None
    if name not in SCORERS:
        raise ValueError(f"알 수 없는 대조군: {name} (가능: {', '.join(SCORERS)}, kronos)")
    if name == "random":
        return random_scorer(salt=salt)
    return SCORERS[name]()
