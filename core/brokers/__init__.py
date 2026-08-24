"""
증권사 어댑터 — **조회 전용**.

■ 이 패키지에는 주문 기능이 없다. 없는 게 설계다.

이 프로젝트의 안전선은 "브로커 커넥터 없음"이었다. 실계좌 조회를 붙이면서도
그 선을 지키는 방법은, 주문 경로를 **만들지 않는 것**이다. 어댑터 인터페이스
(`base.BrokerAdapter`)에는 `fetch_holdings()` 하나뿐이고, 주문·취소·정정에
해당하는 메서드는 이름조차 없다.

토스증권 Open API 에는 주문 생성/정정/취소 엔드포인트가 실제로 존재한다
(문서 기준 42개 중 3개). **그 경로를 이 코드에 옮겨 적지 말 것.** 매매를 하고
싶으면 증권사 앱을 쓰면 된다. 이 프로그램이 대신 주문을 내야 할 이유가 없다.

■ 키 취급

- 자격증명은 `.env` 에서만 읽는다 (`core/config.py` 경유). 코드·설정 파일에
  적지 않는다.
- 토큰은 메모리에만 둔다. 파일로 저장하지 않는다.
- 오류 메시지에 자격증명을 넣지 않는다 — 대시보드 화면과 로그에 그대로 뜬다.

■ 실패는 실패로 알린다

조회에 실패했을 때 빈 목록을 돌려주면 호출부가 "보유 종목이 없다"로 읽고,
`replace_source` 가 멀쩡한 장부를 지워버린다. 그래서 실패는 반드시 예외다.
"""
from .base import BrokerAdapter, BrokerError

_REGISTRY = {}


def register(name, factory):
    _REGISTRY[name] = factory


def available():
    """설정이 갖춰져 실제로 쓸 수 있는 증권사 목록."""
    out = []
    for name, factory in sorted(_REGISTRY.items()):
        try:
            adapter = factory()
            out.append({
                "name": name,
                "label": adapter.label,
                "configured": adapter.is_configured(),
                "missing": adapter.missing_config(),
                "docs": adapter.docs_url,
            })
        except Exception as e:                       # 어댑터 자체가 깨져도 목록은 나와야 한다
            out.append({"name": name, "label": name, "configured": False,
                        "missing": [f"어댑터 오류: {e}"], "docs": None})
    return out


def get(name):
    factory = _REGISTRY.get(name)
    if factory is None:
        raise BrokerError(f"알 수 없는 증권사 '{name}'. "
                          f"가능: {', '.join(sorted(_REGISTRY)) or '(없음)'}")
    return factory()


# 어댑터 등록 (임포트 순서상 아래에 둔다)
from .toss import TossAdapter          # noqa: E402
from .kis import KisAdapter            # noqa: E402

register("toss", TossAdapter)
register("kis", KisAdapter)

__all__ = ["BrokerAdapter", "BrokerError", "available", "get", "register",
           "TossAdapter", "KisAdapter"]
