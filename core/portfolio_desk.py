"""
포트폴리오 데스크 — skfolio.

Kronos 견해를 Black-Litterman 제약으로 넣고 CVaR 기준 MeanRisk를 푼다.
확신도가 낮으면 BL이 사후 기대수익을 사전분포(시장 균형) 쪽으로 되돌리므로
포트폴리오가 알아서 벤치마크에 가까워진다 — 이게 이 스택의 안전판이다.
"""
import logging

import numpy as np
import pandas as pd
from skfolio import RiskMeasure
from skfolio.optimization import MeanRisk, ObjectiveFunction
from skfolio.prior import BlackLitterman, EmpiricalPrior

from . import config

log = logging.getLogger("portfolio")


def optimize(returns, views, confidences, previous_weights=None):
    """
    returns          : 일간 수익률 DataFrame (컬럼 = 종목)
    views            : {ticker: 일간 기대수익}
    confidences      : {ticker: 0~1}
    previous_weights : 직전 비중 ndarray (없으면 회전율 제약 미적용)

    반환: (weights Series, meta dict)
    """
    assets = list(returns.columns)
    view_strs = [f"{tk} == {views[tk]:.8f}" for tk in assets]
    view_conf = [float(confidences[tk]) for tk in assets]

    bl = BlackLitterman(
        views=view_strs,
        view_confidences=view_conf,
        tau=config.BL_TAU,
        prior_estimator=EmpiricalPrior(),
    )

    kwargs = dict(
        objective_function=ObjectiveFunction.MINIMIZE_RISK,
        risk_measure=RiskMeasure.CVAR,
        cvar_beta=config.CVAR_BETA,
        prior_estimator=bl,
        min_weights=config.MIN_WEIGHT,
        max_weights=config.MAX_WEIGHT,
        budget=1.0,
        transaction_costs=config.TX_COST_BPS,
    )
    # 최초 편입에는 회전율 제약을 걸지 않는다 (무포지션 → 편입 자체가 대상이 아님).
    if previous_weights is not None:
        kwargs["max_turnover"] = config.MAX_TURNOVER
        kwargs["previous_weights"] = np.asarray(previous_weights, dtype=float)

    meta = {"solver": "CLARABEL", "fallback_used": False,
            "constraints": {"max_weight": config.MAX_WEIGHT,
                             "max_turnover": config.MAX_TURNOVER if previous_weights is not None else None,
                             "tx_cost_bps": config.TX_COST_BPS * 10000,
                             "cvar_beta": config.CVAR_BETA}}

    try:
        opt = MeanRisk(**kwargs)
        opt.fit(returns)
        weights = pd.Series(opt.weights_, index=assets)
    except Exception as e:
        # 최적화가 실패하면 조용히 이전 비중을 유지하지 않는다 — 실패를 명시하고
        # 동일가중으로 후퇴한다. 실패가 보이지 않는 게 제일 위험하다.
        log.error("최적화 실패, 동일가중으로 후퇴: %s", e)
        weights = pd.Series(1.0 / len(assets), index=assets)
        meta["fallback_used"] = True
        meta["fallback_reason"] = str(e)[:200]

    # 솔버가 남기는 1e-10 수준의 잔여값은 0으로 눌러준다 (표시·주문 노이즈 제거)
    weights = weights.clip(lower=0.0)
    weights[weights < 1e-6] = 0.0
    if weights.sum() > 0:
        weights = weights / weights.sum()

    # 사후 기대수익이 사전분포 대비 얼마나 움직였는지 = BL이 견해를 얼마나 받아들였나
    try:
        prior_mu = EmpiricalPrior().fit(returns).return_distribution_.mu
        post_mu = bl.fit(returns).return_distribution_.mu
        meta["bl_shift_bp"] = {
            tk: round(float((post_mu[i] - prior_mu[i]) * 10000), 3)
            for i, tk in enumerate(assets)
        }
    except Exception:
        meta["bl_shift_bp"] = {}

    return weights, meta


def enforce_turnover(target, current, limit=None, skip=False):
    """집행 직전 회전율 재검증.

    옵티마이저의 max_turnover 는 '옵티마이저가 본 유니버스' 안에서만 성립한다.
    유니버스에서 탈락한 보유 종목은 그 계산에 들어가지 않으므로, 실제 회전율은
    제약을 넘을 수 있다. 여기서 전체 유니버스 기준으로 다시 재고, 넘으면
    목표를 현재 비중 쪽으로 선형 보간해 한도 안으로 되돌린다.

    ■ 회전율은 '매도측'으로 잰다
    이 게이트가 막으려는 것은 기존 포지션을 통째로 갈아엎는 과잉 매매다.
    현금에서 신규로 편입하는 것은 교체가 아니라 배치이므로 회전으로 세지 않는다.
    양측 합(|Δ| 총합)으로 재면 최초 편입이 언제나 100%로 잡혀서, 아직 아무것도
    사지 않은 포트폴리오가 영원히 현금에 묶인다.

    skip=True 면 게이트를 통과시킨다 (리스크 게이트가 노출을 줄이는 중일 때).

    반환: (조정된 목표 Series, 진단 dict)
    """
    limit = config.TURNOVER_HARD_LIMIT if limit is None else limit
    idx = target.index.union(current.index)
    t = target.reindex(idx).fillna(0.0)
    c = current.reindex(idx).fillna(0.0)

    gross = float((t - c).abs().sum())        # 양측 합 (참고용)
    sell = float((c - t).clip(lower=0).sum())  # 매도측 = 실제 교체량
    info = {"gross_turnover": round(gross, 6), "sell_turnover": round(sell, 6),
            "limit": limit, "scaled": False, "scale": 1.0,
            "final_turnover": round(sell, 6), "skipped": False}

    if skip:
        info["skipped"] = True
        return t, info
    if sell <= limit or sell <= 1e-12:
        return t, info

    alpha = limit / sell
    adjusted = c + alpha * (t - c)
    info.update(scaled=True, scale=round(alpha, 4),
                final_turnover=round(float((c - adjusted).clip(lower=0).sum()), 6))
    log.warning("매도 회전율 %.1f%% > 한도 %.1f%% — 목표를 %.0f%%만 반영",
                sell * 100, limit * 100, alpha * 100)
    return adjusted, info


def apply_risk_gate(target, drawdown, last_cycle_return):
    """손실 한도에 걸리면 목표 노출을 줄인다.

    비중의 상대 구조(어느 종목을 얼마나 선호하는지)는 그대로 두고 전체 크기만
    줄인다. 남는 부분은 현금으로 간다 — 집행 데스크가 현금 장부를 가지므로
    노출 축소가 그대로 현금 비중 증가로 나타난다.

    반환: (조정된 목표 Series, 진단 dict)
    """
    info = {"triggered": False, "reason": None, "exposure": 1.0,
            "drawdown_pct": round((drawdown or 0.0) * 100, 3)}
    if not config.RISK_GATE_ENABLED:
        return target, info

    reasons = []
    exposure = 1.0
    if drawdown is not None and drawdown >= config.MAX_DRAWDOWN_LIMIT:
        reasons.append(f"누적 낙폭 {drawdown*100:.1f}% ≥ 한도 {config.MAX_DRAWDOWN_LIMIT*100:.0f}%")
        exposure = min(exposure, config.DRAWDOWN_EXPOSURE)
    if last_cycle_return is not None and last_cycle_return <= -config.CYCLE_LOSS_LIMIT:
        reasons.append(f"직전 사이클 손실 {last_cycle_return*100:.1f}% ≥ 한도 {config.CYCLE_LOSS_LIMIT*100:.0f}%")
        exposure = min(exposure, config.DRAWDOWN_EXPOSURE)

    if not reasons:
        return target, info

    info.update(triggered=True, reason=" · ".join(reasons), exposure=exposure)
    log.warning("리스크 게이트 발동 (%s) — 목표 노출 %.0f%%로 축소",
                info["reason"], exposure * 100)
    return target * exposure, info
