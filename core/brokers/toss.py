"""
토스증권 Open API 어댑터 — 조회 전용.

문서: https://developers.tossinvest.com

■ 확인된 것 (공식 OpenAPI 스펙 기준)
- 토큰: `POST {base}/oauth2/token`, `application/x-www-form-urlencoded`,
  `grant_type=client_credentials` + `client_id` + `client_secret`
  → `access_token` · `token_type=Bearer` · `expires_in=86400`(24시간)
- 계좌: `GET {base}/api/v1/accounts` → 응답의 `accountSeq` 가 다음 헤더 값이 된다
- 보유: `GET {base}/api/v1/holdings`, 헤더 `Authorization: Bearer …` +
  `X-Tossinvest-Account: {accountSeq}`

■ 확인하지 못한 것 — 보유 응답의 필드명
공개된 스펙 문서에서 `holdings` 응답 스키마를 읽을 수 없었다. 그래서 흔한 이름
후보를 순서대로 찾아보고, **하나도 맞지 않으면 실제 응답에 있던 키 목록을 담아
오류를 낸다.** 조용히 빈 목록을 돌려주면 장부가 통째로 지워지고, 아무 필드나
집어 쓰면 수량과 평단이 뒤바뀐 채 그럴듯하게 표시된다. 둘 다 더 나쁘다.

실제 계정으로 한 번 돌려보고 오류에 찍힌 키 이름을 아래 후보 목록에 추가하면
바로 맞는다.

■ 주문 API 는 여기에 없다
토스 Open API 에는 주문 생성·정정·취소가 있지만 이 파일에 옮겨 적지 않는다.
자세한 이유는 `core/brokers/__init__.py` 독스트링 참고.
"""
import logging
import time

from .. import config
from .base import BrokerAdapter, BrokerError

log = logging.getLogger("broker.toss")

# 보유 응답에서 찾아볼 필드 이름 후보 (앞에 있을수록 우선)
_F_SYMBOL = ("stockCode", "code", "symbol", "isinCode", "shortCode", "productCode")
_F_NAME = ("stockName", "name", "productName", "koreanName", "companyName")
_F_QTY = ("quantity", "holdingQuantity", "balanceQuantity", "qty", "shares")
_F_AVG = ("averagePrice", "avgPrice", "purchasePrice", "averageBuyPrice",
          "buyPrice", "avgUnitPrice")
_F_CCY = ("currency", "currencyCode", "ccy")
_F_MARKET = ("market", "marketCode", "exchange", "exchangeCode", "nationCode")

# 응답 본문에서 목록이 들어 있을 만한 키
_LIST_KEYS = ("holdings", "items", "data", "result", "results", "list", "content")


def _pick(row, names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def _find_list(payload):
    """응답에서 보유 목록 배열을 찾아낸다. 못 찾으면 None."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in _LIST_KEYS:
        v = payload.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):                     # {"data": {"items": [...]}} 형태
            inner = _find_list(v)
            if inner is not None:
                return inner
    return None


class TossAdapter(BrokerAdapter):
    name = "toss"
    label = "토스증권"
    docs_url = "https://developers.tossinvest.com"
    required_config = (("TOSS_CLIENT_ID", "TOSS_CLIENT_ID"),
                       ("TOSS_CLIENT_SECRET", "TOSS_CLIENT_SECRET"))

    def __init__(self):
        self._token = None
        self._token_expires_at = 0.0

    # ---------------------------------------------------------------- 인증
    def _access_token(self):
        """토큰은 메모리에만 둔다. 파일로 저장하지 않는다."""
        if self._token and time.time() < self._token_expires_at:
            return self._token

        self.require_config()
        data = self._request(
            f"{config.TOSS_API_BASE}/oauth2/token",
            method="POST",
            form={
                "grant_type": "client_credentials",
                "client_id": config.TOSS_CLIENT_ID,
                "client_secret": config.TOSS_CLIENT_SECRET,
            },
        )
        token = data.get("access_token")
        if not token:
            raise BrokerError("토스증권 토큰 발급 응답에 access_token 이 없습니다")

        # 만료 60초 전에 새로 받는다 (경계에서 401 이 나지 않게)
        self._token = token
        self._token_expires_at = time.time() + max(int(data.get("expires_in", 86400)) - 60, 60)
        return token

    def _headers(self, account=None):
        h = {"Authorization": f"Bearer {self._access_token()}",
             "Accept": "application/json"}
        if account:
            h["X-Tossinvest-Account"] = str(account)
        return h

    # ---------------------------------------------------------------- 계좌
    def accounts(self):
        payload = self._request(f"{config.TOSS_API_BASE}/api/v1/accounts",
                                headers=self._headers())
        rows = _find_list(payload) or []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            seq = _pick(row, ("accountSeq", "accountNo", "accountNumber", "id"))
            if seq is None:
                continue
            out.append({"account_seq": str(seq),
                        "name": _pick(row, ("accountName", "name", "nickname")) or str(seq)})
        if not out:
            raise BrokerError(
                "토스증권 계좌를 찾지 못했습니다. 응답 키: "
                + ", ".join(sorted(payload)[:20] if isinstance(payload, dict) else ["(배열)"])
            )
        return out

    # ---------------------------------------------------------------- 보유
    def fetch_holdings(self, account=None):
        accounts = [{"account_seq": account}] if account else self.accounts()

        out, seen_keys, recognized = [], set(), False
        for acc in accounts:
            seq = acc["account_seq"]
            payload = self._request(f"{config.TOSS_API_BASE}/api/v1/holdings",
                                    headers=self._headers(seq))
            rows = _find_list(payload)
            if rows is None:
                raise BrokerError(
                    "토스증권 보유 응답에서 목록을 찾지 못했습니다. 최상위 키: "
                    + ", ".join(sorted(payload)[:20] if isinstance(payload, dict) else ["(배열 아님)"])
                )

            for row in rows:
                if not isinstance(row, dict):
                    continue
                seen_keys.update(row.keys())
                sym = _pick(row, _F_SYMBOL)
                qty = self._num(_pick(row, _F_QTY))
                # 필드를 읽어냈는지와, 그 값이 보유인지는 다른 문제다.
                # 전량 매도해 수량이 0 인 계좌를 '필드명 불일치'로 오진하면 안 된다.
                if sym is not None and qty is not None:
                    recognized = True
                if not sym or not qty or qty <= 0:
                    continue
                out.append({
                    "ticker": self._to_yahoo(sym, _pick(row, _F_MARKET),
                                             _pick(row, _F_CCY)),
                    "name": _pick(row, _F_NAME),
                    "quantity": qty,
                    "avg_cost": self._num(_pick(row, _F_AVG)),
                    "currency": (_pick(row, _F_CCY) or "").upper() or None,
                    "account": str(seq),
                })

        if not out and seen_keys and not recognized:
            # 줄은 있는데 종목코드·수량 필드를 하나도 못 읽었다 = 이름 추정이 틀렸다.
            # 사용자가 이 목록을 그대로 알려주면 후보에 추가해 바로 고칠 수 있다.
            raise BrokerError(
                "토스증권 보유 항목을 해석하지 못했습니다 (종목코드/수량 필드를 못 찾음). "
                "응답에 있던 필드: " + ", ".join(sorted(seen_keys)[:30])
            )
        return out

    # ---------------------------------------------------------------- 심볼 변환
    @staticmethod
    def _to_yahoo(code, market, currency):
        """증권사 종목코드를 yfinance 심볼로. 장부·시세가 같은 표기를 써야 한다."""
        code = str(code).strip().upper()
        mk = str(market or "").upper()
        ccy = str(currency or "").upper()

        if code.endswith((".KS", ".KQ")):
            return code
        # 6자리 숫자 = 국내 종목. 코스닥 표시가 있으면 .KQ, 아니면 .KS
        if code.isdigit() and len(code) == 6:
            return code + (".KQ" if "KOSDAQ" in mk or "KQ" in mk else ".KS")
        if ccy == "KRW" and code.isdigit():
            return code + ".KS"
        return code                          # 해외는 티커 그대로 (AAPL 등)
