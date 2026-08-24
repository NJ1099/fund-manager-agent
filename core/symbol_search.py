"""
종목 검색 · 시세 · 환율.

보유 종목을 손으로 넣으려면 티커를 알아야 하는데, 한국 종목은 `005930.KS` 처럼
접미사가 붙어서 외우기 어렵다. 이름으로 찾을 수 있어야 실제로 쓸 만해진다.

■ 왜 소스가 둘인가 (실측 결과)

- **Yahoo Finance** (`query2.finance.yahoo.com/v1/finance/search`)
  영문명·티커에 강하다. 그리고 검색 결과의 심볼을 그대로 `yf.download` 에 넘길 수
  있다 — 검색과 시세의 심볼 체계가 같다는 뜻이라 이게 기본 소스다.
  **한글 쿼리는 HTTP 400 으로 거부한다** (파라미터를 어떻게 바꿔도 마찬가지였다).
- **네이버 자동완성** (`ac.stock.naver.com/ac`)
  한글에 강하다. "삼성전자"는 물론 "애플"→AAPL, "테슬라"→TSLA 처럼 해외 종목도
  한국어로 찾아주고, 국내 ETF(KODEX·TIGER…)까지 나온다.

그래서 쿼리에 한글이 있으면 네이버를 먼저, 아니면 Yahoo 를 먼저 쓰고, 결과가
모자라면 다른 쪽으로 채운다. 한쪽이 죽어도 검색이 통째로 멈추지 않는다.

■ 네이버는 비공식 엔드포인트다
언제든 바뀔 수 있다. 그래서 **보조 경로가 항상 살아 있어야 하고**, 네이버 실패는
로그만 남기고 넘어간다. 다만 두 소스가 **모두** 실패하면 빈 목록이 아니라 예외를
던진다 — 빈 목록은 사용자에게 "그런 종목이 없다"로 읽히는데, 실제로는 네트워크
문제일 수 있기 때문이다.

■ 심볼 표기는 Yahoo 기준으로 통일한다
장부·시세·Kronos 가 전부 yfinance 심볼을 쓰므로, 네이버 결과도 `005930.KS` 형태로
바꿔서 돌려준다. 변환하지 않으면 검색으로 넣은 종목의 시세가 영영 안 잡힌다.
"""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("search")

YAHOO_URL = "https://query2.finance.yahoo.com/v1/finance/search"
NAVER_URL = "https://ac.stock.naver.com/ac"

# Yahoo 는 기본 파이썬 UA 를 거부한다. yfinance 도 같은 이유로 UA 를 바꿔 보낸다.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 우리가 다룰 수 있는 자산만 남긴다 (옵션·선물은 이 파이프라인의 대상이 아니다)
ALLOWED_TYPES = {"EQUITY", "ETF", "MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY"}

EXCHANGE_LABEL = {
    "KSC": "코스피", "KOE": "코스닥", "KDQ": "코스닥",
    "NMS": "나스닥", "NGM": "나스닥", "NYQ": "뉴욕", "PCX": "NYSE Arca",
    "TOR": "토론토", "LSE": "런던", "HKG": "홍콩", "TYO": "도쿄",
}

# 네이버 시장 코드 → yfinance 접미사
_NAVER_SUFFIX = {
    "KOSPI": ".KS", "KOSDAQ": ".KQ", "KONEX": ".KQ",
    "TOKYO": ".T", "HONGKONG": ".HK", "LONDON": ".L", "SHANGHAI": ".SS",
    "SHENZHEN": ".SZ",
}
# 미국 거래소는 접미사가 없다
_NAVER_NO_SUFFIX = {"NASDAQ", "NYSE", "AMEX", "NYSEARCA", "NYSE ARCA", "BATS"}

_HANGUL = re.compile(r"[가-힣]")


def _get_json(url, timeout=None):
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout or config.BROKER_TIMEOUT_SEC) as r:
        return json.loads(r.read().decode("utf-8"))


def naver_symbol(code, type_code, nation_code):
    """네이버 종목을 yfinance 심볼로 바꾼다."""
    code = (code or "").strip().upper()
    tc = (type_code or "").strip().upper()
    if not code:
        return None
    if (nation_code or "").upper() == "KOR":
        return code + _NAVER_SUFFIX.get(tc, ".KS")
    if tc in _NAVER_NO_SUFFIX:
        return code
    return code + _NAVER_SUFFIX.get(tc, "")


# ------------------------------------------------------------------ 소스별 검색
def _search_yahoo(query, limit):
    url = YAHOO_URL + "?" + urllib.parse.urlencode({
        "q": query, "quotesCount": max(limit * 2, 10), "newsCount": 0,
        "enableFuzzyQuery": "false", "quotesQueryId": "tss_match_phrase_query",
    })
    data = _get_json(url)

    out = []
    for item in data.get("quotes", []):
        qt = (item.get("quoteType") or "").upper()
        sym = item.get("symbol")
        if not sym or qt not in ALLOWED_TYPES:
            continue
        ex = item.get("exchange") or ""
        out.append({
            "symbol": sym,
            "name": item.get("shortname") or item.get("longname") or sym,
            "exchange": EXCHANGE_LABEL.get(ex, item.get("exchDisp") or ex),
            "type": qt,
            "currency": (item.get("currency") or _infer_currency(sym)).upper(),
            "source": "yahoo",
        })
    return out


def _search_naver(query, limit):
    url = NAVER_URL + "?" + urllib.parse.urlencode({
        "q": query, "target": "stock,index,marketindicator"})
    data = _get_json(url)

    out = []
    for item in data.get("items", []):
        # 응답 형태가 두 가지다: 평평한 목록, 또는 그룹({"items": [...]})
        rows = item.get("items") if isinstance(item.get("items"), list) else [item]
        for row in rows:
            sym = naver_symbol(row.get("code"), row.get("typeCode"), row.get("nationCode"))
            if not sym:
                continue
            out.append({
                "symbol": sym,
                "name": row.get("name") or sym,
                "exchange": row.get("typeName") or row.get("typeCode") or "",
                "type": "EQUITY",
                "currency": _infer_currency(sym),
                "source": "naver",
            })
            if len(out) >= limit * 2:
                return out
    return out


def _infer_currency(symbol):
    from .holdings import infer_currency
    return infer_currency(symbol)


# ------------------------------------------------------------------ 공개 API
def search(query, limit=10):
    """이름·티커로 종목을 찾는다. [{symbol, name, exchange, type, currency, source}]

    한글이 섞이면 네이버를 먼저 본다 (Yahoo 는 한글을 400 으로 거부한다).
    """
    q = (query or "").strip()
    if not q:
        return []

    order = ((_search_naver, _search_yahoo) if _HANGUL.search(q)
             else (_search_yahoo, _search_naver))

    results, seen, failures = [], set(), []
    for fn in order:
        if len(results) >= limit:
            break
        try:
            found = fn(q, limit)
        except urllib.error.HTTPError as e:
            failures.append(f"{fn.__name__} HTTP {e.code}")
            continue
        except Exception as e:                      # 네트워크·파싱 실패
            failures.append(f"{fn.__name__} {type(e).__name__}")
            continue
        for row in found:
            if row["symbol"] in seen:
                continue
            seen.add(row["symbol"])
            results.append(row)
            if len(results) >= limit:
                break

    if not results and failures:
        # 조용히 빈 목록을 주면 "그런 종목이 없다"로 읽힌다. 실패는 실패로 알린다.
        raise RuntimeError("종목 검색에 실패했습니다 (" + ", ".join(failures) + ")")
    return results


_KR_CACHE = {}


def resolve_korean(code):
    """국내 6자리 종목코드를 `.KS`/`.KQ` 중 맞는 쪽으로 붙여준다.

    증권사 응답과 CSV 는 시장 구분 없이 6자리 코드만 주는 경우가 많다. 코스닥
    종목에 `.KS` 를 붙이면 yfinance 가 시세를 못 찾고, 그러면 그 종목만 조용히
    평가액에서 빠진다. 네이버가 코드 검색으로 시장을 알려주므로 그걸 쓰고,
    실패하면 `.KS` 로 둔다(코스피가 더 흔하다).
    """
    code = str(code or "").strip().upper()
    if not (code.isdigit() and len(code) == 6):
        return code
    if code in _KR_CACHE:
        return _KR_CACHE[code]

    symbol = code + ".KS"
    try:
        for row in _search_naver(code, 5):
            if row["symbol"].startswith(code + "."):
                symbol = row["symbol"]
                break
    except Exception as e:                      # 네트워크 실패 시 기본값으로 둔다
        log.warning("%s 시장 구분 조회 실패(.KS 로 가정): %s", code, e)

    _KR_CACHE[code] = symbol
    return symbol


def quote(tickers):
    """현재가 조회. {ticker: 가격}. 못 받은 종목은 **키 자체를 넣지 않는다**.

    0 이나 None 으로 채우면 평가액이 조용히 틀어진다 — 호출부가 '가격 없음'을
    구분할 수 있어야 경고를 띄울 수 있다.
    """
    tickers = [t for t in dict.fromkeys(tickers) if t]
    if not tickers:
        return {}

    import pandas as pd
    import yfinance as yf

    out = {}
    try:
        data = yf.download(tickers, period="5d", interval="1d",
                           auto_adjust=True, progress=False, group_by="column")
    except Exception as e:
        log.error("시세 조회 실패: %s", e)
        return {}

    if data is None or len(data) == 0:
        return {}

    single = not isinstance(data.columns, pd.MultiIndex)
    for tk in tickers:
        try:
            col = data["Close"] if single else data["Close"][tk]
            series = col.dropna()
            if len(series):
                out[tk] = float(series.iloc[-1])
        except (KeyError, TypeError):
            continue

    missing = [t for t in tickers if t not in out]
    if missing:
        log.warning("시세를 받지 못한 종목: %s", missing)
    return out


def fx_rates(currencies, base=None):
    """기준통화 환산율. {통화: 1단위당 기준통화 금액}

    예) base=KRW 일 때 {"USD": 1350.0} 은 1 USD = 1350 KRW 를 뜻한다.
    구하지 못한 통화는 넣지 않는다 (valuation 이 합산에서 빼고 경고한다).
    """
    base = (base or config.HOLDINGS_BASE_CURRENCY).upper()
    wanted = {c.upper() for c in currencies if c} - {base}
    rates = {base: 1.0}
    if not wanted:
        return rates

    pairs = {f"{c}{base}=X": c for c in wanted}
    prices = quote(list(pairs))
    for sym, cur in pairs.items():
        if sym in prices:
            rates[cur] = prices[sym]

    missing = wanted - set(rates)
    if missing:
        log.warning("환율을 받지 못한 통화: %s", sorted(missing))
    return rates
