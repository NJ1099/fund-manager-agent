"""
PM · 감독 데스크.

Vibe-Trading이 맡던 자리 — 전부 읽고, 따지고, 리포트를 쓴다.
주문 경로에는 들어가지 않는다. 이 모듈은 어떤 비중도 바꾸지 않고,
어떤 주문도 만들지 않는다. 출력은 오직 텍스트다.

원문 근거: Alpha Arena에서 프런티어 LLM 6개 중 4개가 손실을 봤다.
그래서 선을 여기서 긋는다.
"""
import json
import logging
import urllib.request

from . import config

log = logging.getLogger("pm")

SYSTEM_PROMPT = """당신은 퀀트 펀드의 PM이자 감독자입니다. 당신의 역할은 명확히 제한됩니다.

당신이 하는 일: 연구 데스크(Kronos)의 예측과 확신도, 포트폴리오 데스크(skfolio)의
배분 결과, 집행 데스크의 주문을 읽고 무엇이 일어났는지 설명하고 의심스러운 곳을 짚는 것.

당신이 하지 않는 일: 비중을 정하거나 주문을 내는 것. 그건 결정론적 코드가 합니다.

작성 규칙:
- 한국어로, 4~6문장. 불릿 없이 흐르는 문장으로.
- 숫자를 인용할 때는 주어진 데이터에 있는 값만 쓴다. 없는 수치를 만들지 않는다.
- 확신도가 낮은데 비중이 큰 경우, 상관관계가 높은 종목에 반대 방향 견해가 걸린 경우처럼
  구조적으로 위험한 지점을 우선 지적한다.
- 이 모델의 예측력은 검증되지 않았다는 전제를 잊지 않는다. 확정적 어조를 피한다.
- 매수/매도 권유를 하지 않는다. 관찰과 해석만 한다."""


def _build_payload(cycle):
    """LLM에 넘길 요약 — 원본 숫자를 그대로 넣되 분량을 통제한다."""
    return {
        "as_of": cycle["as_of"],
        "watchlist_ranked": [
            {
                "rank": s["rank"], "ticker": s["ticker"],
                "view_5d_pct": s["view_horizon_pct"],
                "confidence": s["confidence"],
                "up_path_ratio": s["up_path_ratio"],
                "mom_20d_pct": s["mom_20d_pct"],
                "realized_vol_pct": s["realized_vol_pct"],
            } for s in cycle["watchlist"]
        ],
        "target_weights_pct": {k: round(v * 100, 2) for k, v in cycle["target_weights"].items()},
        "previous_weights_pct": {k: round(v * 100, 2) for k, v in cycle["previous_weights"].items()},
        "orders": [{"ticker": o["ticker"], "side": o["side"], "notional": o["notional"]}
                    for o in cycle["orders"]],
        "equity": cycle["equity"],
        "period_return_pct": cycle["period_return_pct"],
        "bl_shift_bp": cycle["portfolio_meta"].get("bl_shift_bp", {}),
        "optimizer_fallback_used": cycle["portfolio_meta"].get("fallback_used", False),
        "risk_gate": cycle["portfolio_meta"].get("risk_gate", {}),
        "turnover_gate": cycle["portfolio_meta"].get("turnover_gate", {}),
        "cash_weight_pct": round(cycle.get("cash_weight", 0.0) * 100, 2),
        "degraded_tickers": cycle.get("degraded", []),
    }


def _llm_opinion(cycle):
    body = json.dumps({
        "model": config.PM_MODEL,
        "max_tokens": 700,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": "이번 사이클 데이터입니다. PM 논평을 작성하세요.\n\n"
                        + json.dumps(_build_payload(cycle), ensure_ascii=False, indent=2),
        }],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def _rule_opinion(cycle):
    """LLM 키가 없을 때 쓰는 결정론적 논평. 같은 입력이면 항상 같은 출력."""
    wl = cycle["watchlist"]
    tw = cycle["target_weights"]
    top = wl[0]
    weak = min(wl, key=lambda s: s["confidence"])
    held = sorted(tw.items(), key=lambda kv: -kv[1])
    parts = []

    most_conf = max(wl, key=lambda s: s["confidence"])
    parts.append(
        f"{cycle['as_of']} 기준, 주목도(확신도×견해크기) 1위는 {top['ticker']}로 5일 견해 "
        f"{top['view_horizon_pct']:+.2f}%에 확신도 {top['confidence']:.2f}, "
        f"경로의 {top['up_path_ratio']*100:.0f}%가 상승을 가리켰습니다. "
        f"확신도 자체가 가장 높은 종목은 {most_conf['ticker']}({most_conf['confidence']:.2f})입니다."
    )
    if held:
        parts.append(
            "배분 결과는 " + ", ".join(f"{t} {w*100:.1f}%" for t, w in held[:4]) + " 입니다."
        )

    # 확신도 낮은데 비중이 큰 종목 = 구조적 경고
    risky = [(t, w) for t, w in tw.items()
             if w > 0.20 and next((s["confidence"] for s in wl if s["ticker"] == t), 1) < 0.40]
    if risky:
        parts.append(
            "주의할 점은 " + ", ".join(f"{t}({w*100:.0f}%)" for t, w in risky)
            + "가 확신도가 낮은데도 비중이 큰 자리라는 것입니다. 경로가 넓다는 건 "
              "모델이 방향을 모른다는 뜻이므로 이 비중은 견해보다 공분산 구조에서 나왔을 가능성이 큽니다."
        )
    else:
        parts.append(
            f"확신도가 가장 낮은 {weak['ticker']}({weak['confidence']:.2f})는 비중이 "
            f"{tw.get(weak['ticker'], 0)*100:.1f}%로 억제돼 있어, Black-Litterman의 "
            "되돌림이 의도대로 작동한 것으로 보입니다."
        )

    if cycle["orders"]:
        buys = [o for o in cycle["orders"] if o["side"] == "BUY"]
        sells = [o for o in cycle["orders"] if o["side"] == "SELL"]
        parts.append(f"주문은 매수 {len(buys)}건, 매도 {len(sells)}건이 모의 생성됐고 "
                     "실제로 전송된 것은 없습니다.")
    else:
        parts.append("이번 사이클은 회전율 제약 안에서 조정할 것이 없어 주문이 없습니다.")

    if cycle["portfolio_meta"].get("fallback_used"):
        parts.append("다만 최적화가 실패해 동일가중으로 후퇴했습니다. 이 사이클의 비중은 "
                     "견해를 반영한 결과가 아니므로 그대로 신뢰하면 안 됩니다.")

    gate = cycle["portfolio_meta"].get("risk_gate") or {}
    if gate.get("triggered"):
        parts.append(f"리스크 게이트가 발동해({gate['reason']}) 목표 노출을 "
                     f"{gate['exposure']*100:.0f}%로 줄였습니다. 남은 자리는 현금이며, "
                     "이는 견해가 바뀌어서가 아니라 손실 한도 규칙이 작동한 결과입니다.")

    turn = cycle["portfolio_meta"].get("turnover_gate") or {}
    if turn.get("scaled"):
        parts.append(f"원래 목표는 기존 포지션의 {turn['sell_turnover']*100:.0f}%를 갈아엎어야 해서 "
                     f"한도 {turn['limit']*100:.0f}%를 넘었으므로, {turn['scale']*100:.0f}%만 "
                     "반영해 여러 사이클에 걸쳐 단계적으로 이동합니다.")

    parts.append("이 모델의 예측력은 독립 검증에서 무작위 걷기와 구분되지 않은 바 있으므로, "
                 "위 해석은 전부 잠정적입니다.")
    return " ".join(parts)


def material_change(cycle):
    """이 사이클에 '사람이 읽고 판단할 필요가 있는 일'이 일어났는가.

    일상적인 리밸런싱 주문은 여기 포함하지 않는다 — 거의 매 사이클 주문이 나오므로
    그걸 기준으로 삼으면 on_change 가 사실상 always 가 되어 비용이 전혀 절감되지 않는다.
    걸러내려는 것은 '평소와 다른 일'이다: 게이트 발동, 최적화 실패, 데이터 결손, 큰 손실.
    """
    meta = cycle.get("portfolio_meta") or {}
    ret = cycle.get("period_return_pct")
    return bool(
        cycle.get("degraded")
        or meta.get("fallback_used")
        or (meta.get("risk_gate") or {}).get("triggered")
        or (meta.get("turnover_gate") or {}).get("scaled")
        or (ret is not None and float(ret) <= config.PM_ALERT_LOSS_PCT)
    )


def should_call_llm(cycle, llm_calls=0):
    """LLM 을 호출할지 판정한다. 반환: (호출 여부, 사람이 읽을 사유)"""
    if not config.PM_ENABLED:
        return False, "API 키 없음"

    mode = config.PM_LLM_MODE
    if mode == "never":
        return False, "PM_LLM_MODE=never"
    if mode == "always":
        return True, "PM_LLM_MODE=always"
    if mode == "on_change":
        if llm_calls == 0:
            return True, "최초 논평"
        if material_change(cycle):
            return True, "포트폴리오 변경 발생"
        return False, "변경 없음 — 비용 절감"
    # 기본값 once
    if llm_calls == 0:
        return True, "최초 논평 (이후 사이클은 무료)"
    return False, "최초 1회 완료 — 비용 절감"


def opinion(cycle, llm_calls=0):
    """PM 논평을 반환한다.

    반환: (텍스트, 출처, LLM을 실제로 호출했는지)
    호출하지 않기로 했거나 실패하면 결정론적 룰 기반 논평으로 간다 — 이쪽은 언제나 무료다.
    """
    use_llm, reason = should_call_llm(cycle, llm_calls)
    if use_llm:
        try:
            text = _llm_opinion(cycle)
            if text:
                log.info("PM 논평: LLM 호출 (%s)", reason)
                return text, f"llm:{config.PM_MODEL}", True
            log.warning("LLM이 빈 응답을 반환 — 룰 기반으로 폴백")
        except Exception as e:
            log.warning("LLM 호출 실패(%s) — 룰 기반으로 폴백", e)
    else:
        log.info("PM 논평: 룰 기반 (%s)", reason)
    return _rule_opinion(cycle), f"rules:{reason}", False


def cycle_from_state(state):
    """저장된 state.json 을 논평 생성에 필요한 cycle 형태로 되돌린다.

    사이클을 다시 돌리지 않고 '이 사이클에 대한 LLM 논평만' 새로 받고 싶을 때 쓴다
    (대시보드의 'PM 논평 받기' 버튼). 시장 데이터를 다시 부르지 않으므로
    화면에 보이는 숫자와 논평이 반드시 같은 사이클을 가리킨다.
    """
    p = state.get("portfolio", {})
    return {
        "cycle_no": state.get("cycle_no"),
        "as_of": state.get("as_of"),
        "watchlist": state.get("watchlist", []),
        "picks": state.get("picks", []),
        "target_weights": p.get("target_weights", {}),
        "previous_weights": p.get("previous_weights", {}),
        "final_weights": p.get("weights", {}),
        "cash_weight": p.get("cash_weight", 0.0),
        "orders": state.get("orders", []),
        "equity": p.get("equity", 0.0),
        "period_return_pct": p.get("period_return_pct", 0.0),
        "portfolio_meta": p.get("meta", {}),
        "degraded": p.get("degraded", []),
    }


def watch_notes(cycle):
    """종목별 한 줄 코멘트 — 대시보드 '주목 종목' 카드에 붙는다.

    대시보드가 표시하는 것은 목표비중이 아니라 실제 체결 후 비중이므로,
    코멘트도 같은 값을 기준으로 써야 카드와 문장이 어긋나지 않는다.
    """
    notes = {}
    tw = cycle.get("final_weights") or cycle["target_weights"]
    for s in cycle["watchlist"]:
        tk, w = s["ticker"], tw.get(s["ticker"], 0.0)
        if w > 0.15 and s["confidence"] >= 0.5:
            n = "확신도와 비중이 함께 높은 핵심 보유"
        elif w > 0.15:
            n = "비중은 크지만 확신도가 낮음 — 공분산이 만든 자리일 가능성"
        elif w > 0:
            n = "소액 편입, 관찰 유지"
        elif s["confidence"] >= 0.6:
            n = "확신도는 높으나 견해 크기가 작아 미편입"
        else:
            n = "경로가 넓어 판단 보류"
        if abs(s["view_horizon_pct"]) > 3:
            n += f" · 견해 {s['view_horizon_pct']:+.1f}%는 이례적으로 큼, 재확인 필요"
        notes[tk] = n
    return notes
