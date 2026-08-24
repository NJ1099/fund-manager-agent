"""
에이전트 설정.

⚠️ 안전 원칙 (원문 "LLM은 애널리스트지 트레이더가 아닙니다"를 그대로 구현)
   - EXECUTION_MODE 는 'paper' 로 고정되어 있고, 코드가 이를 강제한다.
   - 브로커 커넥터는 붙어 있지 않다. 주문은 NautilusTrader 도메인 객체로
     '표현'만 되고 어디에도 전송되지 않는다.
   - PM(LLM)은 리포트만 쓴다. 주문 경로에 들어가지 않는다.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv 미설치 시 시스템 환경변수만 사용

# ---------------------------------------------------------------- 실행 모드
EXECUTION_MODE = "paper"          # 'paper' 외의 값은 bot.py 가 거부한다
ALLOW_LIVE_ORDERS = False          # 절대 True 로 두지 말 것

# ---------------------------------------------------------------- 관찰 유니버스
# 에이전트가 매 사이클 "주목"하며 점수를 매기는 종목들.
WATCHLIST = [
    "SPY",   # S&P 500
    "QQQ",   # 나스닥 100
    "IWM",   # 러셀 2000
    "TLT",   # 장기 국채
    "IEF",   # 중기 국채
    "GLD",   # 금
    "SLV",   # 은
    "XLE",   # 에너지
    "XLF",   # 금융
    "XLK",   # 기술
]

# 확신도 상위 몇 개를 실제 포트폴리오 후보로 올릴지
TOP_K = 5

# ---------------------------------------------------------------- 연구 데스크 (Kronos)
KRONOS_TOKENIZER = "NeoQuasar/Kronos-Tokenizer-2k"
KRONOS_MODEL = "NeoQuasar/Kronos-mini"       # 4.1M, CPU에서 동작
KRONOS_DEVICE = os.getenv("KRONOS_DEVICE", "cpu")
KRONOS_MAX_CONTEXT = 2048
LOOKBACK = 300          # 컨텍스트 거래일
PRED_LEN = 5            # 예측 거래일 (1주)
SAMPLE_COUNT = 16       # 경로 샘플 수 (원문은 32; CPU 예산에 맞춰 축소)
TEMPERATURE = 1.0
TOP_P = 0.9

# ---------------------------------------------------------------- 포트폴리오 데스크 (skfolio)
BL_TAU = 0.05
CVAR_BETA = 0.95
MAX_TURNOVER = 0.10     # 원문: 회전율 10%
TX_COST_BPS = 0.001     # 원문: 거래비용 10bps
MIN_WEIGHT = 0.0        # 롱온리
MAX_WEIGHT = 0.40       # 단일 종목 상한 (원문에 없음; 집중도 방어용으로 추가)
COV_WINDOW = 252        # 공분산 추정 구간

# ---------------------------------------------------------------- 집행 데스크 (NautilusTrader)
TRADER_ID = "CLAUDE-PM-001"
STRATEGY_ID = "BL-CVAR-WATCHLIST"
VENUE = "SIM"
INITIAL_EQUITY = 1_000_000.0
MIN_ORDER_NOTIONAL = 500.0   # 이보다 작은 차이는 주문하지 않음

# ---------------------------------------------------------------- 성과 · 리스크
# 벤치마크: 이 파이프라인 전체가 존재할 가치가 있는지 판정하는 기준선.
BENCHMARK = "SPY"

# 리스크 게이트 — 손실이 한도를 넘으면 자동으로 노출을 줄인다.
# 예측이 틀렸을 때 얼마나 잃느냐가 예측이 맞을 확률보다 중요하기 때문이다.
RISK_GATE_ENABLED = True
MAX_DRAWDOWN_LIMIT = 0.20     # 고점 대비 낙폭이 이 값을 넘으면 노출 축소
DRAWDOWN_EXPOSURE = 0.50      # 축소 후 유지할 목표 노출 비율
CYCLE_LOSS_LIMIT = 0.05       # 한 사이클에 이만큼 잃으면 다음 사이클 노출 축소

# 회전율 게이트 — 옵티마이저 제약을 통과했더라도 집행 직전에 다시 검증한다.
# 유니버스가 바뀌면 옵티마이저가 보는 회전율과 실제 회전율이 어긋날 수 있다.
# 기준은 '매도측' 회전율(기존 포지션을 얼마나 갈아엎는가)이다 — 현금에서 신규
# 편입하는 것은 교체가 아니므로 세지 않는다.
TURNOVER_HARD_LIMIT = 0.25    # 이 값을 넘는 목표는 이전 비중 쪽으로 되돌린다

# ---------------------------------------------------------------- 보유 종목 (실계좌)
# 사용자가 실제 증권사에서 산 종목. 봇의 모의 장부와 **완전히 분리된** 별도 파일이다.
# 이 데이터는 분석·표시에만 쓰이며 어떤 목표비중에도 들어가지 않는다.
# 자세한 이유는 core/holdings.py 모듈 독스트링 참고.
HOLDINGS_BASE_CURRENCY = os.getenv("HOLDINGS_BASE_CURRENCY", "KRW")

# 증권사 연동은 **조회 전용**이다. 주문 API 는 어댑터 인터페이스에조차 없다.
# 키는 .env 에만 두고 코드·설정 파일에 쓰지 않는다.
TOSS_CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "")
TOSS_CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "")
TOSS_API_BASE = os.getenv("TOSS_API_BASE", "https://openapi.tossinvest.com")

KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT = os.getenv("KIS_ACCOUNT", "")          # 8자리-2자리 (예: 12345678-01)
KIS_API_BASE = os.getenv("KIS_API_BASE", "https://openapi.koreainvestment.com:9443")

# 증권사 호출 타임아웃 (초). 대시보드 요청이 무한정 매달리지 않게 한다.
BROKER_TIMEOUT_SEC = int(os.getenv("BROKER_TIMEOUT_SEC", "15"))

# ---------------------------------------------------------------- 서버
# 기본은 루프백 전용. 다른 기기에서 열려면 SERVER_HOST 를 명시적으로 바꿔야 한다.
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8787"))

# 이력 파일 상한 (넘으면 오래된 줄부터 잘라낸다 — 무한 증가 방지)
HISTORY_MAX_LINES = 5000

# ---------------------------------------------------------------- PM 데스크 (LLM, 선택)
# 키가 없으면 결정론적 룰 기반 논평으로 자동 폴백한다.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PM_MODEL = os.getenv("PM_MODEL", "claude-sonnet-4-6")
PM_ENABLED = bool(ANTHROPIC_API_KEY)

# LLM 호출 정책 — 사이클마다 호출하면 비용이 계속 나가므로 기본은 최초 1회다.
#   once      : 최초 1회만 호출. 이후 사이클은 무료 룰 기반 논평 (기본)
#   on_change : 포트폴리오가 실제로 달라진 사이클에만 호출
#               (주문 발생 · 게이트 발동 · 최적화 후퇴 · 데이터 결손 중 하나라도 있을 때)
#   always    : 매 사이클 호출 — 비용이 사이클 수에 비례한다
#   never     : 키가 있어도 호출하지 않음
#
# 대시보드 폴링(/api/state)은 저장된 파일을 읽을 뿐이라 어떤 모드에서도 비용이 0이다.
# LLM 은 '사이클이 실제로 실행될 때'만 호출될 수 있다.
PM_LLM_MODE = os.getenv("PM_LLM_MODE", "once")

# on_change 모드에서 '큰 손실'로 보는 기준 (%). 이보다 나쁜 사이클은 설명을 받는다.
PM_ALERT_LOSS_PCT = float(os.getenv("PM_ALERT_LOSS_PCT", "-2.0"))

# ---------------------------------------------------------------- 스케줄
# 원문: "16:00 타이머". 기본은 미국장 마감 후.
CYCLE_HOUR_ET = 16
CYCLE_MINUTE_ET = 5
CYCLE_INTERVAL_SEC = int(os.getenv("CYCLE_INTERVAL_SEC", "0"))  # >0 이면 주기 실행(테스트용)

# ---------------------------------------------------------------- 경로
BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "state.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
HOLDINGS_FILE = STATE_DIR / "holdings.json"
WEB_DIR = BASE_DIR / "web"

STATE_DIR.mkdir(parents=True, exist_ok=True)
