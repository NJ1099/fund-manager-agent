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

        self._token = token
        self._token_expires_at = time.time() + max(int(data.get("expires_in", 86400)) - 60, 60)
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
    def _query(self, path, tr_id, params):
        import urllib.parse
        url = f"{config.KIS_API_BASE}{path}?" + urllib.parse.urlencode(params)
        data = self._request(url, headers=self._headers(tr_id))

        # KIS 는 HTTP 200 으로 오류를 돌려준다. rt_cd 가 "0" 이 아니면 실패다.
        if str(data.get("rt_cd", "0")) != "0":
            raise BrokerError(
                f"한국투자증권 조회 실패: {data.get('msg1') or data.get('msg_cd') or '사유 미상'}"
            )
        rows = data.get("output1")
        return rows if isinstance(rows, list) else []

    def fetch_holdings(self):
        cano, prdt = self._account_parts()
        out = []

        # --- 국내주식 ---
        rows = self._query(
            "/uapi/domestic-stock/v1/trading/inquire-balance", TR_DOMESTIC_BALANCE,
            {"CANO": cano, "ACNT_PRDT_CD": prdt, "AFHR_FLPR_YN": "N",
             "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
             "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
             "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
        )
        for r in rows:
            qty = self._num(r.get("hldg_qty"))
            code = (r.get("pdno") or "").strip()
            if not code or not qty or qty <= 0:
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

        # --- 해외주식 ---
        try:
            rows = self._query(
                "/uapi/overseas-stock/v1/trading/inquire-balance", TR_OVERSEAS_BALANCE,
                {"CANO": cano, "ACNT_PRDT_CD": prdt, "OVRS_EXCG_CD": "NASD",
                 "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""},
            )
        except BrokerError as e:
            # 해외 계좌가 없거나 권한이 없을 수 있다. 국내분까지 버릴 이유는 없다.
            log.warning("해외주식 잔고 조회를 건너뜁니다: %s", e)
            rows = []

        for r in rows:
            qty = self._num(r.get("ovrs_cblc_qty"))
            code = (r.get("ovrs_pdno") or "").strip().upper()
            if not code or not qty or qty <= 0:
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
