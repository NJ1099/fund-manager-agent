"""
뉴스 데스크 — 경제·시사 헤드라인 수집과 주제 분류.

RSS 만 읽는다. 표준 라이브러리(`urllib` + `xml.etree`)로 충분해서 의존성을
늘리지 않았고, **LLM 을 부르지 않는다** — 이 프로젝트의 비용 규율(대시보드
새로고침은 언제나 0원)을 지키기 위해서다. 분류는 키워드 규칙이다.

■ 왜 요약이 아니라 분류인가

헤드라인을 LLM 으로 요약하면 화면은 예뻐지지만 새로고침마다 돈이 나간다.
그리고 요약은 **원문에 없는 확신**을 만들어내기 쉽다. 여기서는 제목·요약문을
그대로 보여주고, 어느 주제(원유·금리·달러…)에 걸리는지만 표시한다.
판단은 지표 쪽(`macro_brief`)이 하고, 뉴스는 그 판단의 배경으로 둔다.

■ 실패를 조용히 넘기지 않는다

피드 하나가 죽는 것은 흔한 일이라 전체를 실패시키지 않지만, **어떤 피드가
왜 실패했는지 결과에 담아 돌려준다.** 조용히 빼면 "오늘은 뉴스가 없네"와
"수집이 깨졌네"를 구분할 수 없다.
"""
import html
import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

log = logging.getLogger("news")

UA = "Mozilla/5.0 (compatible; fund-manager-agent/1.0; +https://github.com/NJ1099/fund-manager-agent)"
TIMEOUT = 12

# 실측으로 살아 있는 것만 남겼다 (2026-08-28 확인).
# CNBC 는 `search.cnbc.com/rs/search/...` 형식이 빈 응답을 준다 — 아래 형식을 쓸 것.
# Reuters 공개 RSS 는 폐지됐다(DNS 조차 안 잡힌다). 되살리려 하지 말 것.
FEEDS = [
    # (이름, URL, 언어, 기본 주제 힌트)
    ("CNBC 경제",     "https://www.cnbc.com/id/20910258/device/rss/rss.html", "en", None),
    ("CNBC 시장",     "https://www.cnbc.com/id/10000664/device/rss/rss.html", "en", None),
    ("MarketWatch",   "http://feeds.marketwatch.com/marketwatch/topstories/", "en", None),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex",              "en", None),
    ("Investing 원자재", "https://www.investing.com/rss/news_11.rss",         "en", "commodities"),
    ("Investing 외환",  "https://www.investing.com/rss/news_1.rss",           "en", "fx"),
    ("OilPrice",      "https://oilprice.com/rss/main",                        "en", "energy"),
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/",      "en", "crypto"),
    ("연합뉴스 경제",  "https://www.yna.co.kr/rss/economy.xml",                "ko", None),
    ("한국경제",       "https://www.hankyung.com/feed/economy",                "ko", None),
    ("한국경제 금융",  "https://www.hankyung.com/feed/finance",                "ko", None),
]

# 주제 키워드. 한국어·영어를 같은 주제에 매단다.
#
# ■ 왜 단순 부분 문자열이 아니라 점수인가 (실측으로 고친 것)
# 처음엔 "키워드가 하나라도 걸리면 그 주제"로 했는데, 국내 경제면 RSS 가 지역·기업
# 단신 범벅이라 "고흥군, 몽골·중국서 500만달러 농수산물 수출 협약"이 달러·정책 주제로
# 올라왔다("달러"·"중국"에 걸려서). 매크로 화면에 지역 단신이 섞이면 화면 전체의
# 신뢰가 깎인다. 그래서 ①금액 표현을 먼저 지우고 ②제목 매치에 가중치를 주고
# ③최소 점수를 넘겨야 주제로 인정한다.
TOPIC_KEYWORDS = {
    "energy": ["oil price", "crude", "wti", "brent", "opec", "gasoline", "natural gas",
               "lng", "refinery", "petroleum", "barrel", "oil market", "oil demand",
               "유가", "원유", "석유", "정유", "천연가스", "에너지 가격", "오펙", "배럴", "유류"],
    "metals": ["gold price", "gold", "silver", "copper", "platinum", "palladium",
               "bullion", "precious metal", "base metal",
               "금값", "금 가격", "국제 금", "은값", "구리", "귀금속", "백금", "비철금속"],
    "fx":     ["dollar index", "the dollar", "us dollar", "currency market", "forex",
               "exchange rate", "greenback", "yen weak", "euro rose", "euro fell",
               "currencies", "fx market", "devaluation",
               "환율", "원/달러", "달러화", "달러 강세", "달러 약세", "원화", "엔화",
               "위안화", "외환시장", "외환당국"],
    "rates":  ["fed", "federal reserve", "interest rate", "bond yield", "treasury yield",
               "treasuries", "central bank", "ecb", "boj", "rate cut", "rate hike",
               "monetary policy", "fomc", "jackson hole", "yield curve",
               "금리", "국채", "채권", "연준", "기준금리", "한국은행", "통화정책",
               "금리 인상", "금리 인하", "국채 수익률"],
    "crypto": ["bitcoin", "ethereum", "crypto", "blockchain", "stablecoin",
               "digital asset", "btc", "eth", "defi", "altcoin",
               "비트코인", "이더리움", "가상자산", "암호화폐", "코인 시장", "블록체인",
               "스테이블코인"],
    "stocks": ["stock market", "equities", "s&p 500", "nasdaq", "dow jones", "wall street",
               "shares fell", "shares rose", "earnings", "rally", "selloff", "benchmark index",
               "증시", "코스피", "코스닥", "주식시장", "뉴욕증시", "주가지수", "상장지수"],
    "econ":   ["inflation", "cpi", "gdp", "jobs report", "unemployment", "payroll",
               "recession", "retail sales", "pmi", "manufacturing", "housing market",
               "consumer confidence", "economic growth",
               # '고용'·'경기' 단독은 지역 단신에 너무 많이 걸린다 — 지표 이름으로 좁힌다
               "물가", "인플레", "고용지표", "고용보고서", "실업률", "경기침체",
               "소비자물가", "생산자물가", "수출입", "무역수지", "경제성장", "산업생산",
               "소비심리", "경기지표"],
    "policy": ["tariff", "trade war", "sanction", "geopolit", "conflict", "regulation",
               "trade deal", "export control", "stimulus",
               "관세", "무역전쟁", "제재", "지정학", "수출규제", "무역협상", "부양책"],
}

# 제목 매치 가중 2, 요약 매치 1. 이 점수를 넘겨야 주제로 인정한다.
MIN_TOPIC_SCORE = 2

# 금액 표현 — "500만달러 수출"의 '달러'가 외환 뉴스로 잡히는 것을 막는다.
_MONEY = re.compile(r"\d[\d,.]*\s*(?:조|억|천만|백만|만|천)?\s*(?:달러|원|엔|위안|dollars?|won)")

# 매크로 화면에 올릴 값어치가 없는 단신. 제목 기준으로 거른다.
NOISE = [
    "[특징주]", "[표]", "[게시판]", "[인사]", "[부고]", "[신간]", "[사고]", "[동정]",
    "유상증자", "무상증자", "자사주", "공시", "기업공개 청약", "채용", "위촉", "임명",
    "업무협약", "mou 체결", "개점", "오픈", "출시", "수상", "선정", "축제", "간담회",
    "교육청", "시청", "군청", "도청", "지자체", "봉사", "기부",
]
# 노이즈 패턴에 걸려도 이 태그가 붙어 있으면 살린다 — 매크로 표제 기사들이다.
NOISE_EXEMPT = ["[외환]", "[유가]", "[금리]", "[증시]", "[뉴욕증시]", "[국제유가]", "[코스피]"]

# 국내 경제면 RSS 는 지역 단신 비중이 크다("강원 고유가 피해지원금", "영덕군 관광호텔").
# 지명을 일반 규칙으로 잡으려 하면 '뉴욕증시'의 '시'까지 걸려서, 명시적 목록으로 둔다.
# 완벽하지는 않다 — 잡히지 않는 지역 단신이 남는 것은 감수한다. 여기서 더 정확해지려면
# 형태소 분석기가 필요한데, 헤드라인 몇 줄 때문에 의존성을 늘릴 값어치는 없다.
REGIONS = ["강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주", "세종",
           "교육청", "시청", "군청", "도청", "지자체", "지역경제", "소상공인"]

# 뉴스 주제 → 지표 주제 매핑 (화면에서 나란히 보여주기 위한 것)
NEWS_TO_MACRO = {
    "energy": "energy", "metals": "metals", "fx": "fx", "rates": "rates",
    "crypto": "crypto", "stocks": "stocks", "econ": None, "policy": None,
}

TOPIC_LABEL = {
    "energy": "에너지", "metals": "금속", "fx": "달러 · 환율", "rates": "금리 · 채권",
    "crypto": "크립토", "stocks": "주식", "econ": "경제 지표", "policy": "정책 · 지정학",
    "commodities": "원자재",
}

_TAG = re.compile(r"<[^>]+>")


def _clean(text, limit=400):
    """HTML 조각을 걷어낸 평문."""
    if not text:
        return ""
    t = html.unescape(_TAG.sub(" ", text))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def _parse_date(raw):
    """RSS 의 날짜 표기가 제각각이라 알려진 형식을 순서대로 시도한다."""
    if not raw:
        return None
    raw = raw.strip()
    fmts = ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
            "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"]
    for f in fmts:
        try:
            dt = datetime.strptime(raw, f)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def is_noise(title):
    """지역·기업 단신인가. 매크로 표제 태그가 붙어 있으면 살린다."""
    low = title.lower()
    if any(x in low for x in NOISE_EXEMPT):
        return False
    return any(x in low for x in NOISE) or any(x in title for x in REGIONS)


def classify(title, summary="", hint=None):
    """주제별 점수를 매기고 문턱을 넘은 것만 돌려준다.

    제목 매치는 요약 매치보다 두 배로 센다 — 제목에 '유가'가 있으면 그 기사는
    유가 기사지만, 본문 어딘가에 한 번 나오는 것은 배경 언급일 때가 많다.
    하나도 문턱을 못 넘으면 피드가 준 힌트를 쓰고, 그것도 없으면 빈 목록이다
    (빈 목록 = 매크로 화면에 올리지 않는다).
    """
    t = _MONEY.sub(" ", title.lower())
    s = _MONEY.sub(" ", (summary or "").lower())

    scored = {}
    for topic, kws in TOPIC_KEYWORDS.items():
        score = sum(2 for k in kws if k in t) + sum(1 for k in kws if k in s)
        if score >= MIN_TOPIC_SCORE:
            scored[topic] = score
    if scored:
        return [k for k, _ in sorted(scored.items(), key=lambda kv: -kv[1])]
    if hint and hint in TOPIC_KEYWORDS:
        return [hint]
    return []


def fetch_feed(name, url, lang="en", hint=None, limit=20):
    """RSS 하나를 읽어 항목 목록으로. 실패하면 예외를 올린다(상위에서 기록)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    data = urllib.request.urlopen(req, timeout=TIMEOUT).read()
    root = ET.fromstring(data)

    items = root.findall(".//item")
    atom = "{http://www.w3.org/2005/Atom}"
    if not items:
        items = root.findall(f".//{atom}entry")

    # 필터를 통과한 것으로 limit 을 센다. items[:limit] 를 먼저 자르면 앞쪽이
    # 전부 지역 단신인 국내 피드에서 결과가 통째로 비어버린다.
    out = []
    for it in items:
        if len(out) >= limit:
            break
        title = _clean(it.findtext("title") or it.findtext(f"{atom}title"), 300)
        if not title:
            continue
        if is_noise(title):
            continue
        summary = _clean(it.findtext("description") or it.findtext(f"{atom}summary") or "", 400)
        topics = classify(title, summary, hint)
        if not topics:
            continue                                # 매크로와 무관한 기사
        link = it.findtext("link") or ""
        if not link:
            le = it.find(f"{atom}link")
            link = le.get("href", "") if le is not None else ""
        pub = _parse_date(it.findtext("pubDate") or it.findtext("published")
                          or it.findtext(f"{atom}updated"))
        out.append({
            "title": title,
            "summary": summary,
            "link": link.strip(),
            "source": name,
            "lang": lang,
            "published": pub.isoformat() if pub else None,
            "ts": pub.timestamp() if pub else None,
            "topics": topics,
        })
    return out


def collect(feeds=None, per_feed=20, max_age_hours=72, per_topic=6):
    """전 피드를 훑어 주제별로 정리한다.

    실패한 피드는 결과의 `failed` 에 이유와 함께 남긴다 — "뉴스가 없다"와
    "수집이 깨졌다"를 화면에서 구분할 수 있어야 한다.
    """
    feeds = feeds or FEEDS
    articles, failed = [], []

    for name, url, lang, hint in feeds:
        try:
            got = fetch_feed(name, url, lang, hint, limit=per_feed)
            articles.extend(got)
            log.info("%s: %d건", name, len(got))
        except (urllib.error.URLError, ET.ParseError, OSError, ValueError) as e:
            failed.append({"source": name, "error": f"{type(e).__name__}: {e}"})
            log.warning("%s 수집 실패: %s", name, e)

    # 중복 제거 — 같은 기사가 여러 피드에 뜬다. 제목 앞부분을 키로 쓴다.
    seen, uniq = set(), []
    for a in articles:
        key = re.sub(r"[^a-z0-9가-힣]", "", a["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)

    # 오래된 기사 제외. 날짜가 없는 항목은 버리지 않는다 (피드가 날짜를 안 주는 경우가 있다).
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).timestamp()
    fresh = [a for a in uniq if a["ts"] is None or a["ts"] >= cutoff]
    stale = len(uniq) - len(fresh)

    fresh.sort(key=lambda a: a["ts"] or 0, reverse=True)

    by_topic = {}
    for a in fresh:
        for t in a["topics"]:
            by_topic.setdefault(t, []).append(a)
    by_topic = {t: v[:per_topic] for t, v in by_topic.items()}

    return {
        "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(fresh),
        "sources_ok": len(feeds) - len(failed),
        "sources_total": len(feeds),
        "failed": failed,
        "dropped_stale": stale,
        "max_age_hours": max_age_hours,
        "by_topic": by_topic,
        "labels": TOPIC_LABEL,
        "latest": fresh[:20],
    }
