"""
종목 판정 — "살지 말지"와 **그 이유**.

■ 왜 이런 방식인가 (오늘 배운 것의 적용)

같은 날 `core/validation.py` 로 채점했더니 Kronos 견해는 방향 적중률 51.1%,
IC −0.003 이었고, `core/baselines.py` 대조군에서는 **난수와 구분되지 않았다.**
"다음에 오를 종목을 맞힌다"는 접근이 이 프로젝트에서 실패했다는 뜻이다.

그래서 이 모듈은 **맞히려 하지 않는다.** 대신 세 가지를 한다.

1. **지금 무슨 일이 일어나고 있는지 잰다** — 추세·모멘텀·밸류에이션·수익성·재무.
   전부 과거와 현재의 사실이지 예측이 아니다.
2. **규칙으로 판정한다** — 문턱값이 전부 이 파일 상단에 드러나 있어 검증할 수 있다.
3. **찬성 근거와 반대 근거를 **함께** 낸다** — 한쪽만 보여주면 사고 싶은 이유만
   찾게 된다. 이건 UI 취향이 아니라 판단 품질의 문제다.

■ 사실과 예측을 라벨로 가른다 (`kind`)

| kind | 무엇 | 믿어도 되는가 |
|---|---|---|
| `fact` | 부채비율·ROE·200일선 이탈·고점 대비 낙폭 | **그렇다.** 계산된 사실이다 |
| `forecast` | 애널리스트 목표가·컨센서스 | **예측이다.** 낙관 편향이 크다 |

애널리스트 목표가는 이 시스템에 남은 유일한 예측이라, Kronos 와 같은 의심을 받아야
한다. 그래서 `forecast` 로 라벨하고 판정 가중치도 낮게 둔다. 버리지도 믿지도 않고
"이런 예측이 있다"고 전달만 하는 것이 정직하다.

■ 손절은 예측이 필요 없다 — 이 모듈에서 가장 확실한 부분

"오를까 내릴까"는 못 맞혀도 **"얼마나 잃었나 / 어디서 추세가 깨졌나"는 사실**이다.
그래서 손절선은 미리 규칙으로 정할 수 있고, 그 규칙은 예측력과 무관하게 작동한다.
세 가지를 각각 계산해서 **전부 보여준다** — 하나만 고르면 그 선택 자체가 숨은 가정이 된다.

■ ETF 는 펀더멘털이 없다

ETF 는 회사가 아니라 바구니라서 ROE 도 부채비율도 없다. 이걸 '결측'으로 처리해
0 으로 채우면 "부채 없는 우량 종목"이 되어버린다. **'해당 없음'으로 구분해서
가격 기반 신호만으로 판정하고, 화면에도 그 사실을 밝힌다.**
"""
import logging

import numpy as np

log = logging.getLogger("verdict")

# ---------------------------------------------------------------- 판정 문턱
# 전부 여기 드러나 있어야 검증할 수 있다. 숨기면 "왜 이 판정이 나왔나"를 답할 수 없다.
MA_TREND_PCT = 1.0          # 200일선 대비 이만큼 벗어나야 추세로 본다
DRAWDOWN_WARN = -15.0       # 고점 대비 이보다 빠지면 경고
DRAWDOWN_BEAR = -25.0       # 이보다 빠지면 강한 경고
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
REL_STRENGTH_GOOD = 5.0     # 벤치마크 대비 3개월 초과수익(%p)
REL_STRENGTH_BAD = -10.0

PE_CHEAP = 12.0             # 이보다 낮으면 '싸다' 쪽
PE_RICH = 30.0              # 이보다 높으면 '비싸다' 쪽
PBR_CHEAP = 1.0
PBR_RICH = 5.0
ROE_GOOD = 15.0             # %
ROE_POOR = 5.0
MARGIN_GOOD = 15.0          # %
DEBT_HIGH = 150.0           # 부채비율 % (yfinance debtToEquity 는 이미 %)
DEBT_LOW = 50.0
GROWTH_GOOD = 10.0          # 매출 성장 %
GROWTH_BAD = -5.0

UPSIDE_GOOD = 15.0          # 목표가 상승여력 %
UPSIDE_BAD = 0.0
# 이보다 상승여력이 크면 '싸다'가 아니라 '목표가가 안 따라왔다'로 읽는다.
# 실측 근거: 삼성전자 목표가 470,156원 vs 현재 257,000원(+82.9%) — 주가는 고점 대비
# 29% 빠졌는데 목표가는 그대로였다.
UPSIDE_STALE = 45.0
MIN_ANALYSTS = 5            # 이보다 적으면 컨센서스로 치지 않는다

# 재무 지표의 극단값 — 그대로 '좋다'로 읽으면 오해가 된다.
# ROE 148%(AAPL)는 돈을 잘 벌어서라기보다 자사주 매입으로 자기자본이 줄어든 결과다.
ROE_EXTREME = 60.0
GROWTH_EXTREME = 60.0       # 매출 성장 % — 기저효과·사이클 회복일 때가 많다

ATR_MULT = 3.0              # ATR 손절 배수
LOSS_LIMIT_PCT = -15.0      # 평단 대비 손실 한도

# 신호 가중치. 예측(애널리스트)은 낮게 둔다 — 이유는 모듈 독스트링 참고.
WEIGHTS = {
    "trend": 2.0, "drawdown": 2.0, "rel_strength": 1.5, "rsi": 0.5,
    "valuation": 1.5, "profitability": 1.5, "growth": 1.0, "debt": 1.5,
    "analyst": 0.8, "position": 1.0,
}


# ------------------------------------------------------------------ 지표
def _rsi(close, n=14):
    if len(close) < n + 1:
        return None
    d = np.diff(close)
    up = np.where(d > 0, d, 0.0)
    down = np.where(d < 0, -d, 0.0)
    au, ad = up[-n:].mean(), down[-n:].mean()
    if ad == 0:
        return 100.0
    return float(100 - 100 / (1 + au / ad))


def atr(bars, n=14):
    """Average True Range — 손절 폭을 그 종목의 평소 변동폭에 맞추기 위한 것.

    고정 비율(예: -10%)로 손절하면 조용한 종목은 너무 늦게, 출렁이는 종목은
    너무 자주 털린다. ATR 은 그 차이를 흡수한다.
    """
    if len(bars) < n + 1:
        return None
    h, l, c = bars["high"].values, bars["low"].values, bars["close"].values
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(tr[-n:].mean())


def _pct_change(close, n):
    if len(close) <= n:
        return None
    prev = close[-1 - n]
    return None if prev == 0 else (close[-1] / prev - 1.0) * 100.0


def _sig(key, label, kind, value, stance, why, detail=None):
    """신호 하나. `kind` 로 사실과 예측을 가른다."""
    return {"key": key, "label": label, "kind": kind, "value": value,
            "stance": stance, "why": why, "detail": detail,
            "weight": WEIGHTS.get(key, 1.0)}


# --------------------------------------------------------------- 가격 신호
def price_signals(bars, benchmark_bars=None):
    """가격만으로 만드는 신호 — ETF 에도 적용된다."""
    out = []
    close = bars["close"].values
    last = float(close[-1])

    ma200 = float(close[-200:].mean()) if len(close) >= 200 else None
    ma50 = float(close[-50:].mean()) if len(close) >= 50 else None

    # 1) 추세
    if ma200 is not None:
        v = (last / ma200 - 1.0) * 100
        cross = ("정배열" if (ma50 or 0) > ma200 else "역배열") if ma50 else None
        if v > MA_TREND_PCT:
            out.append(_sig("trend", "추세", "fact", f"200일선 대비 {v:+.1f}%", "positive",
                            f"장기 추세 위에 있습니다({cross}). 추세가 유지되는 동안은 "
                            "보유의 근거가 되고, 200일선이 손절 기준선 역할을 합니다.",
                            {"ma200": round(ma200, 2), "vs_ma200_pct": round(v, 2)}))
        elif v < -MA_TREND_PCT:
            out.append(_sig("trend", "추세", "fact", f"200일선 대비 {v:+.1f}%", "negative",
                            f"장기 추세 아래입니다({cross}). 하락 추세에서 반등을 기다리는 것은 "
                            "예측이지 근거가 아닙니다.",
                            {"ma200": round(ma200, 2), "vs_ma200_pct": round(v, 2)}))
        else:
            out.append(_sig("trend", "추세", "fact", f"200일선 대비 {v:+.1f}%", "neutral",
                            "장기 추세선 근처입니다. 방향이 정해지지 않은 구간입니다.",
                            {"ma200": round(ma200, 2)}))
    else:
        out.append(_sig("trend", "추세", "fact", "—", "neutral",
                        "200일 이동평균을 만들 데이터가 부족합니다(상장 1년 미만 등)."))

    # 2) 고점 대비 낙폭 — '많이 올랐다'와 '지금 빠진다'는 동시에 참일 수 있다
    win = close[-252:] if len(close) >= 60 else close
    hi = float(win.max())
    dd = (last / hi - 1.0) * 100 if hi > 0 else None
    if dd is not None:
        if dd <= DRAWDOWN_BEAR:
            out.append(_sig("drawdown", "고점 대비", "fact", f"{dd:.1f}%", "negative",
                            f"52주 고점({hi:,.0f})에서 {abs(dd):.0f}% 빠졌습니다. "
                            "연초 대비 수익이 나 있어도 위험의 크기는 이 숫자입니다."))
        elif dd <= DRAWDOWN_WARN:
            out.append(_sig("drawdown", "고점 대비", "fact", f"{dd:.1f}%", "negative",
                            f"52주 고점에서 {abs(dd):.0f}% 조정 중입니다."))
        elif dd >= -3.0:
            out.append(_sig("drawdown", "고점 대비", "fact", f"{dd:.1f}%", "positive",
                            "52주 고점 부근입니다. 추세는 강하지만 신규 진입 시점 위험은 큽니다."))
        else:
            out.append(_sig("drawdown", "고점 대비", "fact", f"{dd:.1f}%", "neutral",
                            "고점에서 완만하게 물러난 상태입니다."))

    # 3) 상대강도 — 시장을 이기고 있는가
    if benchmark_bars is not None and len(benchmark_bars) > 63:
        b = benchmark_bars["close"].values
        s3, b3 = _pct_change(close, 63), _pct_change(b, 63)
        if s3 is not None and b3 is not None:
            rel = s3 - b3
            if rel >= REL_STRENGTH_GOOD:
                out.append(_sig("rel_strength", "상대강도", "fact",
                                f"3개월 시장 대비 {rel:+.1f}%p", "positive",
                                f"같은 기간 시장이 {b3:+.1f}%일 때 {s3:+.1f}% — 앞서고 있습니다."))
            elif rel <= REL_STRENGTH_BAD:
                out.append(_sig("rel_strength", "상대강도", "fact",
                                f"3개월 시장 대비 {rel:+.1f}%p", "negative",
                                f"같은 기간 시장이 {b3:+.1f}%일 때 {s3:+.1f}% — 뒤처지고 있습니다. "
                                "시장이 오르는데 못 따라가는 것은 그 자체로 정보입니다."))
            else:
                out.append(_sig("rel_strength", "상대강도", "fact",
                                f"3개월 시장 대비 {rel:+.1f}%p", "neutral",
                                "시장과 비슷하게 움직이고 있습니다."))

    # 4) RSI — 과열·과매도. 가중치를 낮게 둔다(단독 신호로는 약하다)
    r = _rsi(close)
    if r is not None:
        if r >= RSI_OVERBOUGHT:
            out.append(_sig("rsi", "단기 과열", "fact", f"RSI {r:.0f}", "negative",
                            "단기 과열 구간입니다. 추세를 부정하는 신호는 아니지만 "
                            "지금 사는 것은 비싼 자리일 수 있습니다."))
        elif r <= RSI_OVERSOLD:
            out.append(_sig("rsi", "단기 과매도", "fact", f"RSI {r:.0f}", "positive",
                            "단기 과매도 구간입니다. 다만 하락 추세에서는 계속 과매도인 채로 "
                            "더 빠지기도 합니다 — 추세 신호와 함께 보세요."))
    return out


# ----------------------------------------------------------- 펀더멘털 신호
def fundamental_signals(fund, price=None):
    """재무·밸류에이션·애널리스트 신호. ETF 면 '해당 없음'을 돌려준다."""
    out = []
    if fund is None:
        return out
    if fund.get("error"):
        out.append(_sig("data", "데이터", "fact", "조회 실패", "neutral",
                        f"펀더멘털을 받지 못했습니다: {fund['error']}"))
        return out
    if fund.get("is_fund"):
        out.append(_sig("data", "펀더멘털", "fact", "해당 없음", "neutral",
                        "ETF·펀드는 여러 종목을 담은 바구니라 PER·ROE·부채비율 같은 "
                        "기업 지표가 존재하지 않습니다. 가격·추세 신호로만 판단합니다."))
        return out

    f = fund.get("fields", {})

    # 밸류에이션 — PER 은 forward 를 우선한다(과거 이익보다 앞으로가 중요)
    pe = f.get("forwardPE") or f.get("trailingPE")
    pbr = f.get("priceToBook")
    if pe is not None:
        pe = float(pe)
        which = "선행" if f.get("forwardPE") is not None else "후행"
        if pe <= 0:
            out.append(_sig("valuation", "밸류에이션", "fact", f"{which} PER {pe:.1f}",
                            "negative", "이익이 적자라 PER 이 음수입니다. "
                            "밸류에이션으로 싸고 비쌈을 판단할 수 없는 상태입니다."))
        elif pe < PE_CHEAP:
            out.append(_sig("valuation", "밸류에이션", "fact", f"{which} PER {pe:.1f}",
                            "positive", f"이익 대비 주가가 낮은 편입니다(PER {pe:.1f}). "
                            "다만 싼 데는 이유가 있을 수 있으니 성장·재무 신호와 함께 보세요."))
        elif pe > PE_RICH:
            out.append(_sig("valuation", "밸류에이션", "fact", f"{which} PER {pe:.1f}",
                            "negative", f"이익 대비 주가가 높습니다(PER {pe:.1f}). "
                            "성장이 기대만큼 안 나오면 조정 폭이 커지는 자리입니다."))
        else:
            out.append(_sig("valuation", "밸류에이션", "fact", f"{which} PER {pe:.1f}",
                            "neutral", "밸류에이션이 극단적이지 않습니다."))
    if pbr is not None:
        pbr = float(pbr)
        stance = "positive" if pbr < PBR_CHEAP else ("negative" if pbr > PBR_RICH else "neutral")
        out.append(_sig("valuation_pbr", "PBR", "fact", f"{pbr:.2f}배", stance,
                        f"순자산 대비 {pbr:.2f}배에 거래됩니다."
                        + (" 장부가보다 싸게 거래되는 상태입니다." if pbr < 1 else "")))

    # 수익성
    roe = f.get("returnOnEquity")
    if roe is not None:
        roe = float(roe) * 100                     # yfinance 는 비율로 준다
        if roe >= ROE_EXTREME:
            # 극단값을 그대로 긍정으로 세지 않는다 — 분모(자기자본)가 작아진
            # 결과일 수 있고, 그건 수익성이 아니라 자본 구조의 이야기다.
            out.append(_sig("profitability", "수익성", "fact", f"ROE {roe:.0f}%", "neutral",
                            f"ROE 가 {roe:.0f}%로 이례적으로 높습니다. 이 정도 수치는 대개 "
                            "이익이 커서가 아니라 **자사주 매입 등으로 자기자본이 줄어든** "
                            "결과입니다. 수익성이 좋다는 근거로 세지 않았습니다 — "
                            "이익률 쪽을 함께 보세요."))
        elif roe >= ROE_GOOD:
            out.append(_sig("profitability", "수익성", "fact", f"ROE {roe:.1f}%", "positive",
                            f"자기자본 대비 {roe:.1f}%를 벌고 있습니다. "
                            "돈을 잘 버는 회사라는 뜻입니다."))
        elif roe <= ROE_POOR:
            out.append(_sig("profitability", "수익성", "fact", f"ROE {roe:.1f}%", "negative",
                            f"자기자본 대비 수익이 {roe:.1f}%로 낮습니다."))
        else:
            out.append(_sig("profitability", "수익성", "fact", f"ROE {roe:.1f}%", "neutral",
                            "수익성이 보통 수준입니다."))
    margin = f.get("profitMargins")
    if margin is not None:
        m = float(margin) * 100
        stance = "positive" if m >= MARGIN_GOOD else ("negative" if m < 0 else "neutral")
        out.append(_sig("margin", "이익률", "fact", f"{m:.1f}%", stance,
                        f"매출 100원당 {m:.1f}원이 남습니다."
                        + (" 적자입니다." if m < 0 else "")))

    # 성장
    rg = f.get("revenueGrowth")
    if rg is not None:
        g = float(rg) * 100
        if g >= GROWTH_EXTREME:
            out.append(_sig("growth", "성장", "fact", f"매출 {g:+.0f}%", "neutral",
                            f"매출이 전년 대비 {g:+.0f}% 늘었습니다. 이 정도 증가율은 "
                            "대개 **직전 해가 나빴던 기저효과**나 업황 사이클 회복입니다. "
                            "계속 이어질 성장률로 보지 마세요 — 근거로 세지 않았습니다."))
        elif g >= GROWTH_GOOD:
            out.append(_sig("growth", "성장", "fact", f"매출 {g:+.1f}%", "positive",
                            f"매출이 전년 대비 {g:+.1f}% 늘었습니다."))
        elif g <= GROWTH_BAD:
            out.append(_sig("growth", "성장", "fact", f"매출 {g:+.1f}%", "negative",
                            f"매출이 전년 대비 {g:+.1f}%로 줄고 있습니다. "
                            "역성장은 밸류에이션이 싸 보이는 이유가 되기도 합니다."))
        else:
            out.append(_sig("growth", "성장", "fact", f"매출 {g:+.1f}%", "neutral",
                            "매출이 크게 늘지도 줄지도 않았습니다."))

    # 재무 건전성
    de = f.get("debtToEquity")
    if de is not None:
        d = float(de)                              # 이미 % 단위
        if d >= DEBT_HIGH:
            out.append(_sig("debt", "재무", "fact", f"부채비율 {d:.0f}%", "negative",
                            f"자기자본 대비 부채가 {d:.0f}%입니다. 금리가 오르거나 "
                            "업황이 나빠질 때 먼저 흔들리는 구조입니다."))
        elif d <= DEBT_LOW:
            out.append(_sig("debt", "재무", "fact", f"부채비율 {d:.0f}%", "positive",
                            f"부채가 자기자본의 {d:.0f}% 수준으로 낮습니다. "
                            "충격을 견딜 여력이 있습니다."))
        else:
            out.append(_sig("debt", "재무", "fact", f"부채비율 {d:.0f}%", "neutral",
                            "부채 수준이 보통입니다."))

    # 애널리스트 — 유일한 '예측'. 가중치를 낮게 두고 편향을 명시한다.
    from . import fundamentals as F
    up = F.upside_pct(f, price)
    n_analyst = f.get("numberOfAnalystOpinions")
    if up is not None and (n_analyst or 0) >= MIN_ANALYSTS:
        rec = (f.get("recommendationKey") or "").replace("_", " ")
        target = float(f["targetMeanPrice"])
        detail = {"target": target, "upside_pct": round(up, 1),
                  "analysts": int(n_analyst), "recommendation": rec}

        # 상승여력이 과도하면 그 자체가 경고다 — 목표가가 주가 하락을 아직 못
        # 따라온 경우가 흔하다(증권사는 목표가를 늦게, 조금씩 내린다).
        # 실측: 삼성전자 목표가 470,156원 vs 현재 257,000원 = +82.9%.
        # 이걸 '싸다'로 읽으면 정확히 거꾸로 간다.
        if up >= UPSIDE_STALE:
            out.append(_sig("analyst", "애널리스트 목표가", "forecast",
                            f"{target:,.0f} ({up:+.1f}%)", "neutral",
                            f"증권사 {int(n_analyst)}곳 평균 목표가가 현재가보다 {up:.0f}% 높습니다. "
                            "이 정도 괴리는 '많이 싸다'는 신호이기보다 **목표가가 주가 하락을 "
                            "아직 반영하지 못한 것**일 때가 많습니다(증권사는 목표가를 늦게, "
                            "조금씩 내립니다). 근거로 세지 않았습니다.", detail))
        else:
            stance = ("positive" if up >= UPSIDE_GOOD
                      else ("negative" if up <= UPSIDE_BAD else "neutral"))
            out.append(_sig("analyst", "애널리스트 목표가", "forecast",
                            f"{target:,.0f} ({up:+.1f}%)", stance,
                            f"증권사 {int(n_analyst)}곳 평균 목표가가 현재가 대비 {up:+.1f}%"
                            + (f", 컨센서스는 '{rec}'" if rec else "") + "입니다. "
                            "⚠️ 이건 예측이고 증권사 목표가는 낙관 편향이 큽니다"
                            "(매도 의견이 드뭅니다). 사실 신호와 같은 무게로 보지 마세요.",
                            detail))
    elif up is not None:
        out.append(_sig("analyst", "애널리스트 목표가", "forecast", "표본 부족", "neutral",
                        f"목표가를 낸 증권사가 {n_analyst or 0}곳뿐이라 "
                        f"컨센서스로 보기 어렵습니다(기준 {MIN_ANALYSTS}곳)."))
    return out


# ------------------------------------------------------------------ 손절선
def stop_levels(bars, avg_cost=None, currency_note=None):
    """손절 후보 세 가지를 **전부** 돌려준다.

    하나만 고르면 그 선택 자체가 숨은 가정이 된다. 세 가지의 성격이 다르다:
      · ATR   — 그 종목의 평소 변동폭 기준. 흔들림에 안 털리게.
      · 추세  — 200일선. 여기가 깨지면 '보유의 근거'가 사라진다.
      · 손실  — 평단 대비 한도. 계좌를 지키는 선.
    보통은 **가장 높은 값**이 먼저 걸리는 선이고, 그걸 실질 손절선으로 본다.
    """
    close = bars["close"].values
    last = float(close[-1])
    levels = []

    a = atr(bars)
    if a:
        lv = last - ATR_MULT * a
        levels.append({"key": "atr", "label": f"변동성 기준 ({ATR_MULT:g}×ATR)",
                       "price": round(lv, 2),
                       "distance_pct": round((lv / last - 1) * 100, 1),
                       "why": f"최근 하루 평균 변동폭이 {a:,.2f}입니다. "
                              f"그 {ATR_MULT:g}배 아래는 평소 출렁임으로는 잘 닿지 않는 자리라, "
                              "정상적인 흔들림에 털리지 않으면서 추세 이탈은 잡습니다."})

    if len(close) >= 200:
        ma200 = float(close[-200:].mean())
        levels.append({"key": "trend", "label": "추세 기준 (200일선)",
                       "price": round(ma200, 2),
                       "distance_pct": round((ma200 / last - 1) * 100, 1),
                       "why": "200일선 아래로 내려가면 '장기 추세 위에 있다'는 보유 근거가 "
                              "사라집니다. 근거가 사라지면 파는 것이 규율입니다."})

    if avg_cost:
        lv = float(avg_cost) * (1 + LOSS_LIMIT_PCT / 100)
        levels.append({"key": "loss", "label": f"손실 한도 (평단 {LOSS_LIMIT_PCT:g}%)",
                       "price": round(lv, 2),
                       "distance_pct": round((lv / last - 1) * 100, 1),
                       "why": f"평단 {float(avg_cost):,.0f} 대비 {abs(LOSS_LIMIT_PCT):g}% 손실 "
                              "지점입니다. 한 종목의 손실이 계좌 전체 계획을 흔들지 않게 "
                              "미리 정해두는 선입니다."})

    if not levels:
        return {"levels": [], "effective": None,
                "note": "손절선을 계산할 데이터가 부족합니다."}

    # 현재가 아래에 있는 것 중 가장 높은 것 = 앞으로 가장 먼저 닿는 선
    below = [lv for lv in levels if lv["price"] < last]
    # **이미 지난 선을 따로 센다.** 이걸 빼먹으면 "200일선을 한참 전에 깨고 내려온"
    # 종목이 ATR 손절선만 보여주면서 멀쩡해 보인다 — 실측에서 실제로 그랬다
    # (에코프로비엠: 200일선이 현재가보다 43% 위인데 화면엔 ATR 선만 떴다).
    breached = [lv for lv in levels if lv["price"] >= last]
    for lv in breached:
        lv["breached"] = True

    effective = max(below, key=lambda x: x["price"]) if below else None
    if breached:
        names = ", ".join(f"{lv['label']}({lv['price']:,.2f})" for lv in breached)
        note = (f"⚠️ 이미 지난 손절선이 있습니다 — {names}. "
                "이 선들은 '여기가 깨지면 판다'고 정해둔 자리이고, 이미 그 아래입니다."
                + (f" 남은 선은 '{effective['label']}' {effective['price']:,.2f}"
                   f"({effective['distance_pct']:+.1f}%)입니다." if effective else ""))
    elif effective:
        note = (f"가장 먼저 닿는 선은 '{effective['label']}' {effective['price']:,.2f}"
                f" (현재가 대비 {effective['distance_pct']:+.1f}%)입니다.")
    else:
        note = "손절 후보를 계산하지 못했습니다."

    return {
        "levels": sorted(levels, key=lambda x: -x["price"]),
        "effective": effective,
        "breached": breached,
        "note": note,
    }


# ------------------------------------------------------------------ 판정
def _tally(signals):
    """찬반 가중합. 예측(`forecast`)은 이미 가중치가 낮게 잡혀 있다."""
    score = 0.0
    for s in signals:
        w = s["weight"]
        if s["stance"] == "positive":
            score += w
        elif s["stance"] == "negative":
            score -= w
    return round(score, 2)


def _label(score, holding, stop_breached):
    """판정 문구. 보유 중인지 아닌지에 따라 답이 달라진다.

    같은 상태라도 "살까"와 "팔까"는 다른 질문이다 — 이미 보유 중이면 거래비용과
    세금이 들고, 미보유면 진입 시점 위험이 있다.
    """
    if holding and stop_breached:
        return ("손절 검토", "미리 정한 손절선을 이미 지났습니다. "
                             "'조금만 더 기다려보자'는 그 선을 정한 이유를 없애는 결정입니다.")
    if score >= 4:
        return ("매수 검토" if not holding else "보유 유지",
                "긍정 근거가 뚜렷하게 우세합니다.")
    if score >= 1.5:
        return ("분할 매수 검토" if not holding else "보유 유지",
                "긍정 쪽이 우세하지만 반대 근거도 있습니다. 한 번에 넣기보다 나눠서.")
    if score > -1.5:
        return ("관망" if not holding else "보유 (적극적 근거는 약함)",
                "찬반이 팽팽합니다. 확실한 근거 없이 비중을 늘릴 자리는 아닙니다.")
    if score > -4:
        return ("진입 보류" if not holding else "비중 축소 검토",
                "반대 근거가 우세합니다.")
    return ("진입 보류" if not holding else "매도 검토",
            "반대 근거가 뚜렷하게 우세합니다.")


def judge(ticker, bars, fund=None, holding=None, benchmark_bars=None, name=None):
    """종목 하나를 판정하고 그 이유를 돌려준다.

    ticker         : 심볼
    bars           : OHLCV DataFrame (기준일까지)
    fund           : `fundamentals.fetch_one` 결과 (없으면 가격 신호만)
    holding        : {'quantity':…, 'avg_cost':…} — 보유 중이면 판정이 달라진다
    benchmark_bars : 상대강도 계산용 (보통 SPY 또는 지수)
    """
    close = bars["close"].values
    last = float(close[-1])

    signals = price_signals(bars, benchmark_bars)
    signals += fundamental_signals(fund, price=last)

    # 보유 손익 — 사실이고, 판정보다 '지금 무엇을 결정해야 하는가'를 정한다
    avg_cost = (holding or {}).get("avg_cost")
    if avg_cost:
        pnl = (last / float(avg_cost) - 1.0) * 100
        if pnl <= LOSS_LIMIT_PCT:
            signals.append(_sig("position", "보유 손익", "fact", f"{pnl:+.1f}%", "negative",
                                f"평단 대비 {pnl:+.1f}%입니다. 미리 정한 손실 한도"
                                f"({LOSS_LIMIT_PCT:g}%)를 넘었습니다."))
        elif pnl >= 30:
            signals.append(_sig("position", "보유 손익", "fact", f"{pnl:+.1f}%", "neutral",
                                f"평단 대비 {pnl:+.1f}%입니다. 이익이 커진 만큼 이 종목의 "
                                "비중도 커졌을 가능성이 높습니다 — 비중을 확인해보세요."))
        else:
            signals.append(_sig("position", "보유 손익", "fact", f"{pnl:+.1f}%", "neutral",
                                f"평단 대비 {pnl:+.1f}%입니다."))

    stops = stop_levels(bars, avg_cost=avg_cost)
    # 손절선을 하나라도 이미 지났으면 판정을 손절 쪽으로 돌린다.
    # (모든 선을 다 지나야 손절이라고 하면 그건 손절선이 아니라 사후 설명이다)
    stop_breached = bool(stops.get("breached"))

    score = _tally(signals)
    label, summary = _label(score, holding=bool(holding), stop_breached=stop_breached)

    fors = [s for s in signals if s["stance"] == "positive"]
    againsts = [s for s in signals if s["stance"] == "negative"]

    return {
        "ticker": ticker,
        "name": name or (fund or {}).get("fields", {}).get("shortName") or ticker,
        "price": round(last, 2),
        "asof": str(bars.index[-1].date()),
        "verdict": label,
        "summary": summary,
        "score": score,
        "signals": signals,
        "reasons_for": [{"label": s["label"], "value": s["value"], "why": s["why"],
                         "kind": s["kind"]} for s in fors],
        "reasons_against": [{"label": s["label"], "value": s["value"], "why": s["why"],
                             "kind": s["kind"]} for s in againsts],
        "stops": stops,
        "is_fund": bool((fund or {}).get("is_fund")),
        "holding": holding,
        "disclaimer": ("판정은 위 규칙의 결과이지 예측이 아닙니다. 애널리스트 목표가만 "
                       "'예측'이며 낙관 편향이 있습니다. 투자 판단과 결과는 본인 책임입니다."),
    }
