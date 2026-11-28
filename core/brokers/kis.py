"""
한국투자증권(KIS) Open API 어댑터 — 조회 전용.

문서: https://apiportal.koreainvestment.com

■ 흐름
1. `POST {base}/oauth2/tokenP` — JSON 본문에 `grant_type=client_credentials`,
   `appkey`, `appsecret` → `access_token`, `expires_in`
2. 국내 잔고: `GET {base}/uapi/domestic-stock/v1/trading/inquire-balance`
   헤더 `tr_id: TTTC8434R` (실전) — 응답 `output1` 이 종목 배열
3. 해외 잔고: `GET {base}/uapi/overseas-stock/v1/trading/inquire-balance`
   헤더 `tr_id: TTTS3012R` — 응답 `output1`

계좌번호는 `12345678-01` 형태이며 앞 8자리(CANO)와 뒤 2자리(ACNT_PRDT_CD)로 나눠 보낸다.

■ 토큰 발급 횟수 제한
KIS 는 토큰 발급을 자주 하면 막는다(하루 호출 제한이 있다). 그래서 만료 전까지
메모리에 들고 재사용한다. 파일로는 저장하지 않는다 — 키에 준하는 값이기 때문이다.

■ 주문 API 는 여기에 없다
KIS 에는 주문 엔드포인트가 있지만 이 파일에 옮겨 적지 않는다.
자세한 이유는 `core/brokers/__init__.py` 독스트링 참고.
"""
import logging
import time

from .. import config, symbol_search
from .base import BrokerAdapter, BrokerError

log = logging.getLogger("broker.kis")

TR_DOMESTIC_BALANCE = "TTTC8434R"       # 국내주식 잔고조회 (실전)
TR_OVERSEAS_BALANCE = "TTTS3012R"       # 해외주식 잔고조회 (실전)

# 잔고 조회는 거래소마다 한 번씩이다. NASD 만 보면 뉴욕·아멕스 보유가
# '팔린 종목'으로 처리되어 장부에서 지워진다.
# (거래소 코드, 조회 통화)
_OVERSEAS_MARKETS = (
    ("NASD", "USD"),
    ("NYSE", "USD"),
    ("AMEX", "USD"),
    ("SEHK", "HKD"),
    ("TKSE", "JPY"),
    ("SHAA", "CNY"),
    ("SZAA", "CNY"),
)

# 연속 조회가 같은 커서를 반복해도 끝나게 하는 상한.
_MAX_PAGES = 8

# 해외 거래소 코드 → yfinance 접미사 (미국은 접미사 없음)
_EXCHANGE_SUFFIX = {
    "NASD": "", "NAS": "", "NYSE": "", "AMEX": "", "NYS": "", "AMS": "",
    "TKSE": ".T", "SEHK": ".HK", "HKS": ".HK", "SHAA": ".SS", "SZAA": ".SZ",
}


class KisAdapter(BrokerAdapter):
    name = "kis"
    label = "한국투자증권"
    docs_url = "https://apiportal.koreainvestment.com"
    required_config = (("KIS_APP_KEY", "KIS_APP_KEY"),
                       ("KIS_APP_SECRET", "KIS_APP_SECRET"),
                       ("KIS_ACCOUNT", "KIS_ACCOUNT (예: 12345678-01)"))

    def __init__(self):
        self._token = None
        self._token_expires_at = 0.0

    # ---------------------------------------------------------------- 인증
    def _access_token(self):
        if self._token and time.time() < self._token_expires_at:
            return self._token

        self.require_config()
        data = self._request(
            f"{config.KIS_API_BASE}/oauth2/tokenP",
            method="POST",
            body={"grant_type": "client_credentials",
                  "appkey": config.KIS_APP_KEY,
                  "appsecret": config.KIS_APP_SECRET},
        )
        token = data.get("access_token")
        if not token:
            raise BrokerError("한국투자증권 토큰 발급 응답에 access_token 이 없습니다")

        try:
            ttl = int(data.get("expires_in", 86400))
        except (TypeError, ValueError):
            ttl = 86400
        self._token = token
        self._token_expires_at = time.time() + max(ttl - 60, 60)
        return token

    def _headers(self, tr_id):
        return {
            "authorization": f"Bearer {self._access_token()}",
            "appkey": config.KIS_APP_KEY,
            "appsecret": config.KIS_APP_SECRET,
            "tr_id": tr_id,
            "custtype": "P",                 # 개인
            "Accept": "application/json",
        }

    def _account_parts(self):
        raw = (config.KIS_ACCOUNT or "").strip()
        digits = raw.replace("-", "")
        if len(digits) < 10:
            raise BrokerError(
                "KIS_ACCOUNT 형식이 올바르지 않습니다. '12345678-01' 처럼 "
                "종합계좌 8자리와 상품코드 2자리를 넣어주세요."
            )
        return digits[:8], digits[8:10]

    # ---------------------------------------------------------------- 조회
    def _collect(self, path, tr_id, params, fk_param, nk_param):
        """한 조회를 연속조회로 이어 붙인다.

        커서가 비어 있거나 같은 커서가 반복되면 멈춘다. 상한을 넘기면 그때까지
        받은 줄만 돌려주고, 잘렸다는 사실을 예외로 알린다 — 조용히 자르면
        뒤에 있던 보유가 '매도됨'으로 장부에서 지워진다.
        """
        import urllib.parse

        params = dict(params)
        rows, seen = [], set()
        for page in range(_MAX_PAGES):
            url = f"{config.KIS_API_BASE}{path}?" + urllib.parse.urlencode(params)
            data = self._request(url, headers=self._headers(tr_id))
            # KIS 는 HTTP 200 으로 오류를 돌려준다. rt_cd 가 "0" 이 아니면 실패다.
            if str(data.get("rt_cd", "0")) != "0":
                msg = data.get("msg1") or data.get("msg_cd") or "사유 미상"
                raise BrokerError(f"한국투자증권 조회 실패: {self._redact(str(msg))}")
            page_rows = data.get("output1")
            if isinstance(page_rows, list):
                rows.extend(page_rows)
            fk = str(data.get(fk_param.lower()) or data.get(fk_param) or "").strip()
            nk = str(data.get(nk_param.lower()) or data.get(nk_param) or "").strip()
            cursor = (fk, nk)
            if not fk and not nk:
                return rows
            if cursor in seen:
                return rows
            seen.add(cursor)
            params[fk_param] = fk
            params[nk_param] = nk
            if page == _MAX_PAGES - 1:
                raise BrokerError(
                    "한국투자증권 잔고가 한 번에 받지 못할 만큼 깁니다. "
                    "장부를 일부만 갈아끼우면 나머지가 지워지므로 반영하지 않았습니다."
                )
        return rows

    def _row_signature(self, rows, code_key, qty_key):
        return tuple(sorted(
            (str(r.get(code_key) or "").strip().upper(),
             str(r.get(qty_key) or "").strip())
            for r in rows if isinstance(r, dict)
        ))

    def fetch_holdings(self):
        cano, prdt = self._account_parts()
        out = []

        # --- 국내주식 ---
        rows = self._collect(
            "/uapi/domestic-stock/v1/trading/inquire-balance", TR_DOMESTIC_BALANCE,
            {"CANO": cano, "ACNT_PRDT_CD": prdt, "AFHR_FLPR_YN": "N",
             "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
             "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
             "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
            "CTX_AREA_FK100", "CTX_AREA_NK100",
        )
        for r in rows:
            qty = self._num(r.get("hldg_qty"))
            code = (r.get("pdno") or "").strip()
            if not code or not (qty and qty > 0):
                continue
            out.append({
                # 응답에 코스피/코스닥 구분이 없다. 코스닥에 .KS 를 붙이면 시세가
                # 안 잡혀 그 종목만 조용히 평가에서 빠지므로 시장을 조회해 붙인다.
                "ticker": symbol_search.resolve_korean(code),
                "name": (r.get("prdt_name") or "").strip() or None,
                "quantity": qty,
                "avg_cost": self._num(r.get("pchs_avg_pric")),
                "currency": "KRW",
                "account": f"{cano}-{prdt}",
            })

        # --- 해외주식 (거래소별. 한 곳만 보면 나머지가 장부에서 사라진다) ---
        rows = []
        seen_sig = set()
        for exch, ccy in _OVERSEAS_MARKETS:
            try:
                part = self._collect(
                    "/uapi/overseas-stock/v1/trading/inquire-balance", TR_OVERSEAS_BALANCE,
                    {"CANO": cano, "ACNT_PRDT_CD": prdt, "OVRS_EXCG_CD": exch,
                     "TR_CRCY_CD": ccy, "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""},
                    "CTX_AREA_FK200", "CTX_AREA_NK200",
                )
            except BrokerError as e:
                # 연속조회가 상한에서 끊기면 그 거래소만 빼는 순간 빠진 종목이 지워진다.
                # 동기화 전체를 실패시켜 기존 장부를 유지한다.
                if "한 번에 받지 못할" in str(e):
                    raise
                # 그 거래소 계좌가 없거나 권한이 없을 수 있다. 나머지까지 버릴 이유는 없다.
                log.warning("해외주식 잔고(%s) 조회를 건너뜁니다: %s", exch, e)
                continue
            sig = self._row_signature(part, "ovrs_pdno", "ovrs_cblc_qty")
            if sig and sig in seen_sig:
                # 거래소 코드를 무시하고 같은 잔고를 반복해 주는 응답이면 수량이 곱해진다.
                log.warning("해외주식 잔고(%s) 가 앞선 거래소와 동일해 건너뜁니다", exch)
                continue
            if sig:
                seen_sig.add(sig)
            rows.extend(part)

        for r in rows:
            qty = self._num(r.get("ovrs_cblc_qty"))
            code = (r.get("ovrs_pdno") or "").strip().upper()
            if not code or not (qty and qty > 0):
                continue
            suffix = _EXCHANGE_SUFFIX.get((r.get("ovrs_excg_cd") or "").strip().upper(), "")
            out.append({
                "ticker": code + suffix,
                "name": (r.get("ovrs_item_name") or "").strip() or None,
                "quantity": qty,
                "avg_cost": self._num(r.get("pchs_avg_pric")),
                "currency": (r.get("tr_crcy_cd") or "USD").strip().upper(),
                "account": f"{cano}-{prdt}",
            })

        return out
