// docs/script.js
// Script to load and render stock bot data from a JSON file.
// ---------- CONFIG ----------
const DATA_PATH = './data/stockinfo.json';
const CACHE_BUST = () => `?v=${Date.now()}`;
// Free CORS proxy to call Yahoo endpoints from a static page
const PROXY = 'https://api.allorigins.win/raw?url=';
const yahooChartURL = (ticker) => `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?interval=1m&range=1d`;

// ---------- ELEMENTS ----------
const el = {
  year: document.getElementById('year'),
  title: document.getElementById('title'),
  lastUpdate: document.getElementById('lastUpdate'),
  status: document.getElementById('status'),
  raw: document.getElementById('raw'),
  showRawBtn: document.getElementById('showRawBtn'),
  retryBtn: document.getElementById('retryBtn'),
  chart: document.getElementById('chart'),
  chartWrap: document.getElementById('chartWrap'),
  errorBox: document.getElementById('errorBox'),
  errorMsg: document.getElementById('errorMsg'),
  localNotice: document.getElementById('localNotice'),
  pickBtn: document.getElementById('pickBtn'),
  fileInput: document.getElementById('fileInput'),
  // tables
  equityBody: document.getElementById('equityBody'),
  posBody: document.getElementById('posBody'),
  // KPIs
  portfolioValue: document.getElementById('portfolioValue'),
  portfolioPct: document.getElementById('portfolioPct'),
  oneTicker: document.getElementById('oneTicker'),
  oneTickerSym: document.getElementById('oneTickerSym'),
  oneTickerPrice: document.getElementById('oneTickerPrice'),
  oneTickerPct: document.getElementById('oneTickerPct'),
};
el.year.textContent = new Date().getFullYear();

// ---------- HELPERS ----------
function setStatus(text, cls = '') {
  el.status.className = `status ${cls}`;
  el.status.textContent = text;
}
function showError(err) {
  console.error(err);
  el.errorMsg.textContent = (err && err.message) ? err.message : String(err);
  el.errorBox.classList.remove('hidden');
  setStatus('Error loading data', 'err');
}
function hideError() {
  el.errorBox.classList.add('hidden');
  el.errorMsg.textContent = '';
}
function fmtNumber(n, d = 2) {
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}
function signFmt(n, d = 2) {
  const s = Number(n);
  const str = fmtNumber(Math.abs(s), d);
  return (s >= 0 ? '+' : '−') + str;
}
function pctFmt(n) {
  const s = Number(n);
  const str = fmtNumber(Math.abs(s), 2);
  return (s >= 0 ? '+' : '−') + str + '%';
}
function fmtDate(iso) {
  const d = (iso && iso.length <= 10) ? new Date(iso + 'T00:00:00') : new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleDateString();
}
async function fetchWithTimeout(url, ms = 7000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctl.signal, cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally {
    clearTimeout(t);
  }
}
async function getLivePrice(ticker) {
  // Pull last non-null close from 1m range (acts like current price)
  const url = PROXY + encodeURIComponent(yahooChartURL(ticker) + `&v=${Date.now()}`);
  const data = await fetchWithTimeout(url, 8000);
  const res = data?.chart?.result?.[0];
  const closes = res?.indicators?.quote?.[0]?.close || [];
  for (let i = closes.length - 1; i >= 0; i--) {
    if (closes[i] != null) return Number(closes[i]);
  }
  throw new Error(`No price for ${ticker}`);
}

// ---------- RENDER: CHART ----------
function drawChart(series) {
  const svg = el.chart;
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  if (!Array.isArray(series) || series.length < 2) { el.chartWrap.style.display = 'none'; return; }
  el.chartWrap.style.display = '';
  const W = 800, H = 220, PAD = 18;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const ys = series.map(p => Number(p.equity)).filter(v => !Number.isNaN(v));
  const xMax = series.length - 1;
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const xScale = i => PAD + i * (W - 2 * PAD) / (xMax || 1);
  const yScale = v => H - PAD - (v - yMin) * (H - 2 * PAD) / ((yMax - yMin) || 1);

  const last = ys[ys.length - 1];
  const grid = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  grid.setAttribute('x1', PAD); grid.setAttribute('x2', W - PAD);
  grid.setAttribute('y1', yScale(last)); grid.setAttribute('y2', yScale(last));
  grid.setAttribute('stroke', '#2a355f'); grid.setAttribute('stroke-dasharray', '4 4');
  svg.appendChild(grid);

  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  let d = '';
  series.forEach((p, i) => { const x = xScale(i), y = yScale(Number(p.equity)); d += (i ? ` L ${x} ${y}` : `M ${x} ${y}`); });
  path.setAttribute('d', d); path.setAttribute('fill', 'none'); path.setAttribute('stroke', '#6ae2a0'); path.setAttribute('stroke-width', '2.5');
  svg.appendChild(path);

  const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  dot.setAttribute('cx', xScale(xMax)); dot.setAttribute('cy', yScale(last)); dot.setAttribute('r', 4.5); dot.setAttribute('fill', '#6ae2a0');
  svg.appendChild(dot);

  const lbls = [{ x: PAD, y: H - 4, t: fmtNumber(yMin) }, { x: W - PAD, y: H - 4, t: fmtNumber(yMax) }];
  lbls.forEach(l => {
    const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', l.x); t.setAttribute('y', l.y); t.setAttribute('fill', '#9aa3b2'); t.setAttribute('font-size', '11');
    t.setAttribute('text-anchor', l.x < W / 2 ? 'start' : 'end'); t.textContent = l.t; svg.appendChild(t);
  });
}

// ---------- RENDER: TABLES ----------
function renderEquityTable(series) {
  const tb = el.equityBody; tb.innerHTML = '';
  if (!Array.isArray(series) || series.length === 0) {
    tb.innerHTML = '<tr><td colspan="2">No data points.</td></tr>'; return;
  }
  series.slice(-30).forEach(p => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${fmtDate(p.date)}</td><td>${fmtNumber(p.equity)}</td>`;
    tb.appendChild(tr);
  });
}

function renderPositions(positions) {
  const tb = el.posBody; tb.innerHTML = '';
  if (!Array.isArray(positions) || positions.length === 0) {
    tb.innerHTML = '<tr><td colspan="7">No positions.</td></tr>'; return;
  }
  positions.forEach(pos => {
    const { ticker, qty, avg_price, current_price, market_value, unrealized_pl } = pos;
    const plPct = avg_price ? ((current_price - avg_price) / avg_price) * 100 : 0;
    const up = plPct >= 0;
    const tr = document.createElement('tr');
    tr.innerHTML = `
          <td>${ticker}</td>
          <td>${fmtNumber(qty, 4)}</td>
          <td>${fmtNumber(avg_price)}</td>
          <td class="${up ? 'up' : 'down'}">${fmtNumber(current_price)}</td>
          <td>${fmtNumber(market_value)}</td>
          <td class="${unrealized_pl >= 0 ? 'up' : 'down'}">${signFmt(unrealized_pl)}</td>
          <td class="${up ? 'up' : 'down'}">${pctFmt(plPct)}</td>
        `;
    tb.appendChild(tr);
  });
}

// ---------- RENDER: KPIs ----------
function renderKpis(payload, enrichedPositions) {
  const cost = Number(payload.invested_cost_basis || 0);
  const portfolioValue = enrichedPositions.reduce((s, p) => s + Number(p.market_value || 0), 0);
  const vsBasisPct = cost ? ((portfolioValue - cost) / cost) * 100 : 0;
  const up = vsBasisPct >= 0;

  el.portfolioValue.textContent = '$' + fmtNumber(portfolioValue);
  el.portfolioPct.textContent = pctFmt(vsBasisPct);
  el.portfolioPct.className = up ? 'up' : 'down';

  if (enrichedPositions.length === 1) {
    const p = enrichedPositions[0];
    const plPct = p.avg_price ? ((p.current_price - p.avg_price) / p.avg_price) * 100 : 0;
    const up1 = plPct >= 0;
    el.oneTickerSym.textContent = p.ticker;
    el.oneTickerPrice.textContent = '$' + fmtNumber(p.current_price);
    el.oneTickerPrice.className = 'value ' + (up1 ? 'up' : 'down');
    el.oneTickerPct.textContent = pctFmt(plPct);
    el.oneTickerPct.className = up1 ? 'up' : 'down';
    el.oneTicker.classList.remove('hidden');
  } else {
    el.oneTicker.classList.add('hidden');
  }
}

// ---------- VALIDATION ----------
function validatePayload(data) {
  if (!data || typeof data !== 'object') throw new Error('Invalid JSON payload.');
  if (!('equity_series' in data)) throw new Error('Missing "equity_series" array.');
  if (!Array.isArray(data.equity_series)) throw new Error('"equity_series" must be an array.');
  if (!('positions' in data)) data.positions = [];
  return data;
}

// ---------- MASTER RENDER ----------
function renderAll(payload, enrichedPositions) {
  el.title.textContent = payload.title || 'Stock Bot';
  el.lastUpdate.textContent = payload.updated_at ? new Date(payload.updated_at).toLocaleString() : '—';

  renderEquityTable(payload.equity_series);
  drawChart(payload.equity_series);

  renderPositions(enrichedPositions);
  renderKpis(payload, enrichedPositions);

  el.raw.textContent = JSON.stringify({ ...payload, positions: enrichedPositions }, null, 2);
  const n = payload.equity_series?.length || 0;
  setStatus(n ? `Loaded ${n} equity point${n === 1 ? '' : 's'}` : 'Loaded (no equity points)', n ? 'ok' : 'warn');
}

// ---------- LOAD + ENRICH ----------
async function enrichPositionsWithLivePrices(positions) {
  // Fetch current prices concurrently; tolerate failures per ticker
  const results = await Promise.allSettled(
    positions.map(async p => {
      const price = await getLivePrice(p.ticker);
      const qty = Number(p.qty || 0);
      const avg = Number(p.avg_price || 0);
      const mv = qty * price;
      const upl = (price - avg) * qty;
      return {
        ...p,
        current_price: price,
        market_value: mv,
        unrealized_pl: upl
      };
    })
  );
  return results.map((res, i) => {
    const p = positions[i];
    if (res.status === 'fulfilled') return res.value;
    // On failure, fall back to zeros but keep base fields
    console.warn(`Price fetch failed for ${p.ticker}:`, res.reason);
    return { ...p, current_price: 0, market_value: 0, unrealized_pl: 0 };
  });
}

async function loadFromServer() {
  hideError();
  setStatus('Loading…');
  try {
    const resp = await fetch(DATA_PATH + CACHE_BUST(), { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} while fetching ${DATA_PATH}`);
    const data = validatePayload(await resp.json());
    const enriched = await enrichPositionsWithLivePrices(data.positions || []);
    renderAll(data, enriched);
  } catch (e) {
    showError(e);
  }
}

function enableLocalFileMode() {
  el.localNotice.classList.remove('hidden');
  el.pickBtn.addEventListener('click', () => el.fileInput.click());
  el.fileInput.addEventListener('change', async () => {
    const f = el.fileInput.files?.[0];
    if (!f) return;
    try {
      const text = await f.text();
      const json = validatePayload(JSON.parse(text));
      const enriched = await enrichPositionsWithLivePrices(json.positions || []);
      renderAll(json, enriched);
      hideError();
      setStatus('Loaded from local file', 'ok');
    } catch (e) {
      showError(e);
    }
  });
}

// ---------- EVENTS ----------
el.retryBtn.addEventListener('click', () => {
  if (location.protocol === 'file:') {
    el.fileInput.value = ''; el.fileInput.click();
  } else {
    loadFromServer();
  }
});
el.showRawBtn.addEventListener('click', () => {
  el.raw.classList.toggle('hidden');
  el.showRawBtn.textContent = el.raw.classList.contains('hidden') ? 'Show raw JSON' : 'Hide raw JSON';
});

// ---------- BOOT ----------
(function init() {
  if (location.protocol === 'file:') {
    setStatus('Waiting for local JSON…', 'warn');
    enableLocalFileMode();
  } else {
    loadFromServer();
  }
})();
