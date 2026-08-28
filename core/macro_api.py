"""
시장 브리핑 HTTP 핸들러.

`holdings_api` 와 같은 규약이다 — `(상태코드, 응답 dict)` 을 돌려주고 서버는
그대로 JSON 으로 내보낸다.

■ 폴링은 파일만 읽는다
수집은 yfinance 27종목 + RSS 11개라 20~40초 걸린다. 대시보드가 15초마다 폴링하는데
그때마다 수집하면 외부 서비스에 요청 폭탄이 되고 화면도 느려진다. 그래서
**갱신은 버튼(POST)으로만** 하고, 조회(GET)는 저장된 `state/macro.json` 을 읽기만 한다.
보유 분석과 같은 구조이며, 그 이유도 같다.

■ 비용은 0원이다
매크로 경로에는 LLM 이 없다. 코멘트·대응은 `macro_brief` 의 규칙이 만든다.
갱신 버튼을 몇 번 눌러도 과금되지 않는다 — 이 성질을 깨는 코드를 넣지 말 것.
"""
import logging
import threading
from datetime import datetime, timezone

from . import config, macro_brief, macro_desk, news_desk, storage

log = logging.getLogger("macro.api")

MACRO_FILE = config.STATE_DIR / "macro.json"

_lock = threading.Lock()
_running = False

# 이보다 오래된 데이터는 화면에서 '오래됨'으로 표시한다. 시세는 장중에 계속 바뀌고
# 뉴스는 몇 시간이면 낡는다 — 언제 받은 것인지 모르는 화면이 제일 위험하다.
STALE_MINUTES = 30


def _age_minutes(iso):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60.0


def get():
    """저장된 브리핑을 돌려준다. 수집하지 않는다."""
    if not MACRO_FILE.exists():
        return 200, {
            "empty": True,
            "running": _running,
            "message": "아직 시장 데이터를 받지 않았습니다. '지표 갱신'을 누르세요.",
        }
    data = storage.read_json(MACRO_FILE)
    age = _age_minutes(data.get("collected_at"))
    data["running"] = _running
    data["age_minutes"] = None if age is None else round(age, 1)
    data["stale"] = bool(age is not None and age > STALE_MINUTES)
    data["stale_after_minutes"] = STALE_MINUTES
    return 200, data


def refresh(with_news=True):
    """백그라운드로 지표·뉴스를 새로 받는다. 요청을 붙잡지 않는다."""
    global _running
    with _lock:
        if _running:
            return 409, {"error": "이미 갱신 중입니다"}
        _running = True

    def work():
        global _running
        try:
            macro = macro_desk.collect()
            news = news_desk.collect() if with_news else None
            brief = macro_brief.build(macro, news)
            storage.write_json(MACRO_FILE, {
                "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "macro": macro, "news": news, "brief": brief,
            })
            log.info("시장 브리핑 갱신: 지표 %d개 · 뉴스 %d건",
                     macro.get("count", 0), (news or {}).get("count", 0))
        except Exception as e:
            log.exception("시장 브리핑 갱신 실패")
            # 실패를 빈 결과로 덮어쓰지 않는다 — 직전 브리핑은 남겨두고 오류만 기록한다.
            prev = storage.read_json(MACRO_FILE) if MACRO_FILE.exists() else {}
            prev["error"] = f"{type(e).__name__}: {e}"
            prev["error_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            storage.write_json(MACRO_FILE, prev)
        finally:
            _running = False

    threading.Thread(target=work, daemon=True).start()
    return 202, {"started": True,
                 "message": "지표와 뉴스를 받는 중입니다 (20~40초). 비용은 들지 않습니다."}
