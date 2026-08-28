"""
매크로 데스크 — 시장 지표 수집과 판독.

주식·금리·달러·원유·금속·크립토·신용을 한 화면에서 같은 방식으로 재고,
주제마다 "지금 어떤 상태인가"를 판정한다. 코멘트와 대응 문장은
`macro_brief.py` 가 이 판정을 재료로 만든다 — 여기서는 **사실만** 만든다.

■ 이 모듈이 지키는 세 가지

**① 가격과 금리를 같은 단위로 다루지 않는다.**
`^TNX`(미 10년물)의 4.67 은 가격이 아니라 연 4.67%다. 이것의 "3% 상승"은
4.67%→4.81%(+14bp)이지 값이 3% 오른 것이 아니다. 금리를 가격처럼 다루면
채권 관련 판정이 통째로 뒤집힌다(금리가 오르면 채권 가격은 떨어진다).
그래서 `kind` 로 자산을 구분하고, 금리는 변화를 **bp** 로 보고한다.

**② 없는 값을 0 으로 채우지 않는다.**
휴장·상장폐지·데이터 결손은 `None` 으로 두고 화면에 "—"로 나온다.
0 으로 채우면 "변화 없음"으로 잘못 읽힌다. 이건 보유 종목 쪽과 같은 규율이다.

**③ 판정은 문턱값을 코드에 드러내 둔다.**
"과열"·"침체" 같은 말은 문턱을 숨기면 검증할 수 없다. 모든 문턱은 이 파일
상단의 상수이고, 판정 결과에 그 근거 수치를 함께 담아 돌려준다.
"""
import logging

import numpy as np
import pandas as pd

log = logging.getLogger("macro")

# ---------------------------------------------------------------- 판정 문턱
MA_TREND_PCT = 1.0        # 200일선 대비 이만큼 벗어나야 추세로 본다 (%)
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
RANGE_HIGH_PCT = 80.0     # 52주 범위에서 이 위면 '상단'
RANGE_LOW_PCT = 20.0      # 이 아래면 '하단'
VOL_SPIKE_RATIO = 1.5     # 20일 변동성 / 1년 변동성 이 값을 넘으면 '변동성 확대'

# ---------------------------------------------------------------- 관측 유니버스
# kind: price(가격) · index(지수) · fx_rate(환율) · yield(연이율 %) · vol(변동성 지수)
# 낙폭 태그는 price·index 에만 붙인다 — 아래 _state 주석 참고.
UNIVERSE = [
    # symbol,      한글명,            주제,        kind,    비고
    ("SPY",        "S&P 500",         "stocks",   "price", "미국 대형주"),
    ("QQQ",        "나스닥 100",       "stocks",   "price", "미국 기술주"),
    ("IWM",        "러셀 2000",        "stocks",   "price", "미국 중소형주"),
    ("^KS11",      "코스피",           "stocks",   "index", "한국"),
    ("EEM",        "신흥국 주식",       "stocks",   "price", "MSCI EM"),
    ("^VIX",       "VIX 변동성지수",    "risk",     "vol",   "S&P500 30일 내재변동성"),

    ("^TNX",       "미 국채 10년",      "rates",    "yield", "장기 금리 기준"),
    ("^FVX",       "미 국채 5년",       "rates",    "yield", "중기"),
    ("^IRX",       "미 국채 13주",      "rates",    "yield", "정책금리 대용"),
    ("TLT",        "미 장기국채 ETF",   "rates",    "price", "20년+ 듀레이션"),

    ("HYG",        "하이일드 채권",     "credit",   "price", "신용위험 선호도"),
    ("LQD",        "투자등급 회사채",   "credit",   "price", "우량 크레딧"),

    ("DX-Y.NYB",   "달러인덱스",        "fx",       "index", "DXY"),
    ("KRW=X",      "원/달러",          "fx",       "fx_rate", "높을수록 원화 약세"),
    ("EURUSD=X",   "유로/달러",         "fx",       "fx_rate", ""),
    ("JPY=X",      "엔/달러",          "fx",       "fx_rate", "높을수록 엔 약세"),

    ("CL=F",       "WTI 원유",         "energy",   "price", "미국 서부텍사스유"),
    ("BZ=F",       "브렌트유",          "energy",   "price", "국제 기준유"),
    ("NG=F",       "천연가스",          "energy",   "price", "헨리허브"),
    ("XLE",        "에너지 섹터",       "energy",   "price", "미국 에너지주"),

    ("GC=F",       "금",               "metals",   "price", "온스당 달러"),
    ("SI=F",       "은",               "metals",   "price", "온스당 달러"),
    ("HG=F",       "구리",             "metals",   "price", "파운드당 달러 · 경기 민감"),
    ("PL=F",       "백금",             "metals",   "price", ""),

    ("BTC-USD",    "비트코인",          "crypto",   "price", ""),
    ("ETH-USD",    "이더리움",          "crypto",   "price", ""),
    ("SOL-USD",    "솔라나",           "crypto",   "price", ""),
]

TOPICS = {
    "stocks": "주식",
    "risk":   "위험 지표",
    "rates":  "금리 · 채권",
    "credit": "신용",
    "fx":     "달러 · 환율",
    "energy": "에너지",
    "metals": "금속",
    "crypto": "크립토",
}

# 수익률을 재는 창 (거래일). 크립토는 365일 거래라 창 길이가 달력 기준과 어긋나지만,
# 다른 자산과 같은 '봉 개수'로 재는 편이 비교에 일관적이다.
WINDOWS = [("1d", 1), ("1w", 5), ("1m", 21), ("3m", 63), ("6m", 126), ("1y", 252)]


def symbols():
    return [u[0] for u in UNIVERSE]


def meta(symbol):
    for s, name, topic, kind, note in UNIVERSE:
        if s == symbol:
            return {"symbol": s, "name": name, "topic": topic, "kind": kind, "note": note}
    return None


# ---------------------------------------------------------------- 수집
def fetch(symbols_=None, period="2y"):
    """심볼별 종가 시리즈를 받아온다. 실패한 심볼은 조용히 버리지 않고 보고한다."""
    import yfinance as yf

    syms = list(symbols_ or symbols())
    log.info("매크로 데이터 요청: %d종목", len(syms))
    raw = yf.download(syms, period=period, interval="1d",
                      auto_adjust=True, progress=False, group_by="column")

    single = not isinstance(raw.columns, pd.MultiIndex)
    out, failed = {}, []
    for s in syms:
        try:
            close = (raw["Close"] if single else raw["Close"][s]).dropna()
            if len(close) < 30:
                failed.append(s)
                continue
            out[s] = close
        except (KeyError, TypeError):
            failed.append(s)
    if failed:
        log.warning("시세를 못 받은 심볼: %s", failed)
    return out, failed


# ---------------------------------------------------------------- 지표 계산
def _pct_change(series, n):
    """n 거래일 전 대비 변화율(%). 데이터가 모자라면 None."""
    if len(series) <= n:
        return None
    prev = float(series.iloc[-1 - n])
    if prev == 0:
        return None
    return (float(series.iloc[-1]) / prev - 1.0) * 100.0


def _diff(series, n):
    """n 거래일 전 대비 절대 변화 (금리용 — %p)."""
    if len(series) <= n:
        return None
    return float(series.iloc[-1]) - float(series.iloc[-1 - n])


def _rsi(series, n=14):
    if len(series) < n + 1:
        return None
    d = series.diff().dropna()
    up = d.clip(lower=0).rolling(n).mean()
    down = (-d.clip(upper=0)).rolling(n).mean()
    if len(up.dropna()) == 0:
        return None
    last_up, last_down = float(up.iloc[-1]), float(down.iloc[-1])
    if last_down == 0:
        return 100.0
    rs = last_up / last_down
    return float(100 - 100 / (1 + rs))


def _ann_vol(series, n):
    r = series.pct_change().dropna()
    if len(r) < n:
        return None
    return float(r.iloc[-n:].std() * np.sqrt(252) * 100)


def _ytd(series):
    """올해 첫 거래일 대비. 연초 데이터가 없으면 None."""
    year = series.index[-1].year
    ytd = series[series.index.year == year]
    if len(ytd) < 2:
        return None
    base = float(ytd.iloc[0])
    return None if base == 0 else (float(series.iloc[-1]) / base - 1.0) * 100.0


def snapshot(symbol, close):
    """한 자산의 현재 상태를 재는 지표 묶음."""
    m = meta(symbol) or {"symbol": symbol, "name": symbol, "topic": "misc",
                         "kind": "price", "note": ""}
    is_yield = m["kind"] in ("yield", "vol")
    last = float(close.iloc[-1])

    # 금리·변동성지수는 변화를 %p(=bp)로, 나머지는 %로 잰다.
    changes = {}
    for label, n in WINDOWS:
        changes[label] = (_diff(close, n) if is_yield else _pct_change(close, n))
    ytd = (_diff(close, len(close[close.index.year == close.index[-1].year]) - 1)
           if is_yield else _ytd(close))

    win = close.iloc[-252:] if len(close) >= 60 else close
    hi, lo = float(win.max()), float(win.min())
    pos = None if hi <= lo else (last - lo) / (hi - lo) * 100.0
    # 고점 대비 낙폭. 52주 위치만으로는 "많이 올랐다가 크게 빠지는 중"이 안 보인다 —
    # 코스피처럼 연초 대비 +58%면서 고점 대비 −25%인 국면이 실제로 있다.
    dd = None if hi <= 0 else (last / hi - 1.0) * 100.0

    ma50 = float(close.iloc[-50:].mean()) if len(close) >= 50 else None
    ma200 = float(close.iloc[-200:].mean()) if len(close) >= 200 else None

    vol20 = _ann_vol(close, 20)
    vol252 = _ann_vol(close, 252) if len(close) >= 253 else None

    d = {
        **m,
        "last": round(last, 4 if abs(last) < 10 else 2),
        "asof": str(close.index[-1].date()),
        "bars": len(close),
        "unit": "%p" if is_yield else "%",
        "changes": {k: (None if v is None else round(v, 2)) for k, v in changes.items()},
        "ytd": None if ytd is None else round(ytd, 2),
        "range52w": {"high": round(hi, 2), "low": round(lo, 2),
                     "position_pct": None if pos is None else round(pos, 1),
                     "drawdown_pct": None if dd is None else round(dd, 1),
                     "high_date": str(win.idxmax().date()),
                     "low_date": str(win.idxmin().date())},
        "ma50": None if ma50 is None else round(ma50, 2),
        "ma200": None if ma200 is None else round(ma200, 2),
        "vs_ma50_pct": None if not ma50 else round((last / ma50 - 1) * 100, 2),
        "vs_ma200_pct": None if not ma200 else round((last / ma200 - 1) * 100, 2),
        "rsi14": None if _rsi(close) is None else round(_rsi(close), 1),
        "vol20_pct": None if vol20 is None else round(vol20, 1),
        "vol252_pct": None if vol252 is None else round(vol252, 1),
        "vol_ratio": (round(vol20 / vol252, 2)
                      if vol20 is not None and vol252 not in (None, 0) else None),
    }
    d["trend"] = _trend(d)
    d["state"] = _state(d)
    return d


def _trend(d):
    """추세 판정 — 200일선 대비 위치와 50/200 배열.

    장기 추세만 말하면 오해가 생긴다. 200일선 위에 있으면서 최근 석 달은 크게
    빠지는 국면이 실제로 있다(연초 대비 +58%, 고점 대비 −25% 같은). 그래서
    장기 추세와 3개월 모멘텀의 방향이 어긋나면 그 사실을 함께 돌려준다.
    """
    v200, ma50, ma200 = d["vs_ma200_pct"], d["ma50"], d["ma200"]
    if v200 is None:
        return {"label": "판정 불가", "conflict": False,
                "reason": "200일 이동평균을 만들 데이터가 부족합니다"}
    if v200 > MA_TREND_PCT:
        label = "상승 추세"
    elif v200 < -MA_TREND_PCT:
        label = "하락 추세"
    else:
        label = "추세 없음"
    cross = None
    if ma50 is not None and ma200 is not None:
        cross = "정배열" if ma50 > ma200 else "역배열"

    m3 = d["changes"].get("3m")
    conflict = bool(m3 is not None and label != "추세 없음"
                    and ((label == "상승 추세" and m3 < -MA_TREND_PCT)
                         or (label == "하락 추세" and m3 > MA_TREND_PCT)))
    reason = f"200일선 대비 {v200:+.1f}%" + (f" · {cross}" if cross else "")
    if conflict:
        unit = d.get("unit", "%")
        reason += f" · 다만 최근 3개월은 {m3:+.1f}{unit}로 역방향"
    return {"label": label, "cross": cross, "conflict": conflict, "reason": reason}


def _state(d):
    """과열·침체·변동성 확대 같은 '지금의 성격'. 근거 수치를 함께 담는다."""
    tags = []
    rsi, pos, vr = d["rsi14"], d["range52w"]["position_pct"], d["vol_ratio"]
    if rsi is not None:
        if rsi >= RSI_OVERBOUGHT:
            tags.append({"tag": "과열", "why": f"RSI {rsi:.0f} ≥ {RSI_OVERBOUGHT:.0f}"})
        elif rsi <= RSI_OVERSOLD:
            tags.append({"tag": "과매도", "why": f"RSI {rsi:.0f} ≤ {RSI_OVERSOLD:.0f}"})
    if pos is not None:
        if pos >= RANGE_HIGH_PCT:
            tags.append({"tag": "52주 상단", "why": f"1년 범위의 {pos:.0f}% 지점"})
        elif pos <= RANGE_LOW_PCT:
            tags.append({"tag": "52주 하단", "why": f"1년 범위의 {pos:.0f}% 지점"})
    if vr is not None and vr >= VOL_SPIKE_RATIO:
        tags.append({"tag": "변동성 확대",
                     "why": f"최근 20일 변동성이 1년 평균의 {vr:.1f}배"})
    # 고점 대비 낙폭 — "많이 올랐다"와 "지금 빠지는 중"은 동시에 참일 수 있다.
    #
    # 단, **가격 자산에만 붙인다.** VIX 가 고점 대비 −53%인 것은 '약세장'이 아니라
    # '시장이 진정됐다'는 뜻이고, 금리가 고점 대비 낮은 것도 손실이 아니다.
    # 이 구분을 빼먹으면 요약에 "VIX 약세장" 같은 문장이 그대로 올라간다.
    if d["kind"] in ("price", "index"):
        dd = d["range52w"].get("drawdown_pct")
        if dd is not None:
            if dd <= -20.0:
                tags.append({"tag": "약세장", "why": f"52주 고점 대비 {dd:.1f}%"})
            elif dd <= -10.0:
                tags.append({"tag": "조정", "why": f"52주 고점 대비 {dd:.1f}%"})
    return tags


# ---------------------------------------------------------------- 교차 신호
# 개별 자산만 봐서는 안 보이고, 둘의 **관계**에서만 보이는 것들.
def cross_signals(snaps, closes):
    """자산 간 비율로 읽는 국면 신호."""
    out = []

    def ratio_signal(key, label, a, b, up_means, down_means, note):
        if a not in closes or b not in closes:
            return
        s = (closes[a] / closes[b]).dropna()
        if len(s) < 70:
            return
        chg_3m = _pct_change(s, 63)
        chg_1m = _pct_change(s, 21)
        if chg_3m is None:
            return
        direction = "상승" if chg_3m > 0 else "하락"
        out.append({
            "key": key, "label": label, "pair": f"{a}/{b}",
            "value": round(float(s.iloc[-1]), 4),
            "chg_1m_pct": None if chg_1m is None else round(chg_1m, 1),
            "chg_3m_pct": round(chg_3m, 1),
            "direction": direction,
            "reading": up_means if chg_3m > 0 else down_means,
            "note": note,
        })

    ratio_signal("copper_gold", "구리/금 비율", "HG=F", "GC=F",
                 "경기 기대가 안전자산 선호보다 강해지는 쪽",
                 "안전자산 선호가 경기 기대를 앞서는 쪽",
                 "구리는 실물 경기, 금은 불안을 반영한다. 이 비율은 둘 중 어느 쪽이 "
                 "이기고 있는지를 보여주고, 보통 장기금리와 같은 방향으로 움직인다.")

    ratio_signal("hyg_lqd", "하이일드/투자등급", "HYG", "LQD",
                 "신용 위험을 감수하려는 쪽 (위험 선호)",
                 "신용 위험을 피하려는 쪽 (위험 회피)",
                 "주식보다 먼저 움직이는 경우가 많아 조기 경보로 쓰인다.")

    ratio_signal("stock_bond", "주식/장기국채", "SPY", "TLT",
                 "위험자산 선호",
                 "안전자산 선호",
                 "리스크온·오프의 가장 단순한 척도.")

    ratio_signal("silver_gold", "은/금 비율", "SI=F", "GC=F",
                 "산업 수요가 안전자산 수요를 앞서는 쪽",
                 "안전자산 수요가 산업 수요를 앞서는 쪽",
                 "은은 절반이 산업용이라, 금 대비 강세는 경기 회복 신호로 읽힌다.")

    ratio_signal("eth_btc", "이더/비트", "ETH-USD", "BTC-USD",
                 "크립토 안에서 위험 선호가 살아나는 쪽",
                 "비트코인으로 자금이 모이는 쪽 (방어적)",
                 "알트코인 위험 선호의 대용 지표.")

    # 장단기 금리차 — 침체 예고 지표로 가장 널리 쓰인다.
    if "^TNX" in closes and "^IRX" in closes:
        sp = (closes["^TNX"] - closes["^IRX"]).dropna()
        if len(sp) > 63:
            now = float(sp.iloc[-1])
            out.append({
                "key": "yield_curve", "label": "장단기 금리차 (10년 − 13주)",
                "pair": "^TNX-^IRX",
                "value": round(now, 2), "unit": "%p",
                "chg_1m_pct": round(float(sp.iloc[-1] - sp.iloc[-22]), 2),
                "chg_3m_pct": round(float(sp.iloc[-1] - sp.iloc[-64]), 2),
                "direction": "확대" if now > float(sp.iloc[-64]) else "축소",
                "reading": ("역전 상태 — 역사적으로 침체에 선행했다" if now < 0
                            else "정상 (장기금리가 단기보다 높다)"),
                "note": "곡선이 역전됐다가 다시 가팔라지는 국면이 과거 침체 직전에 "
                        "반복적으로 나타났다. 수치 자체보다 방향 전환이 신호다.",
                "inverted": bool(now < 0),
            })
    return out


# ---------------------------------------------------------------- 진입점
def collect(period="2y"):
    """전 유니버스를 받아 지표·교차신호를 계산한다."""
    closes, failed = fetch(period=period)
    snaps = {}
    for s, c in closes.items():
        try:
            snaps[s] = snapshot(s, c)
        except Exception as e:                      # 한 자산 실패가 전체를 죽이지 않게
            log.warning("%s 지표 계산 실패: %s", s, e)
            failed.append(s)

    by_topic = {}
    for s, d in snaps.items():
        by_topic.setdefault(d["topic"], []).append(d)
    for t in by_topic:
        by_topic[t].sort(key=lambda d: symbols().index(d["symbol"]))

    asof = max((d["asof"] for d in snaps.values()), default=None)
    return {
        "asof": asof,
        "count": len(snaps),
        "failed": failed,
        "topics": {k: v for k, v in TOPICS.items() if k in by_topic},
        "by_topic": by_topic,
        "assets": snaps,
        "cross": cross_signals(snaps, closes),
    }
