"""결손 가격을 숫자로 속이지 않는지. 네트워크는 쓰지 않는다."""
import pandas as pd

from core.data_desk import latest_prices


def test_마지막_종가가_NaN이면_키를_뺀다():
    idx = pd.bdate_range("2024-01-01", periods=2)
    bars = {"AAA": pd.DataFrame({"close": [10.0, float("nan")]}, index=idx)}
    assert latest_prices(bars, ["AAA", "없는종목"]) == {}


def test_양수_종가만_남긴다():
    idx = pd.bdate_range("2024-01-01", periods=1)
    bars = {
        "AAA": pd.DataFrame({"close": [10.0]}, index=idx),
        "BBB": pd.DataFrame({"close": [0.0]}, index=idx),
    }
    assert latest_prices(bars, ["AAA", "BBB"]) == {"AAA": 10.0}
