# CLAUDE.md — fund_manager_agent

이 파일은 Claude Code가 이 프로젝트에서 작업할 때 참고하는 가이드입니다.

## 시작하기 전에

`README.md` 로 전체 그림을, `IDEAS.md` 로 남은 작업과 우선순위를 파악하세요.
아래 "장부 불변식"·"두 장부는 절대 섞지 말 것"·"백테스트 규율" 절은 **코드를 고치기 전에**
읽어야 합니다. 과거에 실제로 물렸던 문제들이 왜 그렇게 설계됐는지 설명합니다.

## 프로젝트 요약

skfolio/Kronos/NautilusTrader 3개 레포를 실제로 설치해 엮은 모의투자 에이전트 봇 +
실시간 대시보드. 상세 아키텍처와 사용법은 `README.md` 참고.

```
core/
  config.py          모든 설정값 (WATCHLIST, 리스크 파라미터, 게이트 한도, 실행 모드)
  data_desk.py        yfinance OHLCV
  kronos_paths.py      Kronos 다중 경로 샘플링 래퍼 (원본 레포 함수를 감싼 것)
  research_desk.py     Kronos로 워치리스트 스캔 → 견해 + 확신도
  portfolio_desk.py    skfolio BL + CVaR MeanRisk, 리스크·회전율 게이트
  execution_desk.py    주식 수 + 현금 장부, NautilusTrader MarketOrder 생성 (전송 안 됨)
  cycle.py             ★ 사이클 코어 — 선별·최적화·게이트. 라이브와 백테스트가 공유
  backtest.py          과거 리플레이 하네스
  infer_cache.py       Kronos 추론 캐시 (파라미터 스윕을 싸게 만든다)
  holdings.py          ★ 실계좌 보유 장부 (봇 장부와 완전 분리)
  holdings_analysis.py  보유 종목 견해 + 집중도·상관·변동성 진단
  holdings_api.py       보유 관련 HTTP 핸들러
  holdings_csv.py       증권사 CSV 임포트 (컬럼명 자동 인식, cp949)
  symbol_search.py      종목 검색(네이버+Yahoo)·시세·환율
  brokers/              증권사 어댑터 — **조회 전용, 주문 메서드 없음**
  performance.py       샤프·최대낙폭·벤치마크 대비 등 성과 지표
  pm_desk.py           PM 논평 (LLM 또는 룰 기반)
bot.py                 메인 루프 (once/loop/schedule)
server.py               대시보드 서버 (표준 라이브러리만 사용, FastAPI 없음)
build_snapshot.py       서버 없이 여는 단일 HTML 스냅샷 생성
scripts/backtest.py     백테스트 CLI
web/dashboard.html       대시보드 프런트엔드 (바닐라 JS, 빌드 스텝 없음)
tests/                   pytest (모델·네트워크 불필요, 10초 남짓)
state/                   실행 상태 (state.json, history.jsonl, infer_cache.jsonl) — gitignore
reports/                 백테스트 결과 JSON — gitignore
```

## 장부 불변식 (건드리기 전에 읽을 것)

`execution_desk.py` 는 **주식 수 + 현금**으로 장부를 유지한다. 비중 장부로 되돌리지 말 것.
아래 불변식이 `tests/test_execution_desk.py` 로 고정돼 있다.

- 현금은 절대 음수가 되지 않는다 (매수 전 `affordable` 계산으로 차단)
- 총 노출은 100%를 넘지 않는다 — 목표 비중 합이 1을 넘게 들어와도 마찬가지
- 매도가 매수보다 먼저 체결된다 (현금 확보 순서). `plan.sort(key=lambda r: r[1])` 이 그 역할
- 주문 수량의 합 = 장부 포지션 변화량 (정수 반올림 후에도 일치)
- 가격이 결손된 종목은 주문을 만들지 않고 포지션을 유지하며 `degraded` 에 기록한다

**과거 회귀 2건**: ①유니버스 탈락 종목의 청산 주문 누락으로 포지션 증발, ②부분 실패 상태에서
총 노출 150% 도달. 둘 다 테스트로 고정돼 있으니 이 파일을 고치면 반드시 `pytest -q` 를 돌릴 것.

## 백테스트 규율 (건드리기 전에 읽을 것)

**① 전략 로직을 두 곳에 두지 말 것.**
선별·최적화·리스크/회전율 게이트는 `core/cycle.py::plan` 한 곳에만 있다. `bot.py` 와
`core/backtest.py` 가 **같은 함수**를 부른다. 백테스트 쪽에 "조금만 다른" 로직을 넣는
순간 백테스트는 실제로 돌아가는 전략이 아닌 것을 측정하게 되고, 그 성과 숫자는 아무것도
보장하지 않는다. 이력 레코드 모양도 마찬가지 이유로 `cycle.history_record` 하나를 쓴다
(성과 지표는 `performance.summarize` 하나로만 계산한다).

**② 룩어헤드를 막는 지점은 `backtest.slice_bars` 하나다.**
매 시점 그 날짜까지만 잘라 넘긴다. 룩어헤드는 에러 없이 성과만 좋아지므로 테스트가
없으면 영영 발견되지 않는다. `tests/test_backtest.py::test_스코어러는_기준일_이후_데이터를_보지_못한다`
가 이걸 고정한다 — **지우지 말 것.**

**③ 추론 캐시 키에는 추론 파라미터가 전부 들어가야 한다.**
`infer_cache.fingerprint()` 에 모델·룩백·예측길이·샘플수·온도·top_p 가 들어 있다.
여기에 빠진 파라미터를 새로 만들면 "바꿨는데 결과가 같다"는 침묵 버그가 생긴다.
추론 파라미터를 추가하면 지문에도 반드시 추가할 것.

**④ 백테스트 성과는 상한이지 기대값이 아니다.**
슬리피지가 없고, 워치리스트에 생존 편향이 있고, 수정주가를 쓴다. 이 한계를 지운 채
숫자만 인용하지 말 것 — 한계 목록은 `core/backtest.py` 모듈 독스트링과 README 에 있다.

## 두 장부는 절대 섞지 말 것

이 프로젝트에는 장부가 **두 개** 있다.

| | `core/execution_desk.py` | `core/holdings.py` |
|---|---|---|
| 무엇 | 봇이 모의로 굴리는 포트폴리오 | 사용자가 **실제 증권사에서 산** 종목 |
| 파일 | `state/state.json` | `state/holdings.json` |
| 누가 바꾸나 | 사이클마다 봇이 자동으로 | 사용자가 직접 · 증권사 조회 · CSV |
| 주문 | 모의 주문을 만든다 | **만들지 않는다** |

섞이면 두 가지가 동시에 망가진다. 봇의 장부 불변식이 사용자가 손으로 넣은 수량 때문에
깨지고, 사용자는 "내 실제 보유가 봇의 매매 판단에 반영된다"고 오해하게 된다.
**보유 종목을 `target_weights` 나 옵티마이저 유니버스에 넣는 코드를 추가하지 말 것.**

## 증권사 연동은 조회 전용 (인터페이스에 주문이 없다)

`core/brokers/` 의 `BrokerAdapter` 에는 `fetch_holdings()` 하나뿐이다. 토스·KIS 모두
주문 API 를 제공하지만 **그 경로를 코드에 옮겨 적지 않는다.** 경로 문자열조차 두지
않는다 — 있으면 나중에 이어 붙이기 쉬워진다. `tests/test_brokers.py` 가 메서드 이름과
소스 문자열 양쪽을 검사한다.

증권사 조회 실패는 **반드시 예외**다. 빈 목록으로 돌려주면 `replace_source` 가
"보유 종목이 없다"로 읽고 멀쩡한 장부를 지운다.

자격증명은 `.env` 에서만 읽고, 토큰은 메모리에만 두며, 오류 메시지에 키가 섞이지
않는지도 테스트로 고정돼 있다.

## ⚠️ 절대 원칙

- **`config.EXECUTION_MODE`를 `"paper"` 외의 값으로 바꾸지 말 것.** `ExecutionDesk.__init__`이
  이를 강제로 검사해서 시작을 거부하도록 설계되어 있음. 이 가드를 우회하는 코드를 추가하지 말 것.
- **브로커 커넥터를 추가하지 말 것.** 주문은 NautilusTrader 도메인 객체로만 존재하고 어디로도
  전송되지 않는 것이 설계 의도. 실거래 연동 요청이 오면 사용자에게 명확히 확인받을 것.
- PM(LLM) 데스크는 텍스트만 반환해야 함. `pm_desk.py`가 `target_weights`나 주문에 관여하는
  코드를 추가하지 말 것 — 읽기 전용 역할이 이 프로젝트의 핵심 안전장치.

## 🔐 API 키 규칙

키·토큰은 `.env`에만 둔다. 코드·설정 파일(JSON·YAML 포함)에 직접 쓰지 않는다.
`.env`는 `.gitignore`에 등록돼 있다. 이 프로젝트가 다루는 키는 `ANTHROPIC_API_KEY`(선택)와
증권사 자격증명(선택)뿐이며, 셋 다 없어도 전 기능이 동작한다(증권사 연동만 비활성).

증권사 토큰은 메모리에만 두고 파일로 저장하지 않는다. 오류 메시지에 자격증명이 섞여
나가지 않는지도 테스트로 고정돼 있다.

사용자가 채팅에 API 키를 붙여넣으면 파일에 기록하지 말고 폐기·재발급을 안내할 것.

## 코딩 컨벤션

- 로그·주석·독스트링은 한국어. 변수명·함수명은 영어.
- 각 데스크 모듈은 독립적으로 임포트 가능해야 함 (순환 의존 없음, 전부 `core.config`만 참조).
- `bot.py`의 `Agent._persist`가 `state.json` 스키마의 단일 진처. 대시보드(`dashboard.html`)의
  `render()` 함수가 이 스키마를 그대로 소비하므로, 스키마를 바꾸면 두 파일을 같이 고칠 것.
  `build_snapshot.py` 는 `dashboard.html` 의 특정 문자열을 앵커로 치환하므로, 그 세 군데
  (`<div id="root">`, 폴링 시작 줄, 부제 배지)를 건드리면 앵커도 같이 고칠 것 — 앵커가 어긋나면
  빌드가 실패하도록 되어 있다(조용히 깨진 스냅샷이 나오지 않게).
- 파일을 읽고 쓸 때 **인코딩을 항상 명시**할 것 (`encoding="utf-8"`). Windows 기본값은 cp949라
  한국어가 든 파일에서 깨지거나 죽는다. 상태 파일은 `bot.py` 의 `_atomic_write` 를 쓸 것 —
  대시보드가 15초마다 폴링하므로 부분적으로 쓰인 파일을 읽으면 JSON이 깨진다.
- 새 기능 추가 후에는 `pytest -q` (10초 남짓) 다음 `python bot.py once` 로 한 사이클 돌려서
  `state.json`이 정상 생성되는지 확인할 것 — Kronos 추론 때문에 사이클당 12~15초 걸림.
  전략 로직(`core/cycle.py`·`portfolio_desk.py`)을 건드렸다면 백테스트도 한 번 돌릴 것:
  `python scripts/backtest.py --start <최근 6개월> --step 20` (캐시가 있으면 수 초).
- `ANTHROPIC_API_KEY` 는 `.env` 뿐 아니라 **시스템 환경변수에 있어도** `os.getenv` 가 읽어간다.
  다만 호출은 `PM_LLM_MODE`(기본 `once`)가 통제하므로 최초 1회 이후에는 비용이 나가지 않는다.
  완전히 막으려면 `PM_LLM_MODE=never`. 테스트에서는 `_llm_opinion` 을 monkeypatch 하고
  실제 API 는 절대 부르지 않는다 (`tests/test_pm_desk.py` 참고).
- **비용이 나가는 지점은 두 곳뿐이다**: 사이클 실행(`PM_LLM_MODE` 가 허용할 때)과
  `POST /api/pm-opinion`(사용자가 버튼을 눌렀을 때). 대시보드 폴링·새로고침은 `state.json` 을
  읽기만 한다. LLM 호출 경로를 늘리는 코드를 추가하지 말 것.
- **논평을 캐시하지 말 것.** LLM 을 부르지 않는 사이클에는 룰 기반 논평을 그 사이클 데이터로
  새로 생성한다. 예전 논평을 재사용하면 화면의 숫자와 글이 다른 사이클을 가리키게 된다.
- `core/storage.py` 의 `atomic_write`/`read_json`/`write_json` 을 쓸 것. 상태 파일을
  직접 `write_text` 하지 말 것 (폴링 중 부분 쓰기를 읽으면 JSON 이 깨진다).
