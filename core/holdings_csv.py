"""
CSV 로 보유 종목 가져오기.

증권사 API 가 없거나(신청 대기 중이거나) 여러 계좌를 합치고 싶을 때 쓰는 경로다.
대부분의 증권사 앱·HTS 가 잔고를 CSV/엑셀로 내려받게 해주므로, 실무적으로는
이쪽이 API 보다 먼저 쓰이게 된다.

■ 컬럼 이름은 증권사마다 다르다
"종목코드"·"종목번호"·"단축코드"·"Symbol"… 전부 같은 뜻이다. 그래서 고정된
포맷을 요구하지 않고 **후보 이름으로 찾아낸다**. 못 찾으면 어떤 컬럼이 있었는지
알려준다 — "형식이 잘못됐습니다"만 띄우면 사용자가 고칠 방법이 없다.

■ 인코딩
국내 증권사 CSV 는 대부분 `cp949`(euc-kr)다. UTF-8 로 먼저 읽고 실패하면 cp949 로
다시 읽는다. BOM(`utf-8-sig`)도 흔하다.

■ 숫자
"1,234", "1 234", "1,234.56", "(1,234)"(음수) 같은 표기가 섞여 들어온다.
수량이 0 이하인 줄은 보유가 아니므로 조용히 건너뛴다.
"""
import csv
import io
import logging
import re

from . import symbol_search
from .holdings import infer_currency, normalize_ticker

log = logging.getLogger("holdings.csv")

# 컬럼 이름 후보 (소문자·공백제거 후 비교)
_C_TICKER = ("종목코드", "종목번호", "단축코드", "표준코드", "코드", "티커", "심볼",
             "symbol", "ticker", "code", "stockcode", "isin")
_C_NAME = ("종목명", "상품명", "종목", "회사명", "name", "stockname", "productname",
           "description")
_C_QTY = ("보유수량", "잔고수량", "수량", "주식수", "보유주식수", "체결수량",
          "quantity", "qty", "shares", "balance", "position")
_C_AVG = ("매입평균가격", "매입단가", "평균단가", "평단가", "매입가", "평균매입가",
          "취득단가", "avgprice", "averageprice", "cost", "avgcost", "purchaseprice",
          "unitcost")
_C_CCY = ("통화", "통화코드", "currency", "ccy")
_C_ACC = ("계좌", "계좌번호", "account", "accountno")

_NUM_RE = re.compile(r"[^0-9.\-]")


def _norm_key(s):
    return re.sub(r"[\s_\-()]", "", str(s or "")).lower()


def _find_column(header, candidates):
    """헤더에서 후보와 맞는 컬럼명을 찾는다. 완전일치 → 부분일치 순."""
    norm = {_norm_key(h): h for h in header if h}
    for c in candidates:
        if c in norm:
            return norm[c]
    for c in candidates:
        for k, original in norm.items():
            if c in k:
                return original
    return None


def parse_number(value):
    """'1,234.5' · '(1,234)' · '1 234' → float. 해석 불가면 None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = _NUM_RE.sub("", s)
    if s in ("", "-", "."):
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negative else n


def decode(raw_bytes):
    """국내 CSV 인코딩을 순서대로 시도한다."""
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 인코딩을 알 수 없습니다 (utf-8·cp949 모두 실패)")


def _sniff(text):
    """구분자 추정. 국내 CSV 는 쉼표가 기본이지만 탭·세미콜론도 나온다."""
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def parse(text_or_bytes, resolve_market=True):
    """CSV 를 보유 항목 목록으로. 반환: (항목 리스트, 진단 dict)

    resolve_market=True 면 국내 6자리 코드의 코스피/코스닥을 조회해 붙인다
    (네트워크를 쓴다. 테스트에서는 끄면 된다).
    """
    text = decode(text_or_bytes) if isinstance(text_or_bytes, (bytes, bytearray)) else text_or_bytes
    if not text.strip():
        raise ValueError("빈 파일입니다")

    reader = csv.DictReader(io.StringIO(text), delimiter=_sniff(text))
    header = reader.fieldnames or []
    if not header:
        raise ValueError("헤더 줄을 찾지 못했습니다")

    col_ticker = _find_column(header, _C_TICKER)
    col_qty = _find_column(header, _C_QTY)
    if not col_ticker or not col_qty:
        missing = []
        if not col_ticker:
            missing.append("종목코드")
        if not col_qty:
            missing.append("보유수량")
        raise ValueError(
            f"필요한 컬럼을 찾지 못했습니다: {', '.join(missing)}. "
            f"파일에 있던 컬럼: {', '.join(str(h) for h in header if h)}"
        )

    col_name = _find_column(header, _C_NAME)
    col_avg = _find_column(header, _C_AVG)
    col_ccy = _find_column(header, _C_CCY)
    col_acc = _find_column(header, _C_ACC)

    items, skipped = [], []
    for lineno, row in enumerate(reader, start=2):
        raw_code = (row.get(col_ticker) or "").strip()
        if not raw_code:
            continue
        qty = parse_number(row.get(col_qty))
        if qty is None or qty <= 0:
            skipped.append({"line": lineno, "ticker": raw_code, "reason": "수량이 없거나 0 이하"})
            continue

        ticker = _to_symbol(raw_code, resolve_market)
        currency = (row.get(col_ccy) or "").strip().upper() if col_ccy else ""
        items.append({
            "ticker": ticker,
            "name": (row.get(col_name) or "").strip() or None if col_name else None,
            "quantity": qty,
            "avg_cost": parse_number(row.get(col_avg)) if col_avg else None,
            "currency": currency or infer_currency(ticker),
            "account": (row.get(col_acc) or "").strip() or None if col_acc else None,
        })

    return items, {
        "columns_used": {"ticker": col_ticker, "quantity": col_qty, "name": col_name,
                          "avg_cost": col_avg, "currency": col_ccy, "account": col_acc},
        "parsed": len(items),
        "skipped": skipped,
    }


def _to_symbol(raw_code, resolve_market):
    """CSV 의 종목코드를 yfinance 심볼로."""
    code = normalize_ticker(raw_code)
    # 엑셀이 앞의 0 을 날려 '5930' 처럼 되는 일이 흔하다 — 6자리로 되돌린다
    if code.isdigit() and len(code) < 6:
        code = code.zfill(6)
    if code.isdigit() and len(code) == 6:
        return symbol_search.resolve_korean(code) if resolve_market else code + ".KS"
    return code
