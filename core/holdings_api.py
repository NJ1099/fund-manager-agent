"""
보유 종목 HTTP 핸들러.

`server.py` 가 얇게 유지되도록 보유 관련 요청 처리를 여기 모았다.
반환은 `(상태코드, 응답 dict)` 이며, 서버는 그대로 JSON 으로 내보낸다.

■ 이 레이어의 책임 하나 — 실패를 실패로 돌려준다
증권사 조회가 실패했는데 200 + 빈 목록을 주면 화면은 "보유 종목 없음"으로 보이고,
사용자는 자기 계좌가 빈 줄 안다. 실패는 4xx/5xx 로, 사유를 담아 돌려준다.

■ 분석 비용
`analyze` 는 Kronos 추론을 종목 수만큼 돌린다(종목당 1초 남짓). 그래서 폴링 경로가
아니라 **버튼을 눌렀을 때만** 호출된다. 대시보드의 15초 폴링은 저장된 결과를 읽을
뿐이며 추론을 돌리지 않는다 — 이 성질을 깨지 말 것.
"""
import logging
import threading

from . import config, holdings_analysis, holdings_csv, storage, symbol_search
from .holdings import HoldingsBook

log = logging.getLogger("holdings.api")

ANALYSIS_FILE = config.STATE_DIR / "holdings_analysis.json"

_analysis_lock = threading.Lock()
_analyzing = False


def _book():
    return HoldingsBook.load()


def _ok(book, **extra):
    """장부 현재 상태를 그대로 돌려준다 (편집 직후 화면 갱신용)."""
    return 200, {"ok": True, "holdings": book.to_list(),
                 "count": len(book), "base_currency": config.HOLDINGS_BASE_CURRENCY,
                 **extra}


# ------------------------------------------------------------------ 조회
def list_holdings():
    book = _book()
    return 200, {
        "holdings": book.to_list(),
        "count": len(book),
        "base_currency": config.HOLDINGS_BASE_CURRENCY,
        "analysis_running": _analyzing,
        "analysis": storage.read_json(ANALYSIS_FILE) if ANALYSIS_FILE.exists() else None,
    }


def search(query, limit=10):
    q = (query or "").strip()
    if not q:
        return 400, {"error": "검색어가 비어 있습니다"}
    try:
        return 200, {"query": q, "results": symbol_search.search(q, limit=limit)}
    except Exception as e:
        return 502, {"error": str(e)}


# ------------------------------------------------------------------ 편집
def add(payload):
    """수동 추가. mode='lot' 이면 추가매수(평단 가중평균), 아니면 덮어쓰기."""
    ticker = (payload.get("ticker") or "").strip()
    if not ticker:
        return 400, {"error": "종목을 선택하세요"}

    try:
        quantity = float(payload.get("quantity"))
    except (TypeError, ValueError):
        return 400, {"error": "수량을 숫자로 입력하세요"}

    avg_cost = payload.get("avg_cost")
    try:
        avg_cost = float(avg_cost) if avg_cost not in (None, "") else None
    except (TypeError, ValueError):
        return 400, {"error": "평균단가를 숫자로 입력하세요"}

    book = _book()
    try:
        if payload.get("mode") == "lot":
            if avg_cost is None:
                return 400, {"error": "추가매수에는 매수단가가 필요합니다"}
            book.add_lot(ticker, quantity=quantity, price=avg_cost,
                         name=payload.get("name") or None,
                         currency=(payload.get("currency") or "").upper() or None)
        else:
            book.upsert(ticker=ticker, quantity=quantity, avg_cost=avg_cost,
                        name=payload.get("name") or None,
                        currency=(payload.get("currency") or "").upper() or None,
                        note=payload.get("note") or None, source="manual")
    except ValueError as e:
        return 400, {"error": str(e)}

    book.save()
    return _ok(book, message=f"{ticker} 을(를) 저장했습니다")


def remove(ticker):
    book = _book()
    if book.remove(ticker) is None:
        return 404, {"error": f"{ticker} 은(는) 장부에 없습니다"}
    book.save()
    return _ok(book, message=f"{ticker} 을(를) 삭제했습니다")


# ------------------------------------------------------------------ 가져오기
def brokers_status():
    from . import brokers
    return 200, {"brokers": brokers.available()}


def sync(name):
    """증권사에서 보유를 읽어 해당 출처만 교체한다."""
    from . import brokers
    from .brokers import BrokerError

    try:
        adapter = brokers.get(name)
        items = adapter.fetch_holdings()
    except BrokerError as e:
        # 조회 실패를 빈 목록으로 바꾸면 장부가 통째로 지워진다
        return 502, {"error": str(e)}
    except Exception as e:
        log.exception("증권사 동기화 실패")
        return 502, {"error": f"{name} 동기화 실패: {type(e).__name__}"}

    book = _book()
    info = book.replace_source(name, items)
    book.save()
    return _ok(book, message=(
        f"{adapter.label}에서 {info['synced']}종목을 가져왔습니다"
        + (f" (사라진 종목 {len(info['removed'])}개 정리)" if info["removed"] else "")
    ), sync=info)


def import_csv(raw, replace=False):
    """CSV 업로드. replace=True 면 기존 csv 출처를 통째로 교체한다."""
    try:
        items, info = holdings_csv.parse(raw)
    except ValueError as e:
        return 400, {"error": str(e)}
    except Exception as e:
        log.exception("CSV 해석 실패")
        return 400, {"error": f"CSV 를 읽지 못했습니다: {type(e).__name__}"}

    if not items:
        return 400, {"error": "가져올 보유 종목이 없습니다 (수량이 0보다 큰 줄이 없습니다)",
                     "detail": info}

    book = _book()
    if replace:
        book.replace_source("csv", items)
    else:
        for it in items:
            book.upsert(source="csv", **{k: v for k, v in it.items() if k != "source"})
    book.save()
    return _ok(book, message=f"CSV 에서 {len(items)}종목을 가져왔습니다", detail=info)


# ------------------------------------------------------------------ 분석
def analysis_status():
    return 200, {
        "running": _analyzing,
        "analysis": storage.read_json(ANALYSIS_FILE) if ANALYSIS_FILE.exists() else None,
    }


def start_analysis(with_views=True):
    """백그라운드로 분석을 돌린다. 추론이 종목 수만큼 걸리므로 요청을 붙잡지 않는다."""
    global _analyzing
    book = _book()
    if not len(book):
        return 400, {"error": "보유 종목이 없습니다. 먼저 종목을 추가하세요"}

    with _analysis_lock:
        if _analyzing:
            return 409, {"error": "이미 분석 중입니다"}
        _analyzing = True

    def work():
        global _analyzing
        try:
            result = holdings_analysis.analyze(book, with_views=with_views)
            storage.write_json(ANALYSIS_FILE, result)
            log.info("보유 분석 완료: %d종목", len(book))
        except Exception as e:
            log.exception("보유 분석 실패")
            storage.write_json(ANALYSIS_FILE, {
                "empty": False, "error": f"{type(e).__name__}: {e}",
                "notes": [f"분석에 실패했습니다: {e}"],
            })
        finally:
            _analyzing = False

    threading.Thread(target=work, daemon=True).start()
    return 202, {"started": True,
                 "message": f"{len(book)}종목 분석을 시작했습니다"
                            + (" (견해 산출에 종목당 1초 남짓 걸립니다)" if with_views else "")}
