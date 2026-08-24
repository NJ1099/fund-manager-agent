"""
증권사 어댑터 인터페이스.

**메서드는 `fetch_holdings()` 하나뿐이다.** 주문에 해당하는 메서드를 여기에
추가하지 말 것 — 자세한 이유는 패키지 독스트링 참고.
"""
import json
import logging
import urllib.error
import urllib.request

from .. import config

log = logging.getLogger("broker")


class BrokerError(RuntimeError):
    """증권사 조회 실패. 빈 결과와 반드시 구분되어야 한다."""


class BrokerAdapter:
    name = "base"
    label = "(이름 없음)"
    docs_url = None
    required_config = ()          # (설정명, 사람이 읽는 이름) 튜플들

    # ---------------------------------------------------------------- 설정
    def missing_config(self):
        """비어 있는 필수 설정의 사람이 읽는 이름 목록."""
        return [label for key, label in self.required_config
                if not getattr(config, key, "")]

    def is_configured(self):
        return not self.missing_config()

    def require_config(self):
        missing = self.missing_config()
        if missing:
            raise BrokerError(
                f"{self.label} 설정이 없습니다: {', '.join(missing)}. "
                ".env 파일에 채워 넣으세요 (채팅이나 코드에 붙여넣지 마세요)."
            )

    # ---------------------------------------------------------------- 조회
    def fetch_holdings(self):
        """[{ticker, name, quantity, avg_cost, currency, account}] 를 돌려준다.

        실패하면 **빈 목록이 아니라 BrokerError** 를 던져야 한다. 빈 목록은
        '보유 종목 없음'이라는 뜻이고, 호출부가 그걸 믿고 장부를 비운다.
        """
        raise NotImplementedError

    # ---------------------------------------------------------------- 공통 HTTP
    def _request(self, url, method="GET", headers=None, body=None, form=None):
        """JSON 응답을 돌려준다. 오류 메시지에 자격증명을 넣지 않는다."""
        data = None
        headers = dict(headers or {})
        if form is not None:
            import urllib.parse
            data = urllib.parse.urlencode(form).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif body is not None:
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=config.BROKER_TIMEOUT_SEC) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise BrokerError(
                f"{self.label} 요청 실패 (HTTP {e.code})"
                + (f": {detail}" if detail else "")
            ) from None                     # 원 예외를 숨겨 URL·헤더 노출을 막는다
        except Exception as e:
            raise BrokerError(f"{self.label} 연결 실패: {type(e).__name__}") from None

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise BrokerError(f"{self.label} 응답을 해석하지 못했습니다") from None

    # ---------------------------------------------------------------- 유틸
    @staticmethod
    def _num(value, default=None):
        """증권사 응답의 숫자는 문자열로 오는 경우가 많다."""
        if value in (None, ""):
            return default
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return default
