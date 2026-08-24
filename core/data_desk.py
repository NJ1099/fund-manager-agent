"""데이터 데스크 — yfinance에서 OHLCV를 받아 종목별 DataFrame으로 정리한다."""
import logging

import pandas as pd
import yfinance as yf

from . import config

log = logging.getLogger("data")


def fetch_bars(tickers=None, period="3y", interval="1d"):
    """종목별 open/high/low/close/volume DataFrame 딕셔너리를 반환한다.

    결측 종목은 조용히 버리지 않고 로그를 남긴 뒤 결과에서 제외한다
    (원문 교훈: 데이터가 조용히 줄어드는 게 제일 위험하다).
    """
    tickers = tickers or config.WATCHLIST
    log.info("데이터 요청: %d종목, period=%s", len(tickers), period)

    raw = yf.download(tickers, period=period, interval=interval,
                      auto_adjust=True, progress=False, group_by="column")

    # 종목이 하나면 yfinance 가 종목 레벨 없는 평평한 컬럼을 준다.
    # 그대로 raw["Open"][tk] 로 접근하면 전부 KeyError 로 떨어져 조용히 빈 결과가 된다.
    single = not isinstance(raw.columns, pd.MultiIndex)

    out, dropped = {}, []
    for tk in tickers:
        try:
            col = (lambda name: raw[name] if single else raw[name][tk])
            df = pd.DataFrame({
                "open": col("Open"),
                "high": col("High"),
                "low": col("Low"),
                "close": col("Close"),
                "volume": col("Volume"),
            }).dropna()
        except (KeyError, TypeError):
            dropped.append(tk)
            continue

        if len(df) < config.LOOKBACK + 1:
            log.warning("%s: 봉 부족 (%d) — 제외", tk, len(df))
            dropped.append(tk)
            continue

        # 구조적 무결성 체크 (Kronos 레포의 OHLC sanity check와 같은 취지)
        bad = (df["high"] < df["low"]) | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
        if bad.any():
            log.warning("%s: 비정상 봉 %d개 제거", tk, int(bad.sum()))
            df = df[~bad]

        out[tk] = df

    if dropped:
        log.warning("제외된 종목: %s", dropped)
    log.info("확보: %d종목, 최종일 %s", len(out),
             max(df.index[-1] for df in out.values()).date() if out else "n/a")
    return out


def returns_frame(bars_by_ticker, tickers, window=None):
    """skfolio 입력용 일간 수익률 DataFrame (공통 날짜만)."""
    closes = pd.DataFrame({tk: bars_by_ticker[tk]["close"] for tk in tickers}).dropna()
    rets = closes.pct_change().dropna()
    if window:
        rets = rets.iloc[-window:]
    return rets


def latest_prices(bars_by_ticker, tickers):
    return {tk: float(bars_by_ticker[tk]["close"].iloc[-1]) for tk in tickers}
