"""
파라미터 오버라이드 · 스윕.

백테스트 하네스가 존재하는 이유는 "이 값이 좋은 값인가"에 답하기 위해서다.
그러려면 값을 바꿔가며 돌릴 수 있어야 한다. 추론 캐시 덕에 **추론 이후 단계의**
파라미터 스윕은 사실상 공짜다 (실측: 7사이클 100초 → 0.3초).

■ 왜 화이트리스트인가
config 를 임의로 덮어쓸 수 있게 하면 `EXECUTION_MODE` 나 `ALLOW_LIVE_ORDERS` 까지
바꿀 수 있게 된다. 이 두 값은 이 프로젝트의 안전장치이고, 백테스트 편의를 위해
그 문을 열어둘 이유가 없다. 그래서 **바꿔도 되는 것만** 명시한다.

■ 스윕 결과를 읽을 때
사이클 몇십 개짜리 백테스트에서 가장 좋은 값을 고르는 것은 과최적화다.
스윕은 "이 파라미터가 성과에 얼마나 민감한가"를 보는 도구지, 최적값을 찾는
도구가 아니다. 값에 따라 성과가 요동치면 그 자체가 경고 신호다 —
안정적인 구간을 고르는 편이 낫다.
"""
from . import config

# 백테스트에서 바꿔도 되는 설정. 주석은 스윕 결과를 해석할 때의 요점.
TUNABLE = {
    "TOP_K": int,                  # 몇 종목까지 편입할지
    "MAX_TURNOVER": float,          # 옵티마이저 회전율 제약
    "TURNOVER_HARD_LIMIT": float,   # 집행 직전 회전율 게이트
    "MAX_WEIGHT": float,            # 단일 종목 상한
    "MIN_WEIGHT": float,
    "TX_COST_BPS": float,           # 거래비용 (낮추면 성과가 좋아지는 게 당연하다)
    "CVAR_BETA": float,
    "BL_TAU": float,
    "COV_WINDOW": int,
    "MIN_ORDER_NOTIONAL": float,
    "RISK_GATE_ENABLED": bool,
    "MAX_DRAWDOWN_LIMIT": float,
    "DRAWDOWN_EXPOSURE": float,
    "CYCLE_LOSS_LIMIT": float,
    # 아래 둘은 추론 파라미터라 바꾸면 캐시가 무효화되고 재추론이 일어난다.
    "SAMPLE_COUNT": int,
    "PRED_LEN": int,
    "TEMPERATURE": float,
    "TOP_P": float,
}

# 절대 바꿀 수 없는 것 (여기 없어도 TUNABLE 에 없으면 거부되지만, 의도를 명시해 둔다)
FORBIDDEN = {"EXECUTION_MODE", "ALLOW_LIVE_ORDERS", "ANTHROPIC_API_KEY", "PM_LLM_MODE"}


def _cast(name, raw):
    typ = TUNABLE[name]
    if typ is bool:
        low = str(raw).strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{name}: 참/거짓 값이 아닙니다 — {raw}")
    return typ(raw)


def parse_assignment(text):
    """'TOP_K=3' → ('TOP_K', 3)"""
    if "=" not in text:
        raise ValueError(f"KEY=VALUE 형식이 아닙니다: {text}")
    key, raw = text.split("=", 1)
    key = key.strip().upper()
    if key in FORBIDDEN:
        raise ValueError(f"{key} 는 백테스트에서 바꿀 수 없습니다 (안전장치).")
    if key not in TUNABLE:
        raise ValueError(f"{key} 는 조정 가능한 설정이 아닙니다. "
                         f"가능한 값: {', '.join(sorted(TUNABLE))}")
    return key, _cast(key, raw.strip())


def apply(assignments):
    """config 에 값을 적용하고 (이전값 dict) 를 돌려준다.

    각 데스크가 함수 안에서 `config.X` 를 읽으므로 setattr 이 그대로 반영된다.
    """
    previous = {}
    for key, value in assignments.items():
        previous[key] = getattr(config, key)
        setattr(config, key, value)
    return previous


def restore(previous):
    for key, value in previous.items():
        setattr(config, key, value)


def parse_sweep(text):
    """'TOP_K=3,5,7' → ('TOP_K', [3, 5, 7])"""
    key, raw = text.split("=", 1) if "=" in text else (text, "")
    key = key.strip().upper()
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values:
        raise ValueError(f"스윕할 값이 없습니다: {text}")
    return key, [parse_assignment(f"{key}={v}")[1] for v in values]


def format_table(key, rows):
    """스윕 결과 비교표. rows = [(value, performance dict), …]"""
    def cell(v, nd=2, sign=False, suffix="%"):
        if v is None:
            return "—".rjust(9)
        txt = f"{v:+.{nd}f}{suffix}" if sign else f"{v:.{nd}f}{suffix}"
        return txt.rjust(9)

    head = (f"{key:>16} {'누적수익':>10} {'초과수익':>10} {'샤프':>8} "
            f"{'최대낙폭':>10} {'회전율':>10} {'승률':>9} {'비용':>10}")
    lines = [head, "-" * 88]
    for value, p in rows:
        lines.append(
            f"{str(value):>16} "
            f"{cell(p['cum_return_pct'], sign=True)} "
            f"{cell(p['excess_return_pct'], sign=True)} "
            f"{cell(p['sharpe'], suffix='')} "
            f"{cell(p['max_drawdown_pct'])} "
            f"{cell(p['avg_turnover_pct'])} "
            f"{cell(p['win_rate_pct'], nd=1)} "
            f"{('$' + format(p['total_costs'] or 0, ',.0f')).rjust(9)}"
        )
    lines.append("")
    lines.append("※ 표본이 작은 백테스트에서 최고 성적을 낸 값을 고르는 것은 과최적화다.")
    lines.append("   이 표는 '성과가 이 파라미터에 얼마나 민감한가'를 보는 용도다 —")
    lines.append("   값에 따라 결과가 요동치면 그 자체가 경고 신호이고, 안정 구간을 고르는 편이 낫다.")
    return "\n".join(lines)
