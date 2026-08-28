"""
매크로 데스크·뉴스 데스크·브리핑 테스트 — 네트워크를 쓰지 않는다.

여기서 고정하는 것은 **단위와 분류를 틀리지 않는가**이다. 매크로 화면에서 조용히
틀리는 두 가지가 있다: ①금리를 가격처럼 다뤄서 채권 판정이 뒤집히는 것,
②지역·기업 단신이 매크로 뉴스로 올라와 화면 전체의 신뢰가 깎이는 것.
둘 다 에러를 내지 않고 그럴듯하게 표시되므로 테스트로 고정한다.
"""
import numpy as np
import pandas as pd
import pytest

from core import macro_brief, macro_desk as M, news_desk as N


def series(vals, start="2025-01-01"):
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)))


def ramp(n=400, start=100.0, step=0.1):
    return series(list(np.arange(start, start + n * step, step))[:n])


# ============================================================ 단위 (금리 vs 가격)
def test_금리는_변화를_퍼센트포인트로_잰다():
    """4.0%→4.5% 는 '+0.5%p' 이지 '+12.5%' 가 아니다."""
    s = series([4.0] * 60 + [4.5])
    snap = M.snapshot("^TNX", s)
    assert snap["unit"] == "%p"
    assert snap["changes"]["1d"] == pytest.approx(0.5)


def test_가격은_변화를_퍼센트로_잰다():
    s = series([100.0] * 60 + [110.0])
    snap = M.snapshot("SPY", s)
    assert snap["unit"] == "%"
    assert snap["changes"]["1d"] == pytest.approx(10.0)


def test_금리에는_낙폭_태그를_붙이지_않는다():
    """금리가 고점 대비 낮은 것은 손실이 아니다."""
    s = series(list(np.linspace(5.0, 3.0, 300)))       # 고점 대비 −40%
    tags = [t["tag"] for t in M.snapshot("^TNX", s)["state"]]
    assert "약세장" not in tags and "조정" not in tags


def test_VIX_에도_낙폭_태그를_붙이지_않는다():
    """VIX 가 고점 대비 −53% 인 것은 '약세장'이 아니라 '시장이 진정됐다'는 뜻이다."""
    s = series(list(np.linspace(40.0, 14.0, 300)))
    tags = [t["tag"] for t in M.snapshot("^VIX", s)["state"]]
    assert "약세장" not in tags


def test_환율에는_낙폭_태그를_붙이지_않는다():
    """원/달러가 −20% 면 원화 초강세인데 '약세장'이라 부르면 정반대로 읽힌다."""
    s = series(list(np.linspace(1500.0, 1200.0, 300)))
    tags = [t["tag"] for t in M.snapshot("KRW=X", s)["state"]]
    assert "약세장" not in tags


def test_주가지수에는_낙폭_태그를_붙인다():
    s = series(list(np.linspace(9000.0, 6800.0, 300)))
    tags = [t["tag"] for t in M.snapshot("^KS11", s)["state"]]
    assert "약세장" in tags


# ==================================================================== 판정
def test_많이_올랐지만_지금_빠지는_중을_구분해서_말한다():
    """연초 +58% 이면서 고점 대비 −25% 인 국면이 실제로 있다(2026 코스피).

    실제 값을 본떴다: 2025년 말까지 4,300 근처 → 6월 9,114 고점 → 8월 6,808,
    200일선 5,996. 200일선 위(상승 추세)이면서 3개월은 크게 빠지는 상태다."""
    flat = list(np.linspace(3900.0, 4300.0, 150))
    up = list(np.linspace(4300.0, 9100.0, 80))
    down = list(np.linspace(9100.0, 6800.0, 70))
    snap = M.snapshot("^KS11", series(flat + up + down))
    assert snap["trend"]["label"] == "상승 추세"        # 200일선 위
    assert snap["trend"]["conflict"] is True            # 그러나 3개월은 역방향
    assert "역방향" in snap["trend"]["reason"]
    assert snap["range52w"]["drawdown_pct"] < -20


def test_계산할_수_없는_값은_0_이_아니라_None():
    snap = M.snapshot("SPY", series([100.0] * 40))      # 200일선을 만들 수 없다
    assert snap["ma200"] is None
    assert snap["vs_ma200_pct"] is None
    assert snap["trend"]["label"] == "판정 불가"


def test_교차신호는_한쪽_데이터가_없으면_만들지_않는다():
    closes = {"HG=F": ramp()}                           # GC=F 없음
    out = M.cross_signals({}, closes)
    assert not [c for c in out if c["key"] == "copper_gold"]


def test_장단기_금리차_역전을_표시한다():
    closes = {"^TNX": series([3.0] * 100), "^IRX": series([4.0] * 100)}
    yc = next(c for c in M.cross_signals({}, closes) if c["key"] == "yield_curve")
    assert yc["inverted"] is True
    assert "역전" in yc["reading"]


# ================================================================= 뉴스 분류
def test_금액표현의_달러는_외환뉴스가_아니다():
    """'500만달러 수출' 이 환율 뉴스로 올라오던 실제 오분류."""
    assert "fx" not in N.classify("고흥군, 몽골·중국서 500만달러 농수산물 수출 협약")


def test_지역_단신은_걸러낸다():
    assert N.is_noise("충북교육청, 창의예술교육 활성화 업무협약")
    assert N.is_noise("[특징주] 삼바, 6.7% 급락 마감")
    assert N.is_noise("강원 고유가 피해지원금 44억원 미사용")


def test_지명_목록에_없는_지역_단신은_주제_문턱에서_걸린다():
    """지명을 전부 열거할 수는 없다. 두 번째 방어선은 '주제가 안 잡히면 버린다'이다.
    '영덕군'은 지명 목록에 없지만, 매크로 키워드에 걸리는 것이 없어 화면에 오르지 않는다."""
    title = "영덕군 병곡면에 150실 규모 관광호텔 건립…105명 고용"
    assert N.classify(title) == []


def test_매크로_표제_기사는_노이즈_규칙에서_살린다():
    assert not N.is_noise("[외환] 원/달러 환율 8.4원 내린 1,372.5원")


def test_제목_매치가_요약_매치보다_무겁다():
    """제목에 '유가'가 있으면 유가 기사지만, 본문 한 번 언급은 배경일 때가 많다."""
    strong = N.classify("국제유가 급등, 브렌트유 배럴당 90달러 돌파")
    assert "energy" in strong


def test_아무_주제에도_안_걸리면_빈_목록을_돌려준다():
    """빈 목록 = 매크로 화면에 올리지 않는다. 억지로 'econ' 에 넣지 않는다."""
    assert N.classify("배우 아무개, 신작 영화 촬영 시작") == []


def test_피드_힌트는_주제가_안_잡힐_때만_쓴다():
    assert N.classify("Weekly market wrap", hint="crypto") == ["crypto"]


# =================================================================== 브리핑
def _fake_macro():
    """브리핑이 소비하는 모양의 최소 지표 묶음."""
    def snap(sym, close):
        return M.snapshot(sym, close)
    closes = {
        "SPY": ramp(400, 500.0, 0.5), "QQQ": ramp(400, 400.0, 0.6),
        "IWM": ramp(400, 200.0, 0.2), "^KS11": ramp(400, 4000.0, 5.0),
        "EEM": ramp(400, 50.0, 0.05), "^VIX": series([14.0] * 300),
        "^TNX": series(list(np.linspace(4.0, 4.7, 300))),
        "^FVX": series([4.3] * 300), "^IRX": series([3.6] * 300),
        "TLT": series(list(np.linspace(95.0, 83.0, 300))),
        "HYG": ramp(300, 75.0, 0.02), "LQD": ramp(300, 105.0, 0.01),
        "DX-Y.NYB": series([99.0] * 300),
        "KRW=X": series(list(np.linspace(1500.0, 1370.0, 300))),
        "EURUSD=X": series([1.16] * 300), "JPY=X": series([159.0] * 300),
        "CL=F": ramp(300, 60.0, 0.08), "BZ=F": ramp(300, 65.0, 0.08),
        "NG=F": series(list(np.linspace(4.0, 2.9, 300))), "XLE": ramp(300, 50.0, 0.05),
        "GC=F": ramp(300, 4000.0, 2.0), "SI=F": ramp(300, 60.0, 0.03),
        "HG=F": ramp(300, 5.5, 0.004), "PL=F": series([1866.0] * 300),
        "BTC-USD": ramp(400, 60000.0, 50.0), "ETH-USD": ramp(400, 2000.0, 1.5),
        "SOL-USD": ramp(400, 90.0, 0.05),
    }
    assets = {s: snap(s, c) for s, c in closes.items()}
    return {"asof": "2026-08-28", "assets": assets,
            "cross": M.cross_signals(assets, closes)}


def test_브리핑은_모든_주제를_만든다():
    b = macro_brief.build(_fake_macro())
    topics = {x["topic"] for x in b["briefs"]}
    assert topics == {"stocks", "rates", "fx", "energy", "metals", "crypto", "credit"}


def test_모든_대응에는_대가가_적혀_있다():
    """대가를 안 적은 조언은 조언이 아니다 — 이 규율을 테스트로 고정한다."""
    b = macro_brief.build(_fake_macro())
    for brief in b["briefs"]:
        for a in brief["actions"]:
            assert a["what"] and a["why"] and a["risk"], f"{brief['topic']} 대응에 대가 누락"


def test_전망은_예측이_아니라_조건문임을_명시한다():
    b = macro_brief.build(_fake_macro())
    for brief in b["briefs"]:
        assert "예측이 아니라 조건" in brief["outlook"]["disclaimer"]


def test_원화_강세는_보유자와_매수자에게_반대로_작용한다고_말한다():
    """같은 사실이 두 사람에게 정반대로 작용하는데 한쪽만 말하면 오해를 만든다."""
    b = macro_brief.build(_fake_macro())
    fx = next(x for x in b["briefs"] if x["topic"] == "fx")
    assert "원화가 강해졌다" in fx["comment"]
    assert "반대로 작용" in fx["comment"]


def test_한_주제가_죽어도_나머지_주제는_나온다():
    macro = _fake_macro()
    macro["assets"]["SPY"] = {"이상한": "모양"}          # 주식 주제를 깨뜨린다
    b = macro_brief.build(macro)
    assert {x["topic"] for x in b["briefs"]} >= {"rates", "fx", "metals"}


def test_브리핑은_LLM_을_부르지_않는다():
    """대시보드 새로고침에 돈이 들면 안 된다."""
    src = (macro_brief.__file__)
    with open(src, encoding="utf-8") as f:
        text = f.read()
    for banned in ("anthropic", "openai", "requests.post", "pm_desk"):
        assert banned not in text.lower(), f"매크로 브리핑에 {banned} 경로가 생겼다"
