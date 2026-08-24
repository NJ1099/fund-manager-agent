"""
증권사 어댑터 테스트 — 네트워크를 쓰지 않는다.

가장 중요한 것은 **주문 경로가 존재하지 않는다**는 사실을 고정하는 것이다.
토스·KIS 모두 주문 API 를 제공하므로, 나중에 누군가 "편하니까" 추가할 수 있다.
이 프로젝트의 안전선은 거기서 무너진다.

그 다음으로 중요한 것은 **실패가 빈 결과로 둔갑하지 않는 것**이다. 조회 실패를
빈 목록으로 돌려주면 `replace_source` 가 멀쩡한 장부를 통째로 지운다.
"""
import pytest

from core import brokers, config
from core.brokers import BrokerError
from core.brokers.kis import KisAdapter
from core.brokers.toss import TossAdapter


# ------------------------------------------------------------------ 안전선
@pytest.mark.parametrize("adapter_cls", [TossAdapter, KisAdapter])
def test_어댑터에_주문_메서드가_없다(adapter_cls):
    """토스·KIS 모두 주문 API 가 있지만 이 코드에는 옮겨 적지 않는다."""
    names = {n.lower() for n in dir(adapter_cls) if not n.startswith("__")}
    forbidden = {"order", "place_order", "buy", "sell", "cancel", "amend",
                 "submit", "trade", "execute"}
    hit = names & forbidden
    assert not hit, f"주문 관련 메서드가 생겼다: {hit}"


@pytest.mark.parametrize("adapter_cls", [TossAdapter, KisAdapter])
def test_어댑터_소스에_주문_엔드포인트가_없다(adapter_cls):
    """경로 문자열만 있어도 나중에 이어 붙이기 쉬워진다. 아예 두지 않는다."""
    import inspect
    src = inspect.getsource(inspect.getmodule(adapter_cls))
    body = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    for bad in ("/orders", "order/cash", "order-cash", "v1/orders"):
        assert bad not in body, f"주문 엔드포인트 경로가 들어 있다: {bad}"


def test_레지스트리가_두_증권사를_노출한다():
    names = {b["name"] for b in brokers.available()}
    assert {"toss", "kis"} <= names


def test_모르는_증권사는_명확히_실패한다():
    with pytest.raises(BrokerError, match="알 수 없는 증권사"):
        brokers.get("wrong-broker")


# ------------------------------------------------------------------ 설정 검사
def test_설정이_없으면_무엇이_빠졌는지_알려준다(monkeypatch):
    monkeypatch.setattr(config, "TOSS_CLIENT_ID", "")
    monkeypatch.setattr(config, "TOSS_CLIENT_SECRET", "")
    a = TossAdapter()

    assert not a.is_configured()
    with pytest.raises(BrokerError) as e:
        a.require_config()
    assert "TOSS_CLIENT_ID" in str(e.value)
    assert ".env" in str(e.value)          # 어디에 넣어야 하는지도 알려준다


def test_KIS_계좌번호_형식을_검사한다(monkeypatch):
    monkeypatch.setattr(config, "KIS_APP_KEY", "k")
    monkeypatch.setattr(config, "KIS_APP_SECRET", "s")
    monkeypatch.setattr(config, "KIS_ACCOUNT", "1234")
    with pytest.raises(BrokerError, match="12345678-01"):
        KisAdapter()._account_parts()


def test_KIS_계좌번호를_두_부분으로_나눈다(monkeypatch):
    monkeypatch.setattr(config, "KIS_ACCOUNT", "12345678-01")
    assert KisAdapter()._account_parts() == ("12345678", "01")


# ------------------------------------------------------------------ 토스 파싱
@pytest.fixture
def toss(monkeypatch):
    monkeypatch.setattr(config, "TOSS_CLIENT_ID", "id")
    monkeypatch.setattr(config, "TOSS_CLIENT_SECRET", "secret")
    a = TossAdapter()
    a._token = "tok"
    a._token_expires_at = 1e12          # 만료 안 됨
    return a


def stub_requests(adapter, responses):
    """URL 조각 → 응답 매핑으로 _request 를 갈아끼운다."""
    calls = []

    def fake(url, method="GET", headers=None, body=None, form=None):
        calls.append({"url": url, "method": method, "headers": headers or {}})
        for frag, resp in responses.items():
            if frag in url:
                return resp
        raise AssertionError(f"예상 못한 요청: {url}")

    adapter._request = fake
    return calls


def test_토스_보유를_읽는다(toss):
    stub_requests(toss, {
        "/api/v1/accounts": {"result": [{"accountSeq": "A1", "accountName": "주식"}]},
        "/api/v1/holdings": {"result": [
            {"stockCode": "005930", "stockName": "삼성전자",
             "quantity": "10", "averagePrice": "70,000", "currency": "KRW"},
            {"stockCode": "AAPL", "stockName": "Apple",
             "quantity": "3", "averagePrice": "190.5", "currency": "USD"},
        ]},
    })

    items = toss.fetch_holdings()
    by = {i["ticker"]: i for i in items}

    assert by["005930.KS"]["quantity"] == 10
    assert by["005930.KS"]["avg_cost"] == pytest.approx(70000)
    assert by["AAPL"]["currency"] == "USD"
    assert by["AAPL"]["account"] == "A1"


def test_토스_계좌_헤더가_붙는다(toss):
    calls = stub_requests(toss, {
        "/api/v1/accounts": {"result": [{"accountSeq": "SEQ9"}]},
        "/api/v1/holdings": {"result": []},
    })
    toss.fetch_holdings()
    holdings_call = [c for c in calls if "/holdings" in c["url"]][0]
    assert holdings_call["headers"]["X-Tossinvest-Account"] == "SEQ9"


def test_토스_수량이_0인_항목은_보유가_아니다(toss):
    stub_requests(toss, {
        "/api/v1/accounts": {"result": [{"accountSeq": "A1"}]},
        "/api/v1/holdings": {"result": [{"stockCode": "AAPL", "quantity": "0"}]},
    })
    assert toss.fetch_holdings() == []


def test_토스_필드를_못_읽으면_실제_키를_알려준다(toss):
    """필드명 추정이 틀렸을 때 사용자가 알려줄 수 있어야 고칠 수 있다."""
    stub_requests(toss, {
        "/api/v1/accounts": {"result": [{"accountSeq": "A1"}]},
        "/api/v1/holdings": {"result": [{"완전히다른키": "x", "또다른키": 1}]},
    })
    with pytest.raises(BrokerError) as e:
        toss.fetch_holdings()
    assert "완전히다른키" in str(e.value)


def test_토스_목록을_못_찾으면_최상위_키를_알려준다(toss):
    stub_requests(toss, {
        "/api/v1/accounts": {"result": [{"accountSeq": "A1"}]},
        "/api/v1/holdings": {"unexpectedShape": {"nope": 1}},
    })
    with pytest.raises(BrokerError, match="목록을 찾지 못했습니다"):
        toss.fetch_holdings()


def test_토스_계좌가_없으면_실패한다(toss):
    stub_requests(toss, {"/api/v1/accounts": {"result": []}})
    with pytest.raises(BrokerError, match="계좌를 찾지 못했습니다"):
        toss.fetch_holdings()


@pytest.mark.parametrize("code,market,currency,expected", [
    ("005930", "KOSPI", "KRW", "005930.KS"),
    ("247540", "KOSDAQ", "KRW", "247540.KQ"),
    ("AAPL", "NASDAQ", "USD", "AAPL"),
    ("005930.KS", "", "KRW", "005930.KS"),      # 이미 붙어 있으면 그대로
])
def test_토스_심볼_변환(code, market, currency, expected):
    assert TossAdapter._to_yahoo(code, market, currency) == expected


# ------------------------------------------------------------------ 토큰 캐싱
def test_토큰은_만료_전까지_재사용된다(monkeypatch):
    """KIS 는 토큰 발급 횟수에 제한이 있다. 매 요청마다 받으면 막힌다."""
    monkeypatch.setattr(config, "KIS_APP_KEY", "k")
    monkeypatch.setattr(config, "KIS_APP_SECRET", "s")
    monkeypatch.setattr(config, "KIS_ACCOUNT", "12345678-01")

    a = KisAdapter()
    issued = []

    def fake(url, method="GET", headers=None, body=None, form=None):
        issued.append(url)
        return {"access_token": "T", "expires_in": 86400}

    a._request = fake
    assert a._access_token() == "T"
    assert a._access_token() == "T"
    assert len(issued) == 1, "토큰을 두 번 발급했다"


def test_토큰이_없으면_명확히_실패한다(monkeypatch):
    monkeypatch.setattr(config, "TOSS_CLIENT_ID", "id")
    monkeypatch.setattr(config, "TOSS_CLIENT_SECRET", "s")
    a = TossAdapter()
    a._request = lambda *args, **kw: {"no_token_here": 1}
    with pytest.raises(BrokerError, match="access_token"):
        a._access_token()


# ------------------------------------------------------------------ KIS 파싱
@pytest.fixture
def kis(monkeypatch):
    monkeypatch.setattr(config, "KIS_APP_KEY", "k")
    monkeypatch.setattr(config, "KIS_APP_SECRET", "s")
    monkeypatch.setattr(config, "KIS_ACCOUNT", "12345678-01")
    # 시장 구분 조회가 네트워크를 타지 않게 고정
    monkeypatch.setattr("core.symbol_search.resolve_korean", lambda c: f"{c}.KS")
    a = KisAdapter()
    a._token = "tok"
    a._token_expires_at = 1e12
    return a


def test_KIS_국내잔고를_읽는다(kis):
    stub_requests(kis, {
        "domestic-stock": {"rt_cd": "0", "output1": [
            {"pdno": "005930", "prdt_name": "삼성전자",
             "hldg_qty": "10", "pchs_avg_pric": "70000"},
        ]},
        "overseas-stock": {"rt_cd": "0", "output1": []},
    })
    items = kis.fetch_holdings()
    assert items[0]["ticker"] == "005930.KS"
    assert items[0]["currency"] == "KRW"
    assert items[0]["quantity"] == 10


def test_KIS_해외잔고도_읽는다(kis):
    stub_requests(kis, {
        "domestic-stock": {"rt_cd": "0", "output1": []},
        "overseas-stock": {"rt_cd": "0", "output1": [
            {"ovrs_pdno": "AAPL", "ovrs_item_name": "APPLE",
             "ovrs_cblc_qty": "3", "pchs_avg_pric": "190.5",
             "tr_crcy_cd": "USD", "ovrs_excg_cd": "NASD"},
        ]},
    })
    items = kis.fetch_holdings()
    assert items[0] == {"ticker": "AAPL", "name": "APPLE", "quantity": 3.0,
                        "avg_cost": pytest.approx(190.5), "currency": "USD",
                        "account": "12345678-01"}


def test_KIS_는_HTTP200_안의_오류코드를_잡는다(kis):
    """KIS 는 실패도 200 으로 준다. rt_cd 를 안 보면 오류가 빈 결과로 둔갑한다."""
    stub_requests(kis, {"domestic-stock": {"rt_cd": "1", "msg1": "권한이 없습니다"}})
    with pytest.raises(BrokerError, match="권한이 없습니다"):
        kis.fetch_holdings()


def test_해외조회_실패해도_국내분은_살린다(kis):
    """해외 계좌가 없는 사용자가 국내 보유까지 못 받으면 안 된다."""
    def fake(url, method="GET", headers=None, body=None, form=None):
        if "domestic-stock" in url:
            return {"rt_cd": "0", "output1": [
                {"pdno": "005930", "hldg_qty": "1", "pchs_avg_pric": "70000"}]}
        raise BrokerError("해외 계좌 없음")

    kis._request = fake
    items = kis.fetch_holdings()
    assert len(items) == 1
    assert items[0]["ticker"] == "005930.KS"


# ------------------------------------------------------------------ 자격증명 노출
def test_오류_메시지에_자격증명이_들어가지_않는다(monkeypatch):
    """오류는 대시보드 화면과 로그에 그대로 뜬다."""
    import urllib.error

    secret = "SUPER-SECRET-VALUE"
    monkeypatch.setattr(config, "TOSS_CLIENT_ID", "id")
    monkeypatch.setattr(config, "TOSS_CLIENT_SECRET", secret)

    def boom(*args, **kwargs):
        raise urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    a = TossAdapter()
    with pytest.raises(BrokerError) as e:
        a._access_token()
    assert secret not in str(e.value)
