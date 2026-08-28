"""
매크로 브리핑 — 주제별 판독과 대응 후보.

`macro_desk` 가 만든 **사실**(추세·위치·모멘텀·변동성)을 읽어서, 주제마다
"지금 어떤 상태인가 / 무엇이 갈림길인가 / 어떤 선택지가 있는가"를 문장으로 만든다.
LLM 을 부르지 않는다 — 규칙이 전부 이 파일에 드러나 있어야 검증할 수 있고,
화면 새로고침에 돈이 들면 안 된다.

■ 세 가지를 엄격히 구분한다

| 구분 | 무엇 | 믿어도 되는가 |
|---|---|---|
| `facts` | 가격·수익률·변동성·위치 | **그렇다.** 계산된 사실이다 |
| `comment` | 그 사실이 뜻하는 것 | 해석이다. 근거 수치를 함께 적는다 |
| `outlook` | 앞으로의 방향 | **예측이 아니라 조건문이다** |

`outlook` 을 조건문으로만 쓰는 이유가 있다. 이 프로젝트의 예측 모델은
`core/validation.py` 로 실제 검증했을 때 방향 적중률이 "늘 상승으로 찍기"에도
못 미쳤다. 검증되지 않은 예측력으로 "오를 것이다"를 쓰는 것은 정직하지 않다.
대신 **무엇이 확인되면 어느 쪽으로 기우는지**를 적는다. 그건 사실 관계라
틀려도 검증 가능하다.

■ 대응은 지시가 아니라 선택지다

`actions` 는 "사라/팔아라"가 아니라 "이런 상황이면 보통 이런 선택지가 있고,
그 대가는 이것이다"로 쓴다. 각 항목에 `risk`(이 선택이 틀렸을 때 무엇을 잃는가)를
반드시 붙인다 — 대가를 안 적은 조언은 조언이 아니다.

■ 한국 투자자 기준

원화가 기준 통화라고 가정한다. 그래서 달러 자산의 성과는 환율과 함께 봐야 하고,
환율 항목은 "달러가 강하다"가 아니라 "내 해외자산의 원화 환산액에 무엇이
일어나는가"로 쓴다.
"""
import logging

log = logging.getLogger("brief")

# 대응 문구를 만드는 문턱값. 판정 근거를 숨기지 않기 위해 상단에 모아 둔다.
STRONG_MOVE_1M = 5.0        # 한 달 이 정도 움직이면 '크게 움직였다'
BIG_MOVE_3M = 10.0
RATE_MOVE_3M_BP = 25.0      # 3개월 금리 변화 25bp 이상이면 유의미
VIX_CALM = 15.0
VIX_ELEVATED = 20.0
VIX_STRESS = 28.0


def _get(snaps, sym):
    return snaps.get(sym)


def _fmt(v, unit="%", sign=True):
    if v is None:
        return "—"
    return f"{v:+.2f}{unit}" if sign else f"{v:.2f}{unit}"


def _n(v, fmt="{:.0f}"):
    """None 이면 대시. **0 으로 채우지 않는다** — 상수 시계열이나 데이터 부족으로
    52주 위치·RSI·변동성이 없을 수 있고, 0 으로 채우면 '바닥이다'로 잘못 읽힌다.
    (실제로 이걸 빼먹어서 상수 시세에서 브리핑이 통째로 죽었다.)"""
    return "—" if v is None else fmt.format(v)


def _tags(a):
    return [t["tag"] for t in (a.get("state") or [])]


def _bias(score):
    """점수를 방향 표현으로. 점수는 각 주제 함수가 자기 기준으로 만든다."""
    if score >= 2:
        return "상방 우위"
    if score == 1:
        return "약한 상방"
    if score == 0:
        return "중립"
    if score == -1:
        return "약한 하방"
    return "하방 우위"


# ===================================================================== 주식
def _stocks(snaps, cross):
    spy, qqq, iwm, ks, eem = (_get(snaps, s) for s in ("SPY", "QQQ", "IWM", "^KS11", "EEM"))
    vix = _get(snaps, "^VIX")
    facts, comments, actions, conds = [], [], [], []
    score = 0

    for a in (spy, qqq, iwm, ks, eem):
        if not a:
            continue
        dd = a["range52w"].get("drawdown_pct")
        facts.append({
            "label": a["name"],
            "value": f"{a['last']:,}",
            "detail": (f"1주 {_fmt(a['changes']['1w'])} · 3개월 {_fmt(a['changes']['3m'])} · "
                       f"YTD {_fmt(a['ytd'])} · 고점대비 {_fmt(dd)}"),
            "trend": a["trend"]["label"],
            "tags": _tags(a),
        })

    if spy:
        m3 = spy["changes"]["3m"]
        score += 1 if spy["trend"]["label"] == "상승 추세" else -1
        if m3 is not None and m3 < -BIG_MOVE_3M:
            score -= 1
        comments.append(
            f"미국 대형주는 {spy['trend']['label']}({spy['trend']['reason']}), "
            f"52주 범위의 {_n(spy['range52w']['position_pct'])}% 지점에 있다.")

    # 코스피는 한국 투자자에게 별도 문단을 줄 값어치가 있다.
    if ks:
        dd = ks["range52w"].get("drawdown_pct")
        if dd is not None and dd <= -15:
            comments.append(
                f"코스피는 연초 대비 {_fmt(ks['ytd'])}이지만 6월 고점 대비 {_fmt(dd)}로 "
                f"조정 중이다. '올해 많이 올랐다'와 '지금 빠지고 있다'는 동시에 참이다.")
            conds.append("코스피가 200일선(약 " + f"{_n(ks['ma200'], '{:,.0f}')}" +
                         ")을 지키는지 — 여기가 무너지면 조정이 추세 전환으로 바뀐다")
        elif dd is not None:
            comments.append(f"코스피는 고점 대비 {_fmt(dd)}, 연초 대비 {_fmt(ks['ytd'])}.")

    if vix:
        v = vix["last"]
        if v < VIX_CALM:
            comments.append(f"VIX {v:.1f}로 낮다. 시장이 위험을 거의 가격에 넣지 않고 있다 — "
                            f"방향 예측이 아니라 **헤지 비용이 싸다**는 뜻으로 읽는 편이 낫다.")
            actions.append({
                "what": "헤지를 붙일 생각이 있었다면 지금이 비용이 싼 구간",
                "why": f"VIX {v:.1f}는 1년 범위의 {_n(vix['range52w']['position_pct'])}% 지점. "
                       "변동성이 쌀 때 사두는 것이 비쌀 때 쫓아가는 것보다 유리하다.",
                "risk": "변동성이 계속 낮게 머무르면 헤지 비용은 그대로 소멸한다. "
                        "포트폴리오의 1% 안쪽으로 제한할 것.",
            })
        elif v > VIX_STRESS:
            score -= 1
            comments.append(f"VIX {v:.1f}로 스트레스 구간이다.")
        else:
            comments.append(f"VIX {v:.1f}로 보통 수준.")

    # 위험선호 교차신호
    sb = next((c for c in cross if c["key"] == "stock_bond"), None)
    if sb:
        score += 1 if sb["chg_3m_pct"] > 0 else -1
        comments.append(f"주식/장기국채 비율은 3개월 {_fmt(sb['chg_3m_pct'])} — {sb['reading']}.")

    overbought = [a["name"] for a in (spy, qqq, iwm, ks, eem) if a and "과열" in _tags(a)]
    if overbought:
        conds.append(f"과열 신호가 켜진 지수: {', '.join(overbought)}")

    actions.append({
        "what": "지수 노출은 유지하되 신규 자금은 나눠서 넣기",
        "why": "여러 지수가 52주 상단에 있다. 상단에서의 일시 매수는 진입 시점 위험이 크고, "
               "분할 매수는 그 위험만 줄이면서 추세 참여는 유지한다.",
        "risk": "상승이 이어지면 분할 매수는 일시 매수보다 평균 단가가 높아진다. "
                "그 대가로 사는 것이 시점 위험이다.",
    })
    if ks and (ks["range52w"].get("drawdown_pct") or 0) <= -15:
        actions.append({
            "what": "한국 주식 비중이 큰 경우, 고점 대비 낙폭을 기준으로 리밸런싱 판단",
            "why": "연초 대비 수익률만 보면 여전히 큰 이익이라 위험이 안 보인다. "
                   "고점 대비 낙폭이 실제 위험의 크기다.",
            "risk": "조정이 끝나가는 지점에서 줄이면 반등을 놓친다. "
                    "전량이 아니라 목표 비중으로 되돌리는 정도가 무난하다.",
        })

    return _pack("stocks", "주식", facts, comments, actions, conds, score)


# ================================================================ 금리·채권
def _rates(snaps, cross):
    t10, t5, t3m, tlt = (_get(snaps, s) for s in ("^TNX", "^FVX", "^IRX", "TLT"))
    facts, comments, actions, conds = [], [], [], []
    score = 0

    for a in (t10, t5, t3m):
        if not a:
            continue
        facts.append({
            "label": a["name"], "value": f"{a['last']:.3f}%",
            "detail": (f"1주 {_fmt(a['changes']['1w'], '%p')} · "
                       f"3개월 {_fmt(a['changes']['3m'], '%p')} · YTD {_fmt(a['ytd'], '%p')}"),
            "trend": a["trend"]["label"], "tags": _tags(a),
        })
    if tlt:
        facts.append({
            "label": tlt["name"], "value": f"{tlt['last']:,}",
            "detail": (f"3개월 {_fmt(tlt['changes']['3m'])} · YTD {_fmt(tlt['ytd'])} · "
                       f"고점대비 {_fmt(tlt['range52w'].get('drawdown_pct'))}"),
            "trend": tlt["trend"]["label"], "tags": _tags(tlt),
        })

    if t10:
        m3 = t10["changes"]["3m"]
        bp = None if m3 is None else m3 * 100
        comments.append(
            f"미 10년물은 {t10['last']:.2f}%. 3개월 동안 "
            f"{('약 %+.0fbp' % bp) if bp is not None else '—'} 움직였고, "
            f"52주 범위의 {_n(t10['range52w']['position_pct'])}% 지점이다.")
        if bp is not None and bp > RATE_MOVE_3M_BP:
            score -= 1        # 금리 상승 = 채권 가격 하락
            comments.append("금리가 오르는 방향이다. **채권 가격은 그 반대로 움직인다** — "
                            "장기채 보유자에게는 평가손 요인이다.")
            conds.append("장기금리가 지금 수준을 넘어 더 오르는지 — 넘으면 듀레이션이 긴 채권일수록 손실이 커진다")
        elif bp is not None and bp < -RATE_MOVE_3M_BP:
            score += 1
            comments.append("금리가 내리는 방향이다. 장기채에는 평가익 요인이다.")

    yc = next((c for c in cross if c["key"] == "yield_curve"), None)
    if yc:
        facts.append({
            "label": yc["label"], "value": f"{yc['value']:+.2f}%p",
            "detail": f"1개월 {yc['chg_1m_pct']:+.2f}%p · 3개월 {yc['chg_3m_pct']:+.2f}%p",
            "trend": yc["direction"], "tags": (["금리차 역전"] if yc.get("inverted") else []),
        })
        comments.append(f"장단기 금리차는 {yc['value']:+.2f}%p — {yc['reading']}.")
        if yc.get("inverted"):
            score -= 1
            conds.append("역전된 금리차가 다시 정상으로 되돌아오는 시점 — 과거 침체는 역전 자체보다 "
                         "'역전 해소' 국면에서 시작됐다")

    if tlt and tlt["trend"]["label"] == "하락 추세":
        actions.append({
            "what": "장기채(듀레이션 긴 자산) 비중을 늘릴 거면 서두르지 않기",
            "why": f"{tlt['name']}은 {tlt['trend']['reason']}. 금리가 오르는 국면에서 "
                   "장기채는 계속 평가손을 낸다.",
            "risk": "금리가 정점을 찍고 내려가기 시작하면 장기채가 가장 크게 오른다 — "
                    "그 전환점은 지나고 나서야 확인된다.",
        })
    actions.append({
        "what": "만기가 짧은 채권·예금으로 금리를 확정하는 선택지",
        "why": f"단기금리가 {t3m['last']:.2f}% 수준이면 현금성 자산도 실질적인 수익원이다. "
               "듀레이션 위험 없이 이자를 받는다." if t3m else
               "단기금리가 높으면 현금성 자산도 수익원이 된다.",
        "risk": "금리가 빠르게 내려가면 재예치 시점에 더 낮은 금리를 받는다(재투자 위험). "
                "장기 금리를 지금 확정하는 것과의 맞교환이다.",
    })
    return _pack("rates", "금리 · 채권", facts, comments, actions, conds, score)


# ================================================================ 달러·환율
def _fx(snaps, cross):
    dxy, krw, eur, jpy = (_get(snaps, s) for s in ("DX-Y.NYB", "KRW=X", "EURUSD=X", "JPY=X"))
    facts, comments, actions, conds = [], [], [], []
    score = 0

    for a in (dxy, krw, eur, jpy):
        if not a:
            continue
        facts.append({
            "label": a["name"], "value": f"{a['last']:,}",
            "detail": (f"1주 {_fmt(a['changes']['1w'])} · 3개월 {_fmt(a['changes']['3m'])} · "
                       f"YTD {_fmt(a['ytd'])}"),
            "trend": a["trend"]["label"], "tags": _tags(a),
        })

    if krw:
        m3 = krw["changes"]["3m"]
        pos = krw["range52w"]["position_pct"]
        # KRW=X 는 '1달러가 몇 원인가'다. 값이 내리면 원화 강세.
        if m3 is not None and m3 < 0:
            score += 1
            comments.append(
                f"원/달러는 {krw['last']:,.0f}원. 3개월 {_fmt(m3)}로 **원화가 강해졌다**. "
                f"1년 범위에서 {_n(pos)}% 지점 — 원화 기준으로는 달러가 싼 구간이다.")
            comments.append(
                "달러 자산을 이미 들고 있다면 원화 환산 수익이 그만큼 깎였다는 뜻이고, "
                "앞으로 달러 자산을 살 계획이라면 환전 비용이 유리해졌다는 뜻이다. "
                "**같은 사실이 보유자와 매수자에게 반대로 작용한다.**")
            conds.append("원화 강세가 이어지는지 — 이어지면 환헤지 없는 해외자산의 원화 수익률이 계속 눌린다")
        elif m3 is not None:
            score -= 1
            comments.append(
                f"원/달러는 {krw['last']:,.0f}원. 3개월 {_fmt(m3)}로 원화가 약해졌다 — "
                "환헤지를 하지 않은 해외자산은 환차익이 붙었다.")
        if "과매도" in _tags(krw):
            conds.append("원/달러가 과매도 구간이다(RSI "
                         f"{_n(krw['rsi14'])}) — 되돌림이 나오면 원화가 다시 약해지는 쪽")

    if dxy:
        comments.append(f"달러인덱스는 {dxy['last']:.2f}, {dxy['trend']['reason']}.")

    actions.append({
        "what": "해외자산 신규 매수는 환율 수준을 나눠서 접근",
        "why": "환율은 자산 가격과 별개의 두 번째 베팅이다. 한 시점에 몰아서 환전하면 "
               "종목 선택이 맞아도 환율에서 잃을 수 있다.",
        "risk": "원화가 계속 강해지면 나중에 환전할수록 유리했다는 결과가 된다. "
                "분할은 그 최선을 포기하는 대신 최악을 피한다.",
    })
    if krw and (krw["changes"]["3m"] or 0) < -5:
        actions.append({
            "what": "환헤지형·비헤지형 상품 중 무엇을 들고 있는지 확인",
            "why": f"원화가 3개월 {_fmt(krw['changes']['3m'])} 움직였다. 이 구간에서 "
                   "헤지 여부에 따라 같은 지수를 담은 상품의 수익률이 크게 갈린다.",
            "risk": "헤지에는 비용(금리차)이 든다. 원화가 다시 약해지면 헤지형이 불리해진다.",
        })
    return _pack("fx", "달러 · 환율", facts, comments, actions, conds, score)


# =================================================================== 에너지
def _energy(snaps, cross):
    wti, brent, ng, xle = (_get(snaps, s) for s in ("CL=F", "BZ=F", "NG=F", "XLE"))
    facts, comments, actions, conds = [], [], [], []
    score = 0

    for a in (wti, brent, ng, xle):
        if not a:
            continue
        facts.append({
            "label": a["name"], "value": f"{a['last']:,}",
            "detail": (f"1주 {_fmt(a['changes']['1w'])} · 3개월 {_fmt(a['changes']['3m'])} · "
                       f"YTD {_fmt(a['ytd'])}"),
            "trend": a["trend"]["label"], "tags": _tags(a),
        })

    if wti:
        w1, w3, ytd = wti["changes"]["1w"], wti["changes"]["3m"], wti["ytd"]
        comments.append(
            f"WTI는 배럴당 ${wti['last']:,.2f}. 연초 대비 {_fmt(ytd)}로 크게 올라 있지만 "
            f"최근 1주 {_fmt(w1)}, 3개월 {_fmt(w3)}로 단기는 눌리는 중이다."
            if ytd and ytd > 20 else
            f"WTI는 배럴당 ${wti['last']:,.2f}. 3개월 {_fmt(w3)} · 연초 대비 {_fmt(ytd)}.")
        if ytd and ytd > 30:
            score += 1
            comments.append(
                "연초 대비 상승폭이 크다는 것은 **물가 쪽에 계속 압력을 준다**는 뜻이다. "
                "유가는 헤드라인 물가에 가장 빠르게 반영되는 항목이고, 그것이 금리 기대를 통해 "
                "주식·채권으로 옮겨붙는다.")
            conds.append("유가가 현재 수준 위에서 굳어지는지 — 굳으면 물가 둔화 기대가 늦춰지고 "
                         "금리 인하 기대도 함께 밀린다")
        if w3 is not None and w3 < 0 and (ytd or 0) > 20:
            conds.append("연중 상승 추세와 최근 3개월 하락이 부딪히고 있다 — "
                         "어느 쪽이 이기는지가 에너지 관련 자산의 다음 방향")
    if xle and "과열" in _tags(xle):
        comments.append(f"미국 에너지 섹터는 과열 신호(RSI {_n(xle['rsi14'])})와 함께 "
                        f"52주 상단에 있다. 유가 자체보다 섹터가 더 앞서 갔다.")
        conds.append("에너지주가 유가 없이 혼자 오른 부분은 되돌림 위험이 크다")
    if ng and (ng["ytd"] or 0) < -15:
        comments.append(f"천연가스는 연초 대비 {_fmt(ng['ytd'])}로 유가와 정반대다. "
                        "에너지를 한 덩어리로 보면 안 되는 이유다.")

    actions.append({
        "what": "에너지 노출은 '물가 헤지'로 보고 크기를 정하기",
        "why": "에너지는 그 자체로 수익을 노리기보다, 물가가 다시 오를 때 나머지 포트폴리오가 "
               "받는 타격을 상쇄하는 역할이 크다. 그 목적이면 큰 비중이 필요 없다.",
        "risk": "유가가 빠지면 에너지주는 지수보다 크게 빠진다. 헤지 목적이라면 "
                "그 손실을 다른 자산의 이익으로 감당할 수 있는 크기여야 한다.",
    })
    if xle and "과열" in _tags(xle):
        actions.append({
            "what": "에너지 섹터에서 이익이 크게 난 부분은 일부 실현 고려",
            "why": f"{xle['name']}가 52주 상단·과열 구간이다. 이익 실현은 예측이 아니라 "
                   "비중이 커진 자산을 원래 비중으로 되돌리는 규율이다.",
            "risk": "지정학 사건이 터지면 에너지가 가장 크게 오른다 — 그 국면을 놓칠 수 있다.",
        })
    return _pack("energy", "에너지", facts, comments, actions, conds, score)


# ==================================================================== 금속
def _metals(snaps, cross):
    gold, silver, copper, plat = (_get(snaps, s) for s in ("GC=F", "SI=F", "HG=F", "PL=F"))
    facts, comments, actions, conds = [], [], [], []
    score = 0

    for a in (gold, silver, copper, plat):
        if not a:
            continue
        facts.append({
            "label": a["name"], "value": f"{a['last']:,}",
            "detail": (f"1주 {_fmt(a['changes']['1w'])} · 3개월 {_fmt(a['changes']['3m'])} · "
                       f"YTD {_fmt(a['ytd'])}"),
            "trend": a["trend"]["label"], "tags": _tags(a),
        })

    if gold:
        comments.append(
            f"금은 온스당 ${gold['last']:,.0f}, {gold['trend']['reason']}. "
            f"연초 대비 {_fmt(gold['ytd'])}.")
        if "과열" in _tags(gold):
            score += 1
            comments.append(f"RSI {_n(gold['rsi14'])}로 과열 구간이다. 금의 과열은 대개 "
                            "'불안이 값에 이미 들어갔다'는 신호라, 추격 매수의 위험이 커진 상태로 읽는다.")
    if copper:
        comments.append(
            f"구리는 파운드당 ${copper['last']:.3f}로 52주 범위의 "
            f"{_n(copper['range52w']['position_pct'])}% 지점이다. "
            "구리는 실물 경기를 가장 정직하게 반영하는 금속이라, 높은 구리 값은 "
            "경기가 아직 꺾이지 않았다는 쪽의 증거다.")
        if (copper["range52w"]["position_pct"] or 0) > 90:
            score += 1

    cg = next((c for c in cross if c["key"] == "copper_gold"), None)
    if cg:
        comments.append(f"구리/금 비율은 3개월 {_fmt(cg['chg_3m_pct'])}, 1개월 "
                        f"{_fmt(cg['chg_1m_pct'])} — {cg['reading']}.")
        if cg["chg_1m_pct"] is not None and cg["chg_3m_pct"] is not None \
                and cg["chg_1m_pct"] * cg["chg_3m_pct"] < 0:
            conds.append("구리/금 비율의 1개월과 3개월 방향이 엇갈린다 — "
                         "경기 기대가 흔들리는 구간이라는 뜻이고, 이 비율의 방향이 잡히는 쪽이 "
                         "장기금리·경기민감주와 같이 간다")

    sg = next((c for c in cross if c["key"] == "silver_gold"), None)
    if sg and silver:
        comments.append(f"은/금 비율 3개월 {_fmt(sg['chg_3m_pct'])} — {sg['reading']}. "
                        f"은은 절반이 산업 수요라 금보다 경기에 민감하고, 그만큼 변동성도 크다"
                        f"(은 20일 변동성 {_n(silver['vol20_pct'])}% vs "
                        f"금 {_n(gold['vol20_pct'])}%)." if gold and silver else "")

    actions.append({
        "what": "금은 '보험'으로 보고 비중을 미리 정해두기",
        "why": "금은 현금흐름이 없어 적정가를 계산할 수 없다. 그래서 '얼마가 싸다'가 아니라 "
               "'포트폴리오의 몇 %를 불확실성에 배정할 것인가'로 정하는 편이 일관적이다.",
        "risk": "실질금리가 오르면 금은 이자를 못 주는 약점이 부각되어 오래 눌린다.",
    })
    if gold and "과열" in _tags(gold):
        actions.append({
            "what": "금 신규 진입은 과열이 식은 뒤로 미루는 선택지",
            "why": f"RSI {_n(gold['rsi14'])}는 단기 과열 구간이다. 목표 비중에 이미 도달했다면 "
                   "추가 매수의 근거가 약하다.",
            "risk": "금의 강세는 불안이 커질 때 나오고, 그 국면은 기다려주지 않는다. "
                    "목표 비중에 미달이라면 기다림 자체가 위험이다.",
        })
    return _pack("metals", "금속", facts, comments, actions, conds, score)


# =================================================================== 크립토
def _crypto(snaps, cross):
    btc, eth, sol = (_get(snaps, s) for s in ("BTC-USD", "ETH-USD", "SOL-USD"))
    facts, comments, actions, conds = [], [], [], []
    score = 0

    for a in (btc, eth, sol):
        if not a:
            continue
        facts.append({
            "label": a["name"], "value": f"{a['last']:,}",
            "detail": (f"1주 {_fmt(a['changes']['1w'])} · 3개월 {_fmt(a['changes']['3m'])} · "
                       f"YTD {_fmt(a['ytd'])} · 20일 변동성 {_n(a['vol20_pct'])}%"),
            "trend": a["trend"]["label"], "tags": _tags(a),
        })

    if btc:
        comments.append(
            f"비트코인은 ${btc['last']:,.0f}. 3개월 {_fmt(btc['changes']['3m'])}로 크게 반등했지만 "
            f"연초 대비로는 {_fmt(btc['ytd'])}다. **어느 기간을 보느냐에 따라 이야기가 정반대**가 되는 "
            "대표적인 자산이다.")
        if "과열" in _tags(btc):
            score -= 1
            comments.append(f"RSI {_n(btc['rsi14'])}로 강한 과열 구간이다.")
            conds.append(f"과열이 조정으로 풀릴지 추세로 이어질지 — 크립토는 이 구간에서 "
                         f"20일 변동성이 {_n(btc['vol20_pct'])}%에 이르러 며칠 만에 두 자릿수 등락이 흔하다")
    if sol and (sol["changes"]["1w"] or 0) > 10:
        comments.append(f"솔라나가 1주 {_fmt(sol['changes']['1w'])}로 앞서가고 있다. "
                        "알트코인이 비트코인보다 빨리 오르는 국면은 위험 선호가 강해졌다는 신호이면서, "
                        "동시에 되돌림도 그만큼 크다.")
    eb = next((c for c in cross if c["key"] == "eth_btc"), None)
    if eb:
        comments.append(f"이더/비트 비율 3개월 {_fmt(eb['chg_3m_pct'])} — {eb['reading']}.")

    vol = btc["vol20_pct"] if btc else None
    actions.append({
        "what": "크립토는 '잃어도 계획이 안 바뀌는 금액'으로 상한을 먼저 정하기",
        "why": (f"20일 연환산 변동성이 비트코인 {_n(vol)}% 수준이다. 주식 지수의 서너 배라, "
                "같은 금액을 넣어도 포트폴리오에 주는 충격은 몇 배가 된다."
                if vol else "변동성이 주식 지수의 서너 배다."),
        "risk": "상한을 낮게 잡으면 강세장에서 기여가 작다. 그 대가로 사는 것이 "
                "'하락장에서 계획을 접지 않을 수 있음'이다.",
    })
    if btc and "과열" in _tags(btc):
        actions.append({
            "what": "과열 구간에서는 신규 진입보다 비중 되돌리기",
            "why": f"RSI {_n(btc['rsi14'])}에서 비중이 목표를 넘었다면, 초과분을 줄이는 것은 "
                   "예측이 아니라 규율이다.",
            "risk": "크립토의 상승은 짧은 기간에 몰리는 경향이 있어, 줄인 직후 큰 상승을 놓칠 수 있다.",
        })
    return _pack("crypto", "크립토", facts, comments, actions, conds, score)


# ==================================================================== 신용
def _credit(snaps, cross):
    hyg, lqd = _get(snaps, "HYG"), _get(snaps, "LQD")
    facts, comments, actions, conds = [], [], [], []
    score = 0
    for a in (hyg, lqd):
        if not a:
            continue
        facts.append({
            "label": a["name"], "value": f"{a['last']:,}",
            "detail": (f"1주 {_fmt(a['changes']['1w'])} · 3개월 {_fmt(a['changes']['3m'])} · "
                       f"YTD {_fmt(a['ytd'])}"),
            "trend": a["trend"]["label"], "tags": _tags(a),
        })
    hl = next((c for c in cross if c["key"] == "hyg_lqd"), None)
    if hl:
        score += 1 if hl["chg_3m_pct"] > 0 else -1
        comments.append(
            f"하이일드/투자등급 비율은 3개월 {_fmt(hl['chg_3m_pct'])} — {hl['reading']}. "
            "이 비율은 주식보다 먼저 꺾이는 경우가 많아 조기 경보로 본다.")
        conds.append("이 비율이 꺾이는지 — 주식이 아직 버티는데 여기가 먼저 내려가면 "
                     "위험자산 전반의 경고로 읽는다")
    if hyg and "52주 상단" in _tags(hyg):
        comments.append(f"하이일드 채권이 52주 상단"
                        f"({_n(hyg['range52w']['position_pct'])}% 지점)이다 — "
                        "신용 시장은 아직 불안을 가격에 넣지 않고 있다.")
    actions.append({
        "what": "신용 지표를 주식 비중 조절의 선행 신호로 쓰기",
        "why": "하이일드가 먼저 꺾이고 주식이 따라가는 순서가 반복적으로 관찰된다. "
               "주식만 보면 신호가 늦다.",
        "risk": "선행 신호는 거짓 경보도 낸다. 이것 하나로 큰 비중을 움직이면 "
                "잘못된 신호에 여러 번 당한다.",
    })
    return _pack("credit", "신용", facts, comments, actions, conds, score)


# ----------------------------------------------------------------- 조립
def _pack(key, label, facts, comments, actions, conds, score):
    return {
        "topic": key,
        "label": label,
        "facts": facts,
        "comment": " ".join(c for c in comments if c),
        "outlook": {
            "bias": _bias(score),
            "score": score,
            "watch": conds,
            "disclaimer": "방향은 예측이 아니라 조건이다 — 아래 항목이 확인되는 쪽으로 기운다.",
        },
        "actions": actions,
    }


BUILDERS = [
    ("stocks", _stocks), ("rates", _rates), ("fx", _fx),
    ("energy", _energy), ("metals", _metals), ("crypto", _crypto), ("credit", _credit),
]


def build(macro, news=None):
    """지표(+뉴스)로 주제별 브리핑을 만든다."""
    snaps = macro.get("assets", {})
    cross = macro.get("cross", [])
    news_by_topic = (news or {}).get("by_topic", {})

    briefs = []
    for key, fn in BUILDERS:
        try:
            b = fn(snaps, cross)
        except Exception as e:                       # 한 주제가 죽어도 나머지는 낸다
            log.warning("%s 브리핑 실패: %s", key, e)
            continue
        b["news"] = news_by_topic.get(key, [])[:4]
        briefs.append(b)

    return {
        "asof": macro.get("asof"),
        "generated_by": "규칙 기반 (LLM 미사용)",
        "briefs": briefs,
        "summary": _summary(briefs, macro),
    }


def _summary(briefs, macro):
    """전체 한 문단 — 어느 쪽으로 기운 주제가 많은지와 눈에 띄는 것."""
    up = [b["label"] for b in briefs if b["outlook"]["score"] >= 2]
    down = [b["label"] for b in briefs if b["outlook"]["score"] <= -2]
    hot, cold = [], []
    for a in macro.get("assets", {}).values():
        tags = [t["tag"] for t in a.get("state", [])]
        if "과열" in tags:
            hot.append(a["name"])
        if "약세장" in tags:
            cold.append(a["name"])

    def _join(names, limit=4):
        """길게 나열하면 요약이 아니라 목록이 된다 — 앞의 몇 개만 이름을 쓴다."""
        if len(names) <= limit:
            return ", ".join(names)
        return ", ".join(names[:limit]) + f" 외 {len(names) - limit}개"

    parts = []
    if up:
        parts.append(f"상방으로 기운 주제: {_join(up)}")
    if down:
        parts.append(f"하방으로 기운 주제: {_join(down)}")
    if hot:
        parts.append(f"과열 신호: {_join(hot)}")
    if cold:
        parts.append(f"고점 대비 20% 이상 하락: {_join(cold)}")
    if not parts:
        parts.append("어느 쪽으로도 뚜렷하게 기울지 않은 국면이다")
    return " · ".join(parts) + "."
