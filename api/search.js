// 종목 검색 중계 (Vercel 서버리스 함수).
//
// 왜 중계가 필요한가 — 브라우저에서 직접 부를 수 없다 (실측):
//   · 네이버 자동완성: Origin 헤더가 붙으면 403
//   · Yahoo Finance:  CORS 헤더 없음 + 429
// 그래서 서버가 대신 부르고 결과만 넘긴다.
//
// 파이썬 쪽 `core/symbol_search.py` 와 **같은 규칙**을 쓴다:
//   · 한글이 섞이면 네이버 먼저 (Yahoo 는 한글 쿼리를 400 으로 거부한다)
//   · 심볼 표기는 yfinance 기준으로 통일 (005930 → 005930.KS)
//   · 둘 다 실패하면 빈 목록이 아니라 오류 — 빈 목록은 "그런 종목 없음"으로 읽힌다
// 규칙을 고칠 때는 두 파일을 같이 고칠 것.

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36';

const NAVER_SUFFIX = {
  KOSPI: '.KS', KOSDAQ: '.KQ', KONEX: '.KQ',
  TOKYO: '.T', HONGKONG: '.HK', LONDON: '.L', SHANGHAI: '.SS', SHENZHEN: '.SZ',
};
const NAVER_NO_SUFFIX = new Set(['NASDAQ', 'NYSE', 'AMEX', 'NYSEARCA', 'BATS']);
const ALLOWED_TYPES = new Set(['EQUITY', 'ETF', 'MUTUALFUND', 'INDEX', 'CURRENCY', 'CRYPTOCURRENCY']);
const EXCHANGE_LABEL = {
  KSC: '코스피', KOE: '코스닥', KDQ: '코스닥', NMS: '나스닥', NGM: '나스닥',
  NYQ: '뉴욕', PCX: 'NYSE Arca', TOR: '토론토', LSE: '런던', HKG: '홍콩', TYO: '도쿄',
};

const hasHangul = (s) => /[가-힣]/.test(s);

function inferCurrency(symbol) {
  const s = String(symbol || '').toUpperCase();
  if (s.endsWith('.KS') || s.endsWith('.KQ')) return 'KRW';
  if (s.endsWith('.T')) return 'JPY';
  if (s.endsWith('.HK')) return 'HKD';
  if (s.endsWith('.L')) return 'GBP';
  return 'USD';
}

function naverSymbol(code, typeCode, nationCode) {
  const c = String(code || '').trim().toUpperCase();
  const tc = String(typeCode || '').trim().toUpperCase();
  if (!c) return null;
  if (String(nationCode || '').toUpperCase() === 'KOR') return c + (NAVER_SUFFIX[tc] || '.KS');
  if (NAVER_NO_SUFFIX.has(tc)) return c;
  return c + (NAVER_SUFFIX[tc] || '');
}

async function fetchJson(url) {
  const r = await fetch(url, { headers: { 'User-Agent': UA, Accept: 'application/json' } });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

async function searchNaver(q, limit) {
  const url = 'https://ac.stock.naver.com/ac?q=' + encodeURIComponent(q) +
    '&target=stock,index,marketindicator';
  const data = await fetchJson(url);
  const out = [];
  for (const item of data.items || []) {
    const rows = Array.isArray(item.items) ? item.items : [item];
    for (const row of rows) {
      const sym = naverSymbol(row.code, row.typeCode, row.nationCode);
      if (!sym) continue;
      out.push({
        symbol: sym,
        name: row.name || sym,
        exchange: row.typeName || row.typeCode || '',
        type: 'EQUITY',
        currency: inferCurrency(sym),
        source: 'naver',
      });
      if (out.length >= limit * 2) return out;
    }
  }
  return out;
}

async function searchYahoo(q, limit) {
  const url = 'https://query2.finance.yahoo.com/v1/finance/search?q=' +
    encodeURIComponent(q) + '&quotesCount=' + Math.max(limit * 2, 10) +
    '&newsCount=0&enableFuzzyQuery=false&quotesQueryId=tss_match_phrase_query';
  const data = await fetchJson(url);
  const out = [];
  for (const item of data.quotes || []) {
    const qt = String(item.quoteType || '').toUpperCase();
    if (!item.symbol || !ALLOWED_TYPES.has(qt)) continue;
    out.push({
      symbol: item.symbol,
      name: item.shortname || item.longname || item.symbol,
      exchange: EXCHANGE_LABEL[item.exchange] || item.exchDisp || item.exchange || '',
      type: qt,
      currency: (item.currency || inferCurrency(item.symbol)).toUpperCase(),
      source: 'yahoo',
    });
  }
  return out;
}

export default async function handler(req, res) {
  const q = String((req.query && req.query.q) || '').trim();
  const limit = Math.min(parseInt((req.query && req.query.limit) || '10', 10) || 10, 25);

  if (!q) {
    res.status(400).json({ error: '검색어가 비어 있습니다' });
    return;
  }

  const order = hasHangul(q) ? [searchNaver, searchYahoo] : [searchYahoo, searchNaver];
  const results = [];
  const seen = new Set();
  const failures = [];

  for (const fn of order) {
    if (results.length >= limit) break;
    try {
      for (const row of await fn(q, limit)) {
        if (seen.has(row.symbol)) continue;
        seen.add(row.symbol);
        results.push(row);
        if (results.length >= limit) break;
      }
    } catch (e) {
      failures.push(fn.name + ': ' + e.message);
    }
  }

  if (!results.length && failures.length) {
    // 빈 목록으로 돌려주면 "그런 종목 없음"으로 읽힌다. 실패는 실패로 알린다.
    res.status(502).json({ error: '종목 검색에 실패했습니다 (' + failures.join(', ') + ')' });
    return;
  }

  res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
  res.status(200).json({ query: q, results });
}
