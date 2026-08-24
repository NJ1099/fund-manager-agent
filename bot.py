#!/usr/bin/env python3
"""
클로드 펀드매니저 에이전트 — 메인 루프.

한 사이클:
  데이터 → 연구(Kronos, 워치리스트 전체 스캔) → 상위 K 선별
        → 포트폴리오(skfolio BL + CVaR) → 리스크·회전율 게이트
        → 집행(NautilusTrader 주문 객체) → PM 논평 → state.json 기록

사용법:
  python bot.py once           한 사이클만 실행
  python bot.py loop           CYCLE_INTERVAL_SEC 마다 반복 (테스트용)
  python bot.py schedule       매 영업일 16:05 ET 실행

⚠️ 전 구간 모의투자. 브로커로 전송되는 주문은 없습니다.
"""
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from core import (config, cycle as cycle_core, data_desk, execution_desk,
                  performance, pm_desk, storage)

# 로그가 전부 한국어라 Windows 기본 콘솔(cp949)에서 깨지거나 인코딩 오류로 죽는다.
# 표준 출력만 UTF-8 로 돌려놓는다 (파일 입출력은 각 호출부에서 이미 명시).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


class Agent:
    def __init__(self):
        self.exec_desk = execution_desk.ExecutionDesk()
        self.cycle_no = 0
        self.prev_equity = config.INITIAL_EQUITY   # 직전 사이클 마감 자산
        self.peak_equity = config.INITIAL_EQUITY   # 낙폭 계산 기준 고점
        self.start_equity = config.INITIAL_EQUITY  # 최초 원금 (누적수익 기준)
        self.last_cycle_return = None
        self.llm_calls = 0                         # 지금까지 실제로 나간 LLM 호출 수
        self._restore()

    # ------------------------------------------------------------ 상태 복원
    def _restore(self):
        """재시작해도 포지션과 자산이 이어지도록 마지막 상태를 읽는다."""
        if not config.STATE_FILE.exists():
            return
        try:
            st = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
            p = st.get("portfolio", {})
            self.exec_desk.restore(
                cash=p.get("cash", config.INITIAL_EQUITY),
                positions=p.get("positions", {}),
                seq=st.get("order_seq", 0),
                costs_paid=p.get("costs_paid", 0.0),
                last_prices=st.get("prices", {}),
            )
            self.cycle_no = st.get("cycle_no", 0)
            self.prev_equity = p.get("equity", config.INITIAL_EQUITY)
            self.peak_equity = p.get("peak_equity", self.prev_equity)
            self.start_equity = p.get("start_equity", config.INITIAL_EQUITY)
            self.last_cycle_return = (p.get("period_return_pct") or 0.0) / 100.0
            self.llm_calls = st.get("llm_calls", 0)
            log.info("이전 상태 복원: 사이클 %d, 자산 $%.0f, 보유 %d종목, 현금 $%.0f",
                     self.cycle_no, self.prev_equity,
                     len(self.exec_desk.positions), self.exec_desk.cash)
        except Exception as e:
            log.warning("상태 복원 실패, 초기 상태로 시작: %s", e)

    # ------------------------------------------------------------ 한 사이클
    def run_cycle(self):
        t_start = time.time()
        self.cycle_no += 1
        log.info("=== 사이클 %d 시작 ===", self.cycle_no)

        # 1) 데이터 — 워치리스트 + 벤치마크
        universe = list(dict.fromkeys(config.WATCHLIST + [config.BENCHMARK]))
        bars = data_desk.fetch_bars(universe)

        # 2~5) 연구 → 선별 → 최적화 → 리스크·회전율 게이트.
        # 백테스트(core/backtest.py)가 부르는 것과 **같은 함수**다.
        # 여기서 로직이 갈라지면 백테스트는 실제로 돌아가는 전략이 아니라
        # 백테스트에만 있는 전략을 측정하게 된다.
        plan = cycle_core.plan(bars, self.exec_desk, self.prev_equity,
                                self.peak_equity, self.last_cycle_return)
        as_of = plan["as_of"]
        prices_all = plan["prices"]
        benchmark_close = prices_all.get(config.BENCHMARK)
        watchlist = plan["scores"]
        picks = plan["picks"]
        target_w = plan["target_weights"]
        current_w = plan["current_weights"]
        pmeta = plan["meta"]

        log.info("스캔 완료: %s", ", ".join(
            f"{s['ticker']}({s['confidence']:.2f})" for s in watchlist[:5]))
        log.info("선별 %d종목: %s", len(picks), picks)

        # 5) 집행
        equity_open = self.exec_desk.equity(prices_all)   # 새 가격 반영, 거래 전
        period_ret = (equity_open / self.prev_equity - 1.0) if self.prev_equity else 0.0
        previous_weights = current_w.to_dict()
        orders, final_w = self.exec_desk.rebalance(target_w, prices_all, as_of)

        equity_close = self.exec_desk.equity(prices_all)
        self.peak_equity = max(self.peak_equity, equity_close)
        self.last_cycle_return = period_ret

        # 6) PM 논평
        cycle = {
            "cycle_no": self.cycle_no,
            "as_of": as_of.strftime("%Y-%m-%d"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "watchlist": watchlist,
            "picks": picks,
            "target_weights": {k: float(v) for k, v in target_w.items() if float(v) > 0},
            "previous_weights": {k: float(v) for k, v in previous_weights.items()},
            "orders": orders,
            "equity": round(equity_close, 2),
            "period_return_pct": round(period_ret * 100, 3),
            "portfolio_meta": pmeta,
            "degraded": self.exec_desk.degraded,
            "final_weights": {k: float(v) for k, v in final_w.items()},
            "cash_weight": self.exec_desk.cash_weight(prices_all),
        }
        text, source, used_llm = pm_desk.opinion(cycle, self.llm_calls)
        if used_llm:
            self.llm_calls += 1
        cycle["pm_opinion"] = text
        cycle["pm_source"] = source
        cycle["watch_notes"] = pm_desk.watch_notes(cycle)
        cycle["elapsed_sec"] = round(time.time() - t_start, 1)

        self.prev_equity = equity_close
        self._persist(cycle, prices_all, final_w, benchmark_close)
        log.info("=== 사이클 %d 완료 (%.0fs) 자산 $%.0f (%+.2f%%) ===",
                 self.cycle_no, cycle["elapsed_sec"], equity_close, period_ret * 100)
        return cycle

    # ------------------------------------------------------------ 저장
    def _append_history(self, record):
        """이력 한 줄을 덧붙이고, 상한을 넘으면 오래된 줄부터 잘라낸다."""
        with config.HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        lines = config.HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > config.HISTORY_MAX_LINES:
            keep = lines[-config.HISTORY_MAX_LINES:]
            storage.atomic_write(config.HISTORY_FILE, "\n".join(keep) + "\n")
            log.info("이력 정리: %d줄 → %d줄", len(lines), len(keep))
            lines = keep

        out = []
        for line in lines:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def _persist(self, cycle, prices_all, final_w, benchmark_close):
        # 이력 레코드도 백테스트와 같은 생성자를 쓴다 — 모양이 갈라지면
        # performance.summarize() 가 두 곳에서 다른 것을 계산하게 된다.
        record = cycle_core.history_record(
            cycle_no=cycle["cycle_no"],
            as_of=cycle["as_of"],
            exec_desk=self.exec_desk,
            equity=cycle["equity"],
            start_equity=self.start_equity,
            period_return_pct=cycle["period_return_pct"] / 100.0,
            n_orders=len(cycle["orders"]),
            weights=final_w,
            top_pick=cycle["watchlist"][0]["ticker"],
            benchmark_close=benchmark_close,
            prices=prices_all,
        )
        history = self._append_history(record)
        perf = performance.summarize(history)

        state = {
            "meta": {
                "execution_mode": config.EXECUTION_MODE,
                "live_orders_allowed": config.ALLOW_LIVE_ORDERS,
                "pm_source": cycle["pm_source"],
                "pm_llm_mode": config.PM_LLM_MODE,
                "pm_llm_available": config.PM_ENABLED,
                "kronos_model": config.KRONOS_MODEL,
                "sample_count": config.SAMPLE_COUNT,
                "pred_len": config.PRED_LEN,
                "lookback": config.LOOKBACK,
                "top_k": config.TOP_K,
                "watchlist_size": len(config.WATCHLIST),
                "benchmark": config.BENCHMARK,
                "max_turnover": config.MAX_TURNOVER,
                "turnover_hard_limit": config.TURNOVER_HARD_LIMIT,
                "tx_cost_bps": config.TX_COST_BPS * 10000,
                "max_weight": config.MAX_WEIGHT,
                "max_drawdown_limit": config.MAX_DRAWDOWN_LIMIT,
                "versions": {
                    "skfolio": __import__("skfolio").__version__,
                    "nautilus_trader": __import__("nautilus_trader").__version__,
                },
            },
            "cycle_no": cycle["cycle_no"],
            "as_of": cycle["as_of"],
            "generated_at": cycle["generated_at"],
            "elapsed_sec": cycle["elapsed_sec"],
            "order_seq": self.exec_desk.seq,
            "llm_calls": self.llm_calls,
            "prices": prices_all,
            "portfolio": {
                "equity": cycle["equity"],
                "start_equity": self.start_equity,
                "peak_equity": round(self.peak_equity, 2),
                "period_return_pct": cycle["period_return_pct"],
                "weights": {k: float(v) for k, v in final_w.items()},
                "target_weights": cycle["target_weights"],
                "previous_weights": cycle["previous_weights"],
                "meta": cycle["portfolio_meta"],
                "degraded": cycle["degraded"],
                **self.exec_desk.snapshot(prices_all),
            },
            "performance": perf,
            "equity_curve": performance.equity_curve(history),
            "watchlist": cycle["watchlist"],
            "watch_notes": cycle["watch_notes"],
            "picks": cycle["picks"],
            "orders": cycle["orders"],
            "pm_opinion": cycle["pm_opinion"],
            "pm_source": cycle["pm_source"],
            "history": history[-120:],
        }
        # 원자적 쓰기 — 대시보드가 15초마다 폴링하므로, 부분적으로 쓰인 파일을
        # 읽으면 JSON 파싱이 깨진다. 임시 파일에 다 쓴 뒤 한 번에 교체한다.
        storage.write_json(config.STATE_FILE, state)
        log.info("상태 저장: %s", config.STATE_FILE)


def _seconds_until_next_run():
    """다음 영업일 16:05 ET 까지 남은 초.

    zoneinfo 로 실제 미국 동부 시간을 쓴다 — UTC-4 고정 근사는 겨울(EST)에
    한 시간씩 어긋난다. 미국 공휴일은 반영하지 않는다(휴장일 사이클은
    직전 영업일 데이터로 돌고 주문이 거의 나오지 않는다).
    """
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:                       # tzdata 가 없는 환경 폴백
        et = timezone(timedelta(hours=-4))
        log.warning("zoneinfo 사용 불가 — UTC-4 근사로 대체 (겨울철 1시간 오차)")

    now = datetime.now(et)
    target = now.replace(hour=config.CYCLE_HOUR_ET, minute=config.CYCLE_MINUTE_ET,
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while target.weekday() >= 5:            # 토·일 건너뛰기
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    agent = Agent()

    if mode == "once":
        agent.run_cycle()

    elif mode == "loop":
        interval = config.CYCLE_INTERVAL_SEC or 3600
        log.info("루프 모드: %d초 간격", interval)
        while True:
            try:
                agent.run_cycle()
            except Exception as e:
                log.exception("사이클 실패, 다음 주기에 재시도: %s", e)
            time.sleep(interval)

    elif mode == "schedule":
        log.info("스케줄 모드: 매 영업일 %02d:%02d ET",
                 config.CYCLE_HOUR_ET, config.CYCLE_MINUTE_ET)
        while True:
            wait = _seconds_until_next_run()
            log.info("다음 실행까지 %.1f시간 대기", wait / 3600)
            time.sleep(wait)
            try:
                agent.run_cycle()
            except Exception as e:
                log.exception("사이클 실패: %s", e)
            time.sleep(60)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
