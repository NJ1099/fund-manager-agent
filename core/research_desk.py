"""
연구 데스크 — Kronos.

매 사이클 WATCHLIST 전 종목을 훑어서 종목마다
  - view       : 일간 기대수익 (경로 평균)
  - confidence : 확신도 (경로 표준편차가 작을수록 높음)
  - path spread: 5·95 분위 경로 (대시보드 팬차트용)
를 만들고 확신도 기준으로 순위를 매긴다.

원문의 핵심: "예측값이 아니라 확신도가 베팅 크기를 정한다."
"""
import logging
import time

import numpy as np
import pandas as pd

from . import config

# kronos_paths 는 임포트 시점에 Kronos 원본 레포의 존재를 검사한다.
# 여기서 모듈 레벨로 당겨오면 레포가 없는 환경에서는 이 모듈을 임포트하는 것만으로
# 죽는다 — 테스트·백테스트(추론 캐시 사용)·성적표 분석처럼 **추론이 필요 없는**
# 경로까지 전부 막힌다. 그래서 실제로 추론할 때만 가져온다.

log = logging.getLogger("research")

_predictor = None


def get_predictor():
    """Kronos 모델을 한 번만 로드해서 재사용한다."""
    global _predictor
    if _predictor is None:
        from model import Kronos, KronosTokenizer, KronosPredictor
        log.info("Kronos 로딩: %s", config.KRONOS_MODEL)
        t0 = time.time()
        tok = KronosTokenizer.from_pretrained(config.KRONOS_TOKENIZER)
        mdl = Kronos.from_pretrained(config.KRONOS_MODEL)
        _predictor = KronosPredictor(mdl, tok, device=config.KRONOS_DEVICE,
                                      max_context=config.KRONOS_MAX_CONTEXT)
        log.info("Kronos 로딩 완료 (%.1fs)", time.time() - t0)
    return _predictor


def _momentum(series, n):
    if len(series) <= n:
        return 0.0
    return float(series.iloc[-1] / series.iloc[-1 - n] - 1.0)


def _realized_vol(close):
    r = close.pct_change().dropna()
    if len(r) < 20:
        return 0.0
    return float(r.iloc[-20:].std() * np.sqrt(252))


def score_ticker(ticker, bars, asof=None):
    """단일 종목에 Kronos를 돌려 견해와 확신도를 뽑는다.

    bars: open/high/low/close/volume 컬럼을 가진 DatetimeIndex DataFrame
    """
    from .kronos_paths import predict_paths     # 지연 임포트 (위 주석 참고)

    predictor = get_predictor()
    lb = config.LOOKBACK

    if len(bars) < lb + 1:
        raise ValueError(f"{ticker}: 데이터 부족 ({len(bars)} < {lb + 1})")

    window = bars.iloc[-lb:]
    x_df = window[["open", "high", "low", "close", "volume"]].reset_index(drop=True)
    x_ts = pd.Series(window.index)
    y_ts = pd.Series(pd.bdate_range(window.index[-1] + pd.Timedelta(days=1),
                                     periods=config.PRED_LEN))

    t0 = time.time()
    paths = predict_paths(predictor, x_df, x_ts, y_ts,
                          pred_len=config.PRED_LEN, T=config.TEMPERATURE,
                          top_p=config.TOP_P, sample_count=config.SAMPLE_COUNT,
                          verbose=False)
    infer_sec = time.time() - t0

    last_close = float(window["close"].iloc[-1])
    terminal = paths[:, -1, 3]                      # 경로별 최종 종가
    cum_ret = terminal / last_close - 1.0

    mean_cum = float(np.mean(cum_ret))
    std_cum = float(np.std(cum_ret))
    view_daily = mean_cum / config.PRED_LEN

    # 상승 경로 비율 = 방향에 대한 경로 합의도
    up_ratio = float(np.mean(cum_ret > 0))

    # 대시보드 팬차트용: 각 스텝의 분위수 경로
    step_closes = paths[:, :, 3]                    # (sample, pred_len)
    fan = {
        "p05": [round(float(v), 2) for v in np.percentile(step_closes, 5, axis=0)],
        "p50": [round(float(v), 2) for v in np.percentile(step_closes, 50, axis=0)],
        "p95": [round(float(v), 2) for v in np.percentile(step_closes, 95, axis=0)],
    }

    return {
        "ticker": ticker,
        "last_close": round(last_close, 2),
        "view_daily": view_daily,
        "view_horizon_pct": round(mean_cum * 100, 3),
        "dispersion": std_cum,
        "up_path_ratio": round(up_ratio, 3),
        "fan": fan,
        "mom_20d_pct": round(_momentum(window["close"], 20) * 100, 2),
        "mom_60d_pct": round(_momentum(window["close"], 60) * 100, 2),
        "realized_vol_pct": round(_realized_vol(window["close"]) * 100, 2),
        "infer_sec": round(infer_sec, 2),
        "asof": (asof or window.index[-1]).strftime("%Y-%m-%d"),
    }


def dispersion_to_confidence(scores):
    """경로 분산을 0~1 확신도로 정규화한다.

    분산이 작을수록(경로가 모일수록) 확신도가 높다. 종목 간 상대 비교이므로
    watchlist 안에서의 순위 개념이지, 절대적 예측 신뢰도가 아니다.
    """
    disp = np.array([s["dispersion"] for s in scores])
    lo, hi = disp.min(), disp.max()
    for s in scores:
        if hi - lo < 1e-12:
            conf = 0.5
        else:
            conf = 0.85 - 0.7 * (s["dispersion"] - lo) / (hi - lo)
        s["confidence"] = round(float(np.clip(conf, 0.10, 0.90)), 3)
    return scores


def scan(bars_by_ticker, asof=None, scorer=None):
    """WATCHLIST 전체를 훑어 확신도 순위를 매긴 리스트를 돌려준다.

    scorer 를 주입하면 Kronos 추론을 다른 것으로 갈아낄 수 있다 —
    백테스트의 추론 캐시와 테스트의 가짜 스코어러가 이 자리를 쓴다.
    """
    scorer = scorer or score_ticker
    scores = []
    for tk, bars in bars_by_ticker.items():
        try:
            scores.append(scorer(tk, bars, asof=asof))
        except Exception as e:                       # 한 종목 실패가 사이클 전체를 죽이지 않게
            log.warning("%s 스캔 실패: %s", tk, e)

    if not scores:
        raise RuntimeError("스캔된 종목이 하나도 없습니다")

    scores = dispersion_to_confidence(scores)

    # 확신도(1순위) × 견해 크기(2순위)로 정렬 → "주목 종목"
    for s in scores:
        s["conviction"] = round(s["confidence"] * abs(s["view_horizon_pct"]), 4)
    scores.sort(key=lambda s: (-s["conviction"], -s["confidence"]))
    for i, s in enumerate(scores):
        s["rank"] = i + 1
        s["direction"] = "LONG" if s["view_horizon_pct"] > 0 else "AVOID"
    return scores
