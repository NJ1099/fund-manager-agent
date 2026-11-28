"""로컬 대시보드가 다른 웹페이지에 열리지 않는지.

인증이 없는 서버라, 브라우저에 열린 다른 사이트가 form POST 로 사이클이나
유료 논평을 대신 실행할 수 있다. DNS rebinding 이면 Host 가 루프백이 아니다.
둘 다 거절해야 한다. 네트워크는 쓰지 않는다.
"""
from server import request_allowed, route_of


def test_루프백_조회는_받는다():
    assert request_allowed("GET", "127.0.0.1:8787", None, "127.0.0.1", None)
    assert request_allowed("GET", "localhost:8787", "http://localhost:8787",
                           "127.0.0.1", None)


def test_대시보드_POST는_표식이_있어야_한다():
    assert request_allowed(
        "POST", "127.0.0.1:8787", "http://127.0.0.1:8787", "127.0.0.1", "fma")
    assert not request_allowed(
        "POST", "127.0.0.1:8787", "http://127.0.0.1:8787", "127.0.0.1", None)


def test_다른_사이트의_POST는_거절한다():
    assert not request_allowed(
        "POST", "127.0.0.1:8787", "https://evil.example", "127.0.0.1", "fma")
    assert not request_allowed(
        "POST", "evil.example", "http://evil.example", "127.0.0.1", "fma")


def test_null_오리진은_거절한다():
    assert not request_allowed(
        "POST", "127.0.0.1:8787", "null", "127.0.0.1", "fma")


def test_일부러_연_서버도_오리진과_표식은_본다():
    assert request_allowed(
        "POST", "10.0.0.8:8787", "http://10.0.0.8:8787", "0.0.0.0", "fma")
    assert not request_allowed(
        "POST", "10.0.0.8:8787", "https://evil.example", "0.0.0.0", "fma")
    assert not request_allowed(
        "POST", "10.0.0.8:8787", None, "0.0.0.0", None)


def test_경로는_접두사로_넓히지_않는다():
    assert route_of("/api/state?x=1") == "/api/state"
    assert route_of("/api/state-evil") == "/api/state-evil"
    assert route_of("/api/holdings/") == "/api/holdings"
