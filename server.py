#!/usr/bin/env python3
"""
대시보드 서버.

  python server.py            → http://localhost:8787

엔드포인트
  GET  /                    대시보드
  GET  /api/state           현재 에이전트 상태 (대시보드가 15초마다 폴링)
  POST /api/run-cycle       지금 한 사이클 실행 (백그라운드 스레드)

보유 종목 (사용자가 실제로 산 종목 — 봇의 모의 장부와 별개)
  GET  /api/holdings        보유 목록 + 마지막 분석 결과
  POST /api/holdings        수동 추가/수정 (mode=lot 이면 추가매수)
  POST /api/holdings/delete 삭제
  GET  /api/symbol-search   종목 검색 (?q=삼성전자)
  GET  /api/brokers         증권사 연동 상태
  POST /api/holdings/sync   증권사에서 조회해 가져오기 (?broker=toss)
  POST /api/holdings/import CSV 업로드
  POST /api/holdings/analyze 보유 종목 분석 실행 (Kronos 견해 + 진단)

증권사 연동은 **조회 전용**입니다. 주문 API 는 코드에 존재하지 않습니다.

의존성은 표준 라이브러리뿐입니다 (FastAPI 불필요).
"""
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import config, holdings_api, macro_api, storage

# 한국어 로그가 Windows 기본 콘솔(cp949)에서 깨지지 않게 UTF-8 로 고정한다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [server] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("server")

_run_lock = threading.Lock()
_running = False


def _run_cycle_bg():
    global _running
    with _run_lock:
        if _running:
            return False
        _running = True
    def work():
        global _running
        try:
            from bot import Agent
            Agent().run_cycle()
        except Exception:
            log.exception("사이클 실패")
        finally:
            _running = False
    threading.Thread(target=work, daemon=True).start()
    return True


def _refresh_pm_opinion():
    """지금 화면에 떠 있는 사이클에 대해 LLM 논평만 새로 받는다.

    사이클을 다시 돌리지 않는다 — 시장 데이터를 다시 부르지도, 주문을 만들지도 않는다.
    저장된 state 를 그대로 LLM 에 넘기므로, 화면의 숫자와 논평이 반드시 같은 사이클을
    가리킨다. 유료 호출이므로 사용자가 버튼을 눌렀을 때만 실행된다.

    반환: (성공 여부, 메시지, 갱신된 state 또는 None)
    """
    from core import pm_desk

    if not config.PM_ENABLED:
        return False, "ANTHROPIC_API_KEY 가 설정되어 있지 않습니다 (룰 기반 논평만 가능)", None
    if not config.STATE_FILE.exists():
        return False, "아직 사이클이 실행되지 않았습니다", None

    state = storage.read_json(config.STATE_FILE)
    cycle = pm_desk.cycle_from_state(state)
    try:
        text = pm_desk._llm_opinion(cycle)
    except Exception as e:
        log.warning("PM 논평 갱신 실패: %s", e)
        return False, f"LLM 호출 실패: {e}", None
    if not text:
        return False, "LLM 이 빈 응답을 반환했습니다", None

    state["pm_opinion"] = text
    state["pm_source"] = f"llm:{config.PM_MODEL}"
    state["llm_calls"] = state.get("llm_calls", 0) + 1
    state["meta"]["pm_source"] = state["pm_source"]
    storage.write_json(config.STATE_FILE, state)
    log.info("PM 논평 갱신 완료 (사이클 %s, 누적 호출 %d회)",
             state.get("cycle_no"), state["llm_calls"])
    return True, "PM 논평을 갱신했습니다", state


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def _query(self):
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(self.path).query)

    def _body_bytes(self, limit=8 * 1024 * 1024):
        """요청 본문. 크기 상한을 둔다 — 무제한이면 메모리가 통째로 날아간다."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": "본문이 비어 있습니다"})
            return None
        if length > limit:
            self._json(413, {"error": f"파일이 너무 큽니다 (상한 {limit // 1024 // 1024}MB)"})
            return None
        return self.rfile.read(length)

    def _body_json(self):
        raw = self._body_bytes(limit=1024 * 1024)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "JSON 을 해석하지 못했습니다"})
            return None

    def do_GET(self):
        if self.path.startswith("/api/state"):
            if not config.STATE_FILE.exists():
                self._send(404, json.dumps({
                    "error": "아직 사이클이 실행되지 않았습니다. `python bot.py once` 를 먼저 실행하세요."
                }, ensure_ascii=False))
                return
            state = storage.read_json(config.STATE_FILE)
            state["_running"] = _running
            # 논평을 실제로 호출하는 주체는 이 서버다. 사이클을 돌릴 때 키가 없었더라도
            # 서버에 키가 있으면 버튼은 눌릴 수 있어야 한다 (그 반대도 마찬가지).
            state["_llm_available"] = config.PM_ENABLED
            state["_llm_mode"] = config.PM_LLM_MODE
            self._send(200, json.dumps(state, ensure_ascii=False))

        elif self.path.startswith("/api/holdings"):
            self._json(*holdings_api.list_holdings())

        elif self.path.startswith("/api/symbol-search"):
            self._json(*holdings_api.search(self._query().get("q", [""])[0]))

        elif self.path.startswith("/api/brokers"):
            self._json(*holdings_api.brokers_status())

        elif self.path.startswith("/api/scorecard"):
            # 모델 성적표. 채점은 `scripts/validate.py` 가 미리 해두고 여기서는 읽기만 한다
            # (전 종목 과거 시세를 다시 받아야 해서 요청 안에서 하기엔 무겁다).
            f = config.STATE_DIR / "scorecard.json"
            if not f.exists():
                self._json(404, {"error": "성적표가 아직 없습니다. "
                                          "`python scripts/validate.py --save-state` 를 실행하세요"})
            else:
                self._json(200, storage.read_json(f))

        elif self.path.startswith("/api/macro"):
            # 저장된 브리핑을 읽기만 한다. 수집은 POST /api/macro/refresh 에서만.
            self._json(*macro_api.get())

        elif self.path in ("/", "/index.html"):
            page = config.WEB_DIR / "dashboard.html"
            if not page.exists():
                self._send(500, "dashboard.html 없음", "text/plain; charset=utf-8")
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path.startswith("/api/run-cycle"):
            started = _run_cycle_bg()
            self._send(202 if started else 409, json.dumps(
                {"started": started,
                 "message": "사이클 시작" if started else "이미 실행 중입니다"},
                ensure_ascii=False))

        elif self.path.startswith("/api/pm-opinion"):
            # 유료 LLM 호출 — 사용자가 버튼을 눌렀을 때만 여기로 온다
            if _running:
                self._send(409, json.dumps(
                    {"ok": False, "message": "사이클 실행 중입니다. 끝난 뒤 다시 시도하세요"},
                    ensure_ascii=False))
                return
            ok, message, state = _refresh_pm_opinion()
            self._send(200 if ok else 400, json.dumps(
                {"ok": ok, "message": message,
                 "llm_calls": (state or {}).get("llm_calls")}, ensure_ascii=False))

        elif self.path.startswith("/api/macro/refresh"):
            # 외부 시세·RSS 를 새로 받는다. LLM 을 부르지 않으므로 비용은 0원이다.
            self._json(*macro_api.refresh())

        elif self.path.startswith("/api/holdings/analyze"):
            self._json(*holdings_api.start_analysis())

        elif self.path.startswith("/api/holdings/sync"):
            broker = self._query().get("broker", [""])[0]
            self._json(*holdings_api.sync(broker))

        elif self.path.startswith("/api/holdings/import"):
            # CSV 는 텍스트가 아니라 바이트로 받는다 (국내 증권사 파일은 대부분 cp949)
            raw = self._body_bytes()
            if raw is None:
                return
            replace = self._query().get("replace", ["0"])[0] in ("1", "true", "yes")
            self._json(*holdings_api.import_csv(raw, replace=replace))

        elif self.path.startswith("/api/holdings/delete"):
            payload = self._body_json()
            if payload is None:
                return
            self._json(*holdings_api.remove(payload.get("ticker", "")))

        elif self.path.startswith("/api/holdings"):
            payload = self._body_json()
            if payload is None:
                return
            self._json(*holdings_api.add(payload))

        else:
            self._send(404, "not found", "text/plain")


def main(port=None, host=None):
    # 기본은 루프백 전용. /api/run-cycle 은 인증이 없으므로 0.0.0.0 에 열면
    # 같은 네트워크의 누구나 사이클을 돌릴 수 있다. 외부 노출은 SERVER_HOST 로 명시할 것.
    host = host or config.SERVER_HOST
    port = port or config.SERVER_PORT
    srv = ThreadingHTTPServer((host, port), Handler)
    log.info("대시보드: http://%s:%d  (모드=%s)",
             "localhost" if host in ("127.0.0.1", "0.0.0.0") else host,
             port, config.EXECUTION_MODE)
    if host == "0.0.0.0":
        log.warning("모든 인터페이스에 바인딩됨 — 사이클 실행 API가 무인증으로 노출됩니다")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("종료")


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
