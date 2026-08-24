"""
Kronos 추론 캐시.

백테스트는 (종목 × 리밸런싱 횟수)만큼 추론이 필요하다. 10종목 × 60회면
CPU로 10분 남짓 — 한 번은 견딜 만하지만, 파라미터를 바꿔가며 여러 번 돌리려면
그때마다 10분을 다시 태우게 된다. 그런데 `TOP_K`·`MAX_TURNOVER`·게이트 한도처럼
**추론 이후 단계의 파라미터**는 추론 결과를 바꾸지 않는다. 그러니 한 번 뽑은
견해를 저장해두면 이후 스윕은 거의 공짜가 된다.

■ 캐시 키에 무엇이 들어가는가
`(종목, 기준일)` 만으로는 부족하다. 모델·룩백·예측 길이·샘플 수·온도·top_p 가
바뀌면 같은 종목·같은 날짜라도 견해가 달라진다. 이 값들을 전부 지문에 넣어서,
추론 파라미터를 건드리면 캐시가 **자동으로 무효화**되게 한다. 이걸 빠뜨리면
"파라미터를 바꿨는데 결과가 똑같다"는 가장 악질적인 형태의 침묵 버그가 된다.

■ 샘플링 모델을 캐시해도 되는가
Kronos 는 확률적 샘플링이라 같은 입력에도 매번 다른 경로가 나온다. 캐시는 그
난수를 한 시점에 고정한다. 이건 하류 파라미터 스윕에 오히려 필요한 성질이다 —
고정하지 않으면 `TOP_K` 를 바꿔서 성과가 달라진 건지 추론 난수가 달라진 건지
구분할 수 없다. 다만 **같은 설정으로 여러 번 돌려 견해의 안정성을 보고 싶을 때는
캐시를 꺼야 한다**(`--no-cache`).
"""
import json
import logging

from . import config

log = logging.getLogger("cache")

DEFAULT_PATH = config.STATE_DIR / "infer_cache.jsonl"


def fingerprint():
    """추론 결과를 바꾸는 설정만 모은 지문."""
    return "|".join(str(v) for v in (
        config.KRONOS_MODEL, config.KRONOS_TOKENIZER, config.LOOKBACK,
        config.PRED_LEN, config.SAMPLE_COUNT, config.TEMPERATURE, config.TOP_P,
    ))


class InferenceCache:
    """종목·기준일 단위로 스코어 dict 를 파일에 적재해두는 append-only 캐시."""

    def __init__(self, path=None, enabled=True):
        self.path = path or DEFAULT_PATH
        self.enabled = enabled
        self.fp = fingerprint()
        self.entries = {}
        self.hits = 0
        self.misses = 0
        self._fh = None
        if self.enabled:
            self._load()

    # ------------------------------------------------------------------ 적재
    def _load(self):
        if not self.path.exists():
            return
        stale = 0
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue                      # 쓰다 만 줄은 버린다
                if rec.get("fp") != self.fp:
                    stale += 1
                    continue                      # 다른 추론 설정으로 만든 항목
                self.entries[rec["key"]] = rec["score"]
        log.info("추론 캐시 로드: %d건%s", len(self.entries),
                 f" (설정이 달라 무시한 항목 {stale}건)" if stale else "")

    def _append(self, key, score):
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps({"fp": self.fp, "key": key, "score": score},
                                   ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # ------------------------------------------------------------------ 사용
    @staticmethod
    def make_key(ticker, asof):
        return f"{ticker}@{str(asof)[:10]}"

    def wrap(self, scorer=None):
        """스코어러를 캐시로 감싼다. `research_desk.scan(scorer=...)` 에 넣는다."""
        from . import research_desk
        inner = scorer or research_desk.score_ticker

        def cached(ticker, bars, asof=None):
            stamp = asof if asof is not None else bars.index[-1]
            key = self.make_key(ticker, stamp)
            if self.enabled and key in self.entries:
                self.hits += 1
                # 호출부가 dict 를 손대므로(확신도·순위 부여) 사본을 준다
                return dict(self.entries[key])
            score = inner(ticker, bars, asof=stamp)
            self.misses += 1
            if self.enabled:
                self.entries[key] = score
                self._append(key, score)
            return dict(score)

        return cached

    @property
    def stats(self):
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate_pct": round(self.hits / total * 100, 1) if total else None,
            "entries": len(self.entries),
            "path": str(self.path),
            "enabled": self.enabled,
        }
