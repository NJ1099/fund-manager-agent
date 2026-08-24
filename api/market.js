// 시세 · 과거 종가 · 환율 중계 (Vercel 서버리스 함수).
//
//   GET /api/market?symbols=AAPL,005930.KS            → 현재가만
//   GET /api/market?symbols=...&history=1&range=1y    → 과거 종가까지 (상관·변동성 계산용)
//
// 브라우저에서 Yahoo 를 직접 부르면 CORS 로 막히고 429 가 난다. 그래서 중계한다.
//
// ■ 없는 값을 0 으로 채우지 않는다
// 시세를 못 받은 종목은 응답에 **키 자체를 넣지 않고** `missing` 목록으로 알린다.
// 0 으로 채우면 평가액이 조용히 틀어지고, 화면에는 "자산이 줄었다"로 보인다.
// 파이썬 쪽 `core/symbol_search.py::quote` 와 같은 원칙이다.

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36';

const MAX_SYMBOLS = 40;

async function chart(symbol, range, interval) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
    `?range=${range}&interval=${interval}`;
  const r = await fetch(url, { headers: { 'User-Agent': UA, Accept: 'application/json' } });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const data = await r.json();
  const result = data && data.chart && data.chart.result && data.chart.result[0];
  if (!result) throw new Error('빈 응답');

  const meta = result.meta || {};
  const quote = (result.indicators && result.indicators.quote && result.indicators.quote[0]) || {};
  const adj = result.indicators && result.indicators.adjclose && result.indicators.adjclose[0];

  // 배당·분할이 반영된 수정종가를 쓴다 (없으면 종가)
  const raw = (adj && adj.adjclose) || quote.close || [];
  const stamps = result.timestamp || [];

  const closes = [];
  const dates = [];
  for (let i = 0; i < raw.length; i++) {
    const v = raw[i];
    if (v === null || v === undefined || Number.isNaN(v)) continue;
    closes.push(v);
    dates.push(new Date(stamps[i] * 1000).toISOString().slice(0, 10));
  }
  if (!closes.length) throw new Error('가격 없음');

  return {
    price: meta.regularMarketPrice != null ? meta.regularMarketPrice : closes[closes.length - 1],
    currency: (meta.currency || '').toUpperCase() || null,
    name: meta.shortName || meta.longName || null,
    closes,
    dates,
  };
}

export default async function handler(req, res) {
  const q = req.query || {};
  const symbols = String(q.symbols || '')
    .split(',').map((s) => s.trim()).filter(Boolean).slice(0, MAX_SYMBOLS);
  const wantHistory = String(q.history || '') === '1';
  const range = ['1mo', '3mo', '6mo', '1y', '2y', '5y'].includes(String(q.range)) ? q.range : '1y';

  if (!symbols.length) {
    res.status(400).json({ error: 'symbols 파라미터가 필요합니다' });
    return;
  }

  const prices = {};
  const currencies = {};
  const names = {};
  const history = {};
  const missing = [];

  const settled = await Promise.allSettled(
    symbols.map((s) => chart(s, wantHistory ? range : '5d', '1d')));

  settled.forEach((r, i) => {
    const sym = symbols[i];
    if (r.status !== 'fulfilled') {
      missing.push(sym);
      return;
    }
    prices[sym] = r.value.price;
    if (r.value.currency) currencies[sym] = r.value.currency;
    if (r.value.name) names[sym] = r.value.name;
    if (wantHistory) history[sym] = { dates: r.value.dates, closes: r.value.closes };
  });

  const payload = { prices, currencies, names, missing };
  if (wantHistory) payload.history = history;

  // 시세는 짧게, 과거 데이터는 조금 길게 캐시한다 (같은 화면을 새로고침해도 재호출이 적게)
  res.setHeader('Cache-Control', wantHistory
    ? 's-maxage=1800, stale-while-revalidate=3600'
    : 's-maxage=60, stale-while-revalidate=300');
  res.status(200).json(payload);
}
