"""
보유 종목 장부 · CSV 임포트 테스트.

여기서 지키는 것:

**① 두 장부는 절대 섞이지 않는다.** 봇의 모의 장부(`ExecutionDesk`)와 사용자의
실제 보유(`HoldingsBook`)는 다른 파일, 다른 모듈이다. 실제 보유가 봇의 목표비중에
흘러 들어가면 봇이 사용자 돈을 기준으로 판단하는 것처럼 보이게 된다.

**② 없는 값을 0 으로 채우지 않는다.** 시세를 못 받은 종목, 환율이 없는 통화,
평단이 입력 안 된 종목은 합산에서 빼고 **목록으로 보고한다**. 0 으로 채우면
자산이 조용히 줄어들고 손익이 거짓이 된다.

네트워크는 쓰지 않는다.
"""
import json

import pytest

from core import config, holdings, holdings_csv
from core.holdings import HoldingsBook


@pytest.fixture
def book(tmp_path):
    return HoldingsBook(path=tmp_path / "holdings.json")


# ------------------------------------------------------------------ 기본 편집
def test_보유를_추가하고_읽는다(book):
    book.upsert(ticker="005930.KS", quantity=10, avg_cost=70000)
    assert book.tickers == ["005930.KS"]
    assert book.items["005930.KS"]["quantity"] == 10


def test_수량이_0이하면_거부한다(book):
    """0주는 보유가 아니다. 남겨두면 비중 계산에 0 이 섞인다."""
    with pytest.raises(ValueError, match="수량은 0보다"):
        book.upsert(ticker="AAPL", quantity=0)
    with pytest.raises(ValueError, match="수량은 0보다"):
        book.upsert(ticker="AAPL", quantity=-5)


def test_종목코드가_비면_거부한다(book):
    with pytest.raises(ValueError, match="종목 코드가 비어"):
        book.upsert(ticker="  ", quantity=1)


def test_알_수_없는_출처는_거부한다(book):
    """출처가 뒤섞이면 replace_source 가 엉뚱한 줄을 지운다."""
    with pytest.raises(ValueError, match="알 수 없는 출처"):
        book.upsert(ticker="AAPL", quantity=1, source="mystery")


def test_티커는_대문자로_정규화된다(book):
    book.upsert(ticker=" aapl ", quantity=1)
    assert book.tickers == ["AAPL"]


def test_통화는_접미사로_추정된다():
    assert holdings.infer_currency("005930.KS") == "KRW"
    assert holdings.infer_currency("AAPL") == "USD"
    assert holdings.infer_currency("7203.T") == "JPY"


# ------------------------------------------------------------------ 추가매수
def test_추가매수는_평단을_가중평균한다(book):
    """같은 종목을 여러 번 사는 게 정상이다. 사용자가 평단을 손으로 계산하면 안 된다."""
    book.upsert(ticker="AAPL", quantity=10, avg_cost=100.0)
    merged = book.add_lot("AAPL", quantity=10, price=200.0)

    assert merged["quantity"] == 20
    assert merged["avg_cost"] == pytest.approx(150.0)


def test_평단이_없던_종목에_추가매수하면_새_가격이_평단이_된다(book):
    book.upsert(ticker="AAPL", quantity=5, avg_cost=None)
    merged = book.add_lot("AAPL", quantity=5, price=120.0)
    assert merged["quantity"] == 10
    assert merged["avg_cost"] == pytest.approx(120.0)


def test_없던_종목에_추가매수하면_새로_생긴다(book):
    merged = book.add_lot("TSLA", quantity=3, price=250.0)
    assert merged["quantity"] == 3
    assert merged["avg_cost"] == pytest.approx(250.0)


# ------------------------------------------------------------------ 출처 동기화
def test_증권사_동기화는_그_출처만_교체한다(book):
    """수동 입력한 줄이 증권사 동기화로 사라지면 사용자 입력이 증발한다."""
    book.upsert(ticker="AAPL", quantity=1, source="manual")
    book.upsert(ticker="005930.KS", quantity=10, source="toss")

    info = book.replace_source("toss", [
        {"ticker": "000660.KS", "quantity": 5, "currency": "KRW"},
    ])

    assert "AAPL" in book.items                     # 수동 줄은 남는다
    assert "005930.KS" not in book.items            # 판 종목은 사라진다
    assert "000660.KS" in book.items
    assert info["removed"] == ["005930.KS"]


def test_동기화된_줄은_출처가_기록된다(book):
    book.replace_source("kis", [{"ticker": "AAPL", "quantity": 2}])
    assert book.items["AAPL"]["source"] == "kis"


def test_삭제는_해당_종목만_지운다(book):
    book.upsert(ticker="AAPL", quantity=1)
    book.upsert(ticker="TSLA", quantity=1)
    book.remove("AAPL")
    assert book.tickers == ["TSLA"]


# ------------------------------------------------------------------ 저장·복원
def test_저장하고_다시_읽으면_같다(book):
    book.upsert(ticker="AAPL", quantity=3, avg_cost=190.5)
    book.save()

    again = HoldingsBook.load(book.path)
    assert again.items["AAPL"]["quantity"] == 3
    assert again.items["AAPL"]["avg_cost"] == pytest.approx(190.5)


def test_파일이_없으면_빈_장부다(tmp_path):
    assert len(HoldingsBook.load(tmp_path / "none.json")) == 0


def test_깨진_파일은_조용히_비우지_않고_실패한다(tmp_path):
    """빈 장부로 대체하면 사용자 입력이 증발한 것처럼 보인다."""
    path = tmp_path / "holdings.json"
    path.write_text("{ 깨진 json", encoding="utf-8")
    with pytest.raises(Exception):
        HoldingsBook.load(path)


# ------------------------------------------------------------------ 평가
def test_평가액과_비중을_계산한다(book, monkeypatch):
    monkeypatch.setattr(config, "HOLDINGS_BASE_CURRENCY", "KRW")
    book.upsert(ticker="005930.KS", quantity=10, avg_cost=70000, currency="KRW")
    book.upsert(ticker="AAPL", quantity=1, avg_cost=100.0, currency="USD")

    val = book.valuation(prices={"005930.KS": 80000, "AAPL": 200.0},
                         fx={"KRW": 1.0, "USD": 1000.0})

    assert val["total_value"] == pytest.approx(800_000 + 200_000)
    rows = {r["ticker"]: r for r in val["rows"]}
    assert rows["005930.KS"]["weight"] == pytest.approx(0.8)
    assert rows["AAPL"]["weight"] == pytest.approx(0.2)


def test_손익은_평단_기준으로_계산된다(book):
    book.upsert(ticker="AAPL", quantity=10, avg_cost=100.0, currency="USD")
    val = book.valuation(prices={"AAPL": 150.0}, fx={"USD": 1.0})
    row = val["rows"][0]

    assert row["cost_basis"] == pytest.approx(1000.0)
    assert row["pnl"] == pytest.approx(500.0)
    assert row["pnl_pct"] == pytest.approx(50.0)


def test_평단이_없으면_손익을_지어내지_않는다(book):
    book.upsert(ticker="AAPL", quantity=10, avg_cost=None, currency="USD")
    row = book.valuation(prices={"AAPL": 150.0}, fx={"USD": 1.0})["rows"][0]

    assert row["market_value"] == pytest.approx(1500.0)
    assert row["cost_basis"] is None
    assert row["pnl"] is None


def test_시세를_못_받은_종목은_합산에서_빠지고_보고된다(book):
    """0 으로 채우면 총자산이 조용히 줄어든다."""
    book.upsert(ticker="AAPL", quantity=1, currency="USD")
    book.upsert(ticker="NOPRICE", quantity=1, currency="USD")

    val = book.valuation(prices={"AAPL": 100.0}, fx={"USD": 1.0})

    assert val["total_value"] == pytest.approx(100.0)
    assert val["unpriced"] == ["NOPRICE"]


def test_환율을_못_받은_통화는_합산에서_빠지고_보고된다(book, monkeypatch):
    monkeypatch.setattr(config, "HOLDINGS_BASE_CURRENCY", "KRW")
    book.upsert(ticker="7203.T", quantity=1, currency="JPY")
    book.upsert(ticker="005930.KS", quantity=1, currency="KRW")

    val = book.valuation(prices={"7203.T": 3000.0, "005930.KS": 70000.0},
                         fx={"KRW": 1.0})              # JPY 환율 없음

    assert val["total_value"] == pytest.approx(70000.0)
    assert val["unconverted"] == ["7203.T"]


def test_빈_장부의_평가액은_0이고_행도_없다(book):
    val = book.valuation(prices={}, fx={})
    assert val["total_value"] == 0
    assert val["rows"] == []


# ------------------------------------------------------------------ CSV 임포트
def csv_bytes(text, encoding="utf-8"):
    return text.encode(encoding)


def test_국내_증권사_형식을_읽는다():
    text = ("종목코드,종목명,보유수량,매입평균가격\n"
            "005930,삼성전자,10,70000\n"
            "000660,SK하이닉스,5,\"150,000\"\n")
    items, info = holdings_csv.parse(csv_bytes(text), resolve_market=False)

    assert len(items) == 2
    assert items[0]["ticker"] == "005930.KS"
    assert items[0]["quantity"] == 10
    assert items[1]["avg_cost"] == pytest.approx(150000)


def test_영문_컬럼도_읽는다():
    text = "Symbol,Quantity,AvgPrice\nAAPL,3,190.5\n"
    items, _ = holdings_csv.parse(csv_bytes(text), resolve_market=False)
    assert items[0] == {"ticker": "AAPL", "name": None, "quantity": 3.0,
                        "avg_cost": pytest.approx(190.5), "currency": "USD",
                        "account": None}


def test_cp949_인코딩도_읽는다():
    """국내 증권사 CSV 는 대부분 cp949 다."""
    text = "종목코드,종목명,보유수량\n005930,삼성전자,7\n"
    items, _ = holdings_csv.parse(csv_bytes(text, "cp949"), resolve_market=False)
    assert items[0]["quantity"] == 7
    assert items[0]["name"] == "삼성전자"


def test_엑셀이_날린_앞자리_0을_되살린다():
    """엑셀에서 열면 005930 이 5930 이 된다 — 그대로 두면 시세를 못 받는다."""
    text = "종목코드,보유수량\n5930,10\n"
    items, _ = holdings_csv.parse(csv_bytes(text), resolve_market=False)
    assert items[0]["ticker"] == "005930.KS"


def test_수량이_0인_줄은_건너뛰고_보고한다():
    text = "종목코드,보유수량\n005930,0\n000660,5\n"
    items, info = holdings_csv.parse(csv_bytes(text), resolve_market=False)
    assert len(items) == 1
    assert info["skipped"][0]["ticker"] == "005930"


def test_필수_컬럼이_없으면_있는_컬럼을_알려준다():
    """'형식이 잘못됐습니다'만 띄우면 사용자가 고칠 방법이 없다."""
    text = "이름,값\n삼성전자,10\n"
    with pytest.raises(ValueError) as e:
        holdings_csv.parse(csv_bytes(text), resolve_market=False)
    msg = str(e.value)
    assert "종목코드" in msg and "이름" in msg      # 없는 것과 있는 것을 모두 알려준다


def test_탭_구분_파일도_읽는다():
    text = "종목코드\t보유수량\n005930\t10\n"
    items, _ = holdings_csv.parse(csv_bytes(text), resolve_market=False)
    assert items[0]["quantity"] == 10


def test_숫자_표기_변형들을_해석한다():
    assert holdings_csv.parse_number("1,234.5") == pytest.approx(1234.5)
    assert holdings_csv.parse_number("(1,234)") == pytest.approx(-1234)
    assert holdings_csv.parse_number("") is None
    assert holdings_csv.parse_number("-") is None
    assert holdings_csv.parse_number(None) is None


def test_빈_파일은_명확히_실패한다():
    with pytest.raises(ValueError, match="빈 파일"):
        holdings_csv.parse(b"", resolve_market=False)


# ------------------------------------------------------- 봇 장부와의 분리
def test_보유_장부는_봇_상태파일과_다른_경로를_쓴다():
    """같은 파일을 쓰면 사이클 실행이 사용자의 보유 입력을 덮어쓴다."""
    assert config.HOLDINGS_FILE != config.STATE_FILE
    assert config.HOLDINGS_FILE.name == "holdings.json"


def test_보유_장부는_주문을_만들지_않는다():
    """이 모듈에 주문·체결 관련 함수가 생기면 안전선이 무너진다."""
    public = {n for n in dir(HoldingsBook) if not n.startswith("_")}
    forbidden = {"rebalance", "order", "orders", "execute", "buy", "sell", "submit"}
    assert not (public & forbidden), f"주문 관련 메서드가 생겼다: {public & forbidden}"


def test_저장_파일에_주문_필드가_없다(book):
    book.upsert(ticker="AAPL", quantity=1)
    book.save()
    data = json.loads(book.path.read_text(encoding="utf-8"))
    assert set(data) == {"updated_at", "base_currency", "holdings"}
