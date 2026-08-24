"""
보유 종목 분석 테스트 — 네트워크·모델을 쓰지 않는다.

분석의 목적은 "예측을 맞히는 것"이 아니라 **사용자가 몰랐던 사실을 알려주는 것**이다.
예측은 틀릴 수 있지만 "한 종목에 60%가 몰려 있다"나 "보유 종목이 전부 같이 움직인다"는
틀리지 않는다. 그래서 진단 쪽을 더 촘촘히 고정한다.

그리고 여기서도 원칙은 같다 — **계산할 수 없으면 지어내지 않는다.**
"""
import numpy as np
import pandas as pd
import pytest

from core import config, holdings_analysis as ha
from core.holdings import HoldingsBook


def make_bars(spec, n=400, seed=3):
    """spec = {ticker: (드리프트, 변동성, 공통요인 가중치)}

    공통요인 가중치를 주면 종목들이 같이 움직인다 — 상관 테스트에 쓴다.
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    common = rng.normal(0, 0.01, n)
    out = {}
    for tk, (drift, vol, beta) in spec.items():
        idio = rng.normal(drift, vol, n)
        rets = beta * common + (1 - beta) * idio
        close = 100.0 * np.cumprod(1.0 + rets)
        out[tk] = pd.DataFrame({
            "open": close * 0.999, "high": close * 1.004,
            "low": close * 0.996, "close": close,
            "volume": np.full(n, 1e6),
        }, index=idx)
    return out


# ------------------------------------------------------------------ 집중도
def test_한_종목에_몰리면_집중_경고를_낸다():
    conc = ha.concentration({"AAA": 0.7, "BBB": 0.2, "CCC": 0.1})
    assert conc["top_weight_pct"] == 70.0
    assert conc["flags"]


def test_고르게_분산되면_경고가_없다():
    conc = ha.concentration({t: 0.1 for t in "ABCDEFGHIJ"})
    assert not conc["flags"]
    assert conc["effective_positions"] == pytest.approx(10.0, abs=0.1)


def test_유효_종목수는_쏠릴수록_작아진다():
    """10종목을 갖고 있어도 하나에 90%면 사실상 1종목이다."""
    even = ha.concentration({t: 0.1 for t in "ABCDEFGHIJ"})
    skewed = ha.concentration({"A": 0.9, **{t: 0.0111 for t in "BCDEFGHIJ"}})
    assert skewed["effective_positions"] < 2 < even["effective_positions"]


def test_비중이_없으면_집중도를_계산하지_않는다():
    assert ha.concentration({}) is None


# ------------------------------------------------------------------ 상관
def test_같이_움직이는_종목들은_상관_경고를_낸다():
    """종목 수가 많아도 상관이 1에 가까우면 분산이 아니다."""
    bars = make_bars({"AAA": (0.0002, 0.01, 0.95),
                      "BBB": (0.0002, 0.01, 0.95),
                      "CCC": (0.0002, 0.01, 0.95)})
    corr = ha.correlation(bars, ["AAA", "BBB", "CCC"])

    assert corr["avg_correlation"] > 0.7
    assert corr["flags"]


def test_독립적인_종목들은_상관_경고가_없다():
    bars = make_bars({"AAA": (0.0002, 0.01, 0.0),
                      "BBB": (0.0002, 0.01, 0.0),
                      "CCC": (0.0002, 0.01, 0.0)})
    corr = ha.correlation(bars, ["AAA", "BBB", "CCC"])
    assert abs(corr["avg_correlation"]) < 0.3
    assert not corr["flags"]


def test_종목이_하나면_상관을_계산하지_않는다():
    bars = make_bars({"AAA": (0.0002, 0.01, 0.0)})
    assert ha.correlation(bars, ["AAA"]) is None


# ------------------------------------------------------------------ 변동성
def test_분산_효과가_수치로_나온다():
    """포트폴리오 변동성이 가중평균보다 낮은 만큼이 분산 효과다."""
    bars = make_bars({"AAA": (0.0, 0.012, 0.0), "BBB": (0.0, 0.012, 0.0)})
    vol = ha.portfolio_volatility(bars, {"AAA": 0.5, "BBB": 0.5})

    assert vol["portfolio_vol_annual_pct"] < vol["weighted_avg_vol_pct"]
    assert vol["diversification_benefit_pct"] > 0


def test_같이_움직이면_분산_효과가_거의_없다():
    bars = make_bars({"AAA": (0.0, 0.012, 0.99), "BBB": (0.0, 0.012, 0.99)})
    vol = ha.portfolio_volatility(bars, {"AAA": 0.5, "BBB": 0.5})
    assert vol["diversification_benefit_pct"] < 1.0


def test_데이터가_부족하면_변동성을_지어내지_않는다():
    bars = make_bars({"AAA": (0.0, 0.01, 0.0)}, n=10)
    assert ha.portfolio_volatility(bars, {"AAA": 1.0}) is None


# ------------------------------------------------------------------ 통화·손익
def test_통화_노출을_비중으로_보여준다():
    rows = [
        {"currency": "KRW", "market_value_base": 700.0},
        {"currency": "USD", "market_value_base": 300.0},
    ]
    fx = ha.currency_exposure(rows)
    assert fx == {"KRW": 70.0, "USD": 30.0}


def test_환산_실패한_종목은_통화노출에서_빠진다():
    rows = [{"currency": "KRW", "market_value_base": 100.0},
            {"currency": "JPY", "market_value_base": None}]
    assert ha.currency_exposure(rows) == {"KRW": 100.0}


def test_손익은_평단이_있는_종목만_센다():
    rows = [
        {"ticker": "A", "pnl": 100.0, "pnl_pct": 10.0, "cost_basis": 1000.0, "fx_rate": 1.0},
        {"ticker": "B", "pnl": -50.0, "pnl_pct": -5.0, "cost_basis": 1000.0, "fx_rate": 1.0},
        {"ticker": "C", "pnl": None, "pnl_pct": None, "cost_basis": None, "fx_rate": 1.0},
    ]
    pnl = ha.pnl_summary(rows)

    assert pnl["covered"] == 2
    assert pnl["uncovered"] == 1
    assert pnl["total_pnl_base"] == pytest.approx(50.0)
    assert pnl["total_pnl_pct"] == pytest.approx(2.5)
    assert pnl["best"]["ticker"] == "A"
    assert pnl["worst"]["ticker"] == "B"


def test_평단이_하나도_없으면_손익을_내지_않는다():
    rows = [{"ticker": "A", "pnl": None, "pnl_pct": None, "cost_basis": None, "fx_rate": 1.0}]
    assert ha.pnl_summary(rows) is None


# ------------------------------------------------------------------ 견해
def fake_scorer(ticker, bars, asof=None):
    close = bars["close"]
    mom = float(close.iloc[-1] / close.iloc[-21] - 1.0)
    return {
        "ticker": ticker, "last_close": float(close.iloc[-1]),
        "view_daily": mom / 100, "view_horizon_pct": round(mom * 100, 3),
        "dispersion": 0.02 + 0.01 * (sum(ord(c) for c in ticker) % 3),
        "up_path_ratio": 0.6, "fan": {"p05": [], "p50": [], "p95": []},
        "mom_20d_pct": 0.0, "mom_60d_pct": 0.0, "realized_vol_pct": 12.0,
        "infer_sec": 0.0, "asof": str(asof or close.index[-1])[:10],
    }


def test_견해는_보유_종목에_대해_나온다(monkeypatch):
    monkeypatch.setattr(config, "LOOKBACK", 60)
    bars = make_bars({"AAA": (0.001, 0.01, 0.0), "BBB": (-0.001, 0.01, 0.0)}, n=200)

    views, skipped = ha.views_for(["AAA", "BBB"], bars=bars, scorer=fake_scorer)

    assert {v["ticker"] for v in views} == {"AAA", "BBB"}
    assert not skipped
    assert all("confidence" in v and "direction" in v for v in views)


def test_봉이_부족한_종목은_사유와_함께_빠진다(monkeypatch):
    """중립 견해를 지어내면 그게 신호인 줄 알게 된다."""
    monkeypatch.setattr(config, "LOOKBACK", 300)
    bars = make_bars({"AAA": (0.001, 0.01, 0.0)}, n=100)

    views, skipped = ha.views_for(["AAA"], bars=bars, scorer=fake_scorer)

    assert views == []
    assert "봉이 부족" in skipped["AAA"]


def test_시세가_없는_종목도_사유와_함께_빠진다(monkeypatch):
    monkeypatch.setattr(config, "LOOKBACK", 60)
    bars = make_bars({"AAA": (0.001, 0.01, 0.0)}, n=200)

    views, skipped = ha.views_for(["AAA", "GHOST"], bars=bars, scorer=fake_scorer)

    assert [v["ticker"] for v in views] == ["AAA"]
    assert "GHOST" in skipped


# ------------------------------------------------------------------ 전체 분석
def test_빈_장부는_안내만_돌려준다(tmp_path):
    book = HoldingsBook(path=tmp_path / "h.json")
    result = ha.analyze(book)
    assert result["empty"]
    assert "보유 종목이 없습니다" in result["notes"][0]


def test_분석_결과에_면책이_붙는다(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOOKBACK", 60)
    monkeypatch.setattr(config, "HOLDINGS_BASE_CURRENCY", "USD")
    bars = make_bars({"AAA": (0.001, 0.01, 0.2), "BBB": (0.0005, 0.01, 0.2)}, n=200)

    book = HoldingsBook(path=tmp_path / "h.json")
    book.upsert(ticker="AAA", quantity=10, avg_cost=90.0, currency="USD")
    book.upsert(ticker="BBB", quantity=5, avg_cost=110.0, currency="USD")

    monkeypatch.setattr("core.symbol_search.quote",
                        lambda tickers: {tk: 100.0 for tk in tickers})
    monkeypatch.setattr("core.symbol_search.fx_rates", lambda cur, base=None: {"USD": 1.0})

    result = ha.analyze(book, bars=bars, scorer=fake_scorer)

    assert not result["empty"]
    assert "투자 조언이 아닙니다" in result["disclaimer"]
    assert result["diagnostics"]["concentration"]["n_positions"] == 2
    assert result["valuation"]["total_value"] == pytest.approx(1500.0)
    assert any("검증되지 않았" in n for n in result["notes"])


def test_비중이_큰데_견해가_나쁜_종목을_짚어준다(tmp_path, monkeypatch):
    """사용자가 가장 알고 싶어 할 조합이다."""
    monkeypatch.setattr(config, "LOOKBACK", 60)
    monkeypatch.setattr(config, "HOLDINGS_BASE_CURRENCY", "USD")
    bars = make_bars({"BIG": (-0.002, 0.01, 0.0), "SMALL": (0.002, 0.01, 0.0)}, n=200)

    book = HoldingsBook(path=tmp_path / "h.json")
    book.upsert(ticker="BIG", quantity=100, currency="USD")
    book.upsert(ticker="SMALL", quantity=1, currency="USD")

    monkeypatch.setattr("core.symbol_search.quote",
                        lambda tickers: {tk: 100.0 for tk in tickers})
    monkeypatch.setattr("core.symbol_search.fx_rates", lambda cur, base=None: {"USD": 1.0})

    result = ha.analyze(book, bars=bars, scorer=fake_scorer)
    joined = " ".join(result["notes"])

    assert "BIG" in joined
    assert "비중이 큰데" in joined
