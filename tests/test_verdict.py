"""
종목 판정 테스트 — 네트워크를 쓰지 않는다.

여기서 고정하는 것은 **판정이 정직한가**이다. 이 모듈에서 조용히 틀리는 방식은
전부 "그럴듯하게 좋아 보이는" 쪽이다: 예측을 사실처럼 세거나, 극단값을 그대로
장점으로 읽거나, 이미 지난 손절선을 화면에서 흘리거나, 반대 근거를 안 보여주거나.
넷 다 에러가 안 나므로 테스트가 유일한 방어선이다.
"""
import numpy as np
import pandas as pd
import pytest

from core import verdict as V


def bars(prices, highs=None, lows=None, start="2024-01-01"):
    p = np.asarray(prices, dtype=float)
    idx = pd.bdate_range(start, periods=len(p))
    return pd.DataFrame({
        "open": p, "high": highs if highs is not None else p * 1.01,
        "low": lows if lows is not None else p * 0.99,
        "close": p, "volume": np.full(len(p), 1e6),
    }, index=idx)


def rising(n=300, a=100.0, b=200.0):
    return bars(np.linspace(a, b, n))


def falling(n=300, a=200.0, b=100.0):
    return bars(np.linspace(a, b, n))


def fund(**fields):
    return {"ticker": "T", "fields": fields, "missing": [], "not_applicable": [],
            "is_fund": False, "error": None}


# ------------------------------------------------------- 사실과 예측을 가른다
def test_애널리스트_목표가는_예측으로_라벨된다():
    """이 시스템에 남은 유일한 예측이다. 사실 신호와 섞이면 안 된다."""
    sigs = V.fundamental_signals(
        fund(targetMeanPrice=120.0, numberOfAnalystOpinions=20,
             recommendationKey="buy"), price=100.0)
    a = next(s for s in sigs if s["key"] == "analyst")
    assert a["kind"] == "forecast"
    assert "예측" in a["why"]


def test_재무_지표는_사실로_라벨된다():
    sigs = V.fundamental_signals(fund(debtToEquity=30.0, returnOnEquity=0.2), price=100.0)
    assert all(s["kind"] == "fact" for s in sigs)


def test_예측은_사실보다_가중치가_낮다():
    """검증되지 않은 예측이 판정을 좌우하면 오늘 Kronos 에서 겪은 일이 반복된다."""
    assert V.WEIGHTS["analyst"] < V.WEIGHTS["trend"]
    assert V.WEIGHTS["analyst"] < V.WEIGHTS["debt"]


# ------------------------------------------------------------- 극단값 처리
def test_상승여력이_과도하면_근거로_세지_않는다():
    """실측(삼성전자): 목표가가 현재가보다 82.9% 높았는데, 주가가 고점 대비 29%
    빠지는 동안 목표가는 안 내려온 것이었다. 이걸 '싸다'로 읽으면 거꾸로 간다."""
    sigs = V.fundamental_signals(
        fund(targetMeanPrice=183.0, numberOfAnalystOpinions=30), price=100.0)
    a = next(s for s in sigs if s["key"] == "analyst")
    assert a["stance"] == "neutral"
    assert "반영하지 못한" in a["why"]


def test_적당한_상승여력은_긍정_근거가_된다():
    sigs = V.fundamental_signals(
        fund(targetMeanPrice=125.0, numberOfAnalystOpinions=30), price=100.0)
    a = next(s for s in sigs if s["key"] == "analyst")
    assert a["stance"] == "positive"


def test_애널리스트가_적으면_컨센서스로_치지_않는다():
    sigs = V.fundamental_signals(
        fund(targetMeanPrice=125.0, numberOfAnalystOpinions=2), price=100.0)
    a = next(s for s in sigs if s["key"] == "analyst")
    assert a["stance"] == "neutral"
    assert "표본 부족" in a["value"]


def test_ROE_극단값은_수익성_근거로_세지_않는다():
    """실측(AAPL): ROE 148.8%. 이익이 커서가 아니라 자사주 매입으로 자기자본이
    줄어든 결과다. 그대로 '돈을 잘 번다'로 읽으면 오해가 된다."""
    sigs = V.fundamental_signals(fund(returnOnEquity=1.488), price=100.0)
    p = next(s for s in sigs if s["key"] == "profitability")
    assert p["stance"] == "neutral"
    assert "자기자본이 줄어든" in p["why"]


def test_정상_범위의_ROE_는_긍정_근거다():
    sigs = V.fundamental_signals(fund(returnOnEquity=0.30), price=100.0)
    p = next(s for s in sigs if s["key"] == "profitability")
    assert p["stance"] == "positive"


def test_성장률_극단값은_기저효과로_다룬다():
    """실측(삼성전자): 매출 +130%. 반도체 사이클 회복이지 지속 성장률이 아니다."""
    sigs = V.fundamental_signals(fund(revenueGrowth=1.3), price=100.0)
    g = next(s for s in sigs if s["key"] == "growth")
    assert g["stance"] == "neutral"
    assert "기저효과" in g["why"]


def test_적자_기업의_PER_은_긍정이_될_수_없다():
    sigs = V.fundamental_signals(fund(forwardPE=-8.0), price=100.0)
    v = next(s for s in sigs if s["key"] == "valuation")
    assert v["stance"] == "negative"
    assert "적자" in v["why"]


# ----------------------------------------------------------------- ETF
def test_ETF_는_펀더멘털을_해당없음으로_처리한다():
    """ETF 는 회사가 아니라 바구니다. 없는 값을 0 으로 채우면
    '부채 없는 우량 종목'이 되어버린다."""
    f = {"ticker": "SPY", "fields": {}, "missing": [], "is_fund": True,
         "not_applicable": ["returnOnEquity", "debtToEquity"], "error": None}
    sigs = V.fundamental_signals(f, price=100.0)
    assert len(sigs) == 1
    assert sigs[0]["stance"] == "neutral"
    assert "해당 없음" in sigs[0]["value"]
    assert not [s for s in sigs if s["key"] in ("debt", "profitability")]


def test_ETF_도_가격_신호로는_판정된다():
    v = V.judge("SPY", rising(), fund={"ticker": "SPY", "fields": {}, "is_fund": True,
                                       "missing": [], "not_applicable": [], "error": None})
    assert v["is_fund"] is True
    assert [s for s in v["signals"] if s["key"] == "trend"]


# ---------------------------------------------------------------- 손절선
def test_이미_지난_손절선을_흘리지_않는다():
    """실측(에코프로비엠): 200일선이 현재가보다 43% 위였는데 화면엔 ATR 선만 떠서
    멀쩡해 보였다. '이미 깨진 선'이야말로 제일 중요한 정보다."""
    st = V.stop_levels(falling())
    assert st["breached"], "하락 추세인데 지나온 손절선이 없다고 보고했다"
    assert any(lv["key"] == "trend" for lv in st["breached"])
    assert "이미 지난" in st["note"]


def test_상승_추세에서는_지난_손절선이_없다():
    st = V.stop_levels(rising())
    assert not st["breached"]
    assert st["effective"] is not None
    assert st["effective"]["price"] < float(rising()["close"].iloc[-1])


def test_손절_후보를_하나로_줄이지_않는다():
    """하나만 고르면 그 선택 자체가 숨은 가정이 된다."""
    st = V.stop_levels(rising(), avg_cost=150.0)
    keys = {lv["key"] for lv in st["levels"]}
    assert keys == {"atr", "trend", "loss"}
    assert all(lv["why"] for lv in st["levels"])


def test_평단이_없으면_손실_한도선은_만들지_않는다():
    """모르는 값을 지어내지 않는다."""
    st = V.stop_levels(rising())
    assert "loss" not in {lv["key"] for lv in st["levels"]}


def test_ATR_손절은_종목의_변동폭에_비례한다():
    """고정 비율로 자르면 조용한 종목은 늦게, 출렁이는 종목은 자주 털린다."""
    calm = bars(np.linspace(100, 110, 300),
                highs=np.linspace(100, 110, 300) * 1.002,
                lows=np.linspace(100, 110, 300) * 0.998)
    wild = bars(np.linspace(100, 110, 300),
                highs=np.linspace(100, 110, 300) * 1.05,
                lows=np.linspace(100, 110, 300) * 0.95)
    calm_stop = next(lv for lv in V.stop_levels(calm)["levels"] if lv["key"] == "atr")
    wild_stop = next(lv for lv in V.stop_levels(wild)["levels"] if lv["key"] == "atr")
    assert abs(wild_stop["distance_pct"]) > abs(calm_stop["distance_pct"])


# ---------------------------------------------------------------- 판정
def test_찬성과_반대_근거를_모두_돌려준다():
    """한쪽만 보여주면 사고 싶은 이유만 찾게 된다."""
    v = V.judge("T", rising(), fund=fund(forwardPE=45.0, returnOnEquity=0.25,
                                          debtToEquity=200.0))
    assert v["reasons_for"] and v["reasons_against"]


def test_손절선을_지났으면_판정이_손절_쪽으로_간다():
    v = V.judge("T", falling(), holding={"quantity": 10, "avg_cost": 190.0})
    assert v["verdict"] == "손절 검토"
    assert "손절선" in v["summary"]


def test_같은_상태라도_보유_여부에_따라_판정이_다르다():
    """'살까'와 '팔까'는 다른 질문이다 — 보유 중이면 거래비용과 세금이 든다."""
    b = rising()
    f = fund(forwardPE=10.0, returnOnEquity=0.25, debtToEquity=20.0)
    buy = V.judge("T", b, fund=f)
    hold = V.judge("T", b, fund=f, holding={"quantity": 1, "avg_cost": 100.0})
    assert buy["verdict"] != hold["verdict"]


def test_판정_근거가_비어_있지_않다():
    """근거 없는 판정은 이 모듈의 존재 이유를 없앤다."""
    v = V.judge("T", rising(), fund=fund(returnOnEquity=0.25))
    assert v["signals"]
    assert all(s["why"] for s in v["signals"])
    assert v["summary"]


def test_판정은_예측이_아님을_명시한다():
    v = V.judge("T", rising())
    assert "예측이 아닙니다" in v["disclaimer"]


def test_데이터가_부족해도_죽지_않는다():
    """상장 1년 미만 종목은 200일선을 만들 수 없다. 그래도 판정은 나와야 한다."""
    v = V.judge("T", rising(n=40))
    assert v["verdict"]
    trend = next(s for s in v["signals"] if s["key"] == "trend")
    assert trend["stance"] == "neutral"
    assert "부족" in trend["why"]


def test_펀더멘털이_없어도_가격_신호로_판정한다():
    v = V.judge("T", rising(), fund=None)
    assert v["verdict"]
    assert [s for s in v["signals"] if s["key"] == "trend"]


def test_조회_실패는_실패로_보고한다():
    """빈 결과로 돌려주면 '펀더멘털이 좋지도 나쁘지도 않다'로 잘못 읽힌다."""
    f = {"ticker": "T", "fields": {}, "missing": [], "not_applicable": [],
         "is_fund": False, "error": "HTTPError: 404"}
    sigs = V.fundamental_signals(f, price=100.0)
    assert len(sigs) == 1
    assert "조회 실패" in sigs[0]["value"]
