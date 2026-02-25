// script.js — Inf Money Stock Bot dashboard
'use strict';

const DATA_PATH        = './data/portfolio.json';
const PRICE_REFRESH_MS = 60_000;
const REQUEST_DELAY_MS = 150;   // ms between per-ticker requests
const CACHE_TTL_MS     = 55_000; // price cache TTL (just under the 60s refresh)

// ── Global state ──────────────────────────────────────────────────────────────
let _portfolio  = null;
let _session    = null;
let _livePrices = {};   // { ticker: price } — latest confirmed price per ticker

// ── Price cache ───────────────────────────────────────────────────────────────
// Keeps the last good price + timestamp so stale prices survive brief outages.
const _cache = {}; // { ticker: { price, ts } }

function cacheGet(ticker) {
  const e = _cache[ticker];
  if (!e) return null;
  if (Date.now() - e.ts < CACHE_TTL_MS) return e.price; // fresh
  return null; // expired (stale copy still in _cache for fallback)
}
function cacheSet(ticker, price) {
  _cache[ticker] = { price, ts: Date.now() };
}
function cacheStale(ticker) {
  return _cache[ticker]?.price ?? null; // stale-but-better-than-nothing
}

// ── CORS proxy pool ───────────────────────────────────────────────────────────
// All requests race through every proxy simultaneously; first response wins.
const PROXY_FNS = [
  u => `https://corsproxy.io/?${encodeURIComponent(u)}`,
  u => `https://api.allorigins.win/raw?url=${encodeURIComponent(u)}`,
  u => `https://api.codetabs.com/v1/proxy?quest=${encodeURIComponent(u)}`,
];

async function _fetchWithTimeout(url, ms, asText = false) {
  const ctl = new AbortController();
  const t   = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctl.signal, cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return asText ? r.text() : r.json();
  } finally {
    clearTimeout(t);
  }
}

// Race all proxies; return first valid response or null.
async function raceProxies(url, timeoutMs = 6000, asText = false) {
  try {
    return await Promise.any(
      PROXY_FNS.map(fn =>
        _fetchWithTimeout(fn(url), timeoutMs, asText)
          .then(d => {
            if (d == null) throw new Error('empty');
            return d;
          })
      )
    );
  } catch {
    return null; // AggregateError — all proxies failed
  }
}

// ── Yahoo Finance fetchers ─────────────────────────────────────────────────────
// Try both Yahoo hosts (query1 / query2) in the same race.
const YAHOO_HOSTS = ['query1', 'query2'];

function yahooChartUrl(host, ticker, extraParams = '') {
  return `https://${host}.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?interval=1d&range=5d${extraParams}`;
}

function extractYahooPrice(data) {
  const result = data?.chart?.result?.[0];
  if (!result) return null;
  const live = result.meta?.regularMarketPrice;
  if (live != null && live > 0) return { price: Number(live), result };
  const closes = result.indicators?.quote?.[0]?.close ?? [];
  for (let i = closes.length - 1; i >= 0; i--) {
    if (closes[i] != null) return { price: Number(closes[i]), result };
  }
  return null;
}

async function fetchYahooPrice(ticker) {
  // Fire all (proxy × host) combinations at once — 6 concurrent attempts
  const urls = YAHOO_HOSTS.flatMap(host =>
    PROXY_FNS.map(fn => fn(yahooChartUrl(host, ticker)))
  );
  try {
    const data = await Promise.any(
      urls.map(u =>
        _fetchWithTimeout(u, 6000)
          .then(d => {
            const hit = extractYahooPrice(d);
            if (!hit) throw new Error('no price');
            return hit.price;
          })
      )
    );
    return data;
  } catch {
    return null;
  }
}

// ── Stooq fallback ────────────────────────────────────────────────────────────
// Stooq is a reliable Polish financial site with no meaningful rate limit.
// Returns CSV with the last daily close — no real-time prices.

function parseStooqClose(csv) {
  if (!csv || typeof csv !== 'string') return null;
  const lines = csv.trim().split('\n');
  if (lines.length < 2) return null;
  const headers = lines[0].split(',').map(h => h.trim().replace(/"/g, '').toLowerCase());
  const vals    = lines[1].split(',').map(v => v.trim().replace(/"/g, ''));
  const ci = headers.indexOf('close');
  if (ci < 0) return null;
  const p = parseFloat(vals[ci]);
  return isNaN(p) ? null : p;
}

async function fetchStooqPrice(ticker) {
  const url  = `https://stooq.com/q/l/?s=${encodeURIComponent(ticker.toLowerCase())}.us&f=sd2ohlcv&h&e=csv`;
  const text = await raceProxies(url, 5000, /*asText=*/true);
  return parseStooqClose(text);
}

// ── Combined price fetch ───────────────────────────────────────────────────────
// 1. Fresh cache → 2. Yahoo (parallel proxy+host race) → 3. Stooq → 4. Stale cache

async function fetchPrice(ticker) {
  const fresh = cacheGet(ticker);
  if (fresh != null) return fresh;

  let price = await fetchYahooPrice(ticker);

  if (price == null) {
    console.info(`[${ticker}] Yahoo failed, trying Stooq…`);
    price = await fetchStooqPrice(ticker);
  }

  if (price != null) {
    cacheSet(ticker, price);
    return price;
  }

  // Return stale price so UI doesn't go blank
  const stale = cacheStale(ticker);
  if (stale != null) console.info(`[${ticker}] Using stale price: ${stale}`);
  return stale;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function fmt(n, decimals = 2) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}
function fmtUsd(n) { return n == null || isNaN(n) ? '—' : '$' + fmt(n, 2); }
function fmtPct(n, plus = true) {
  if (n == null || isNaN(n)) return '—';
  return (plus && n >= 0 ? '+' : '') + fmt(n, 2) + '%';
}
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso.length <= 10 ? iso + 'T00:00:00' : iso);
  return isNaN(d) ? iso : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}
function colorClass(n) {
  if (n == null || isNaN(n)) return 'neutral';
  return n >= 0 ? 'up' : 'down';
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Live portfolio value ──────────────────────────────────────────────────────

function computeLivePortfolioValue(session, livePrices) {
  if (!session || session.portfolio_close_value != null) return null;

  const openValue = session.portfolio_open_value || 0;
  let totalInvested = 0;
  let liveInvested  = 0;

  for (const pick of (session.picks || [])) {
    if (pick.shares > 0 && pick.buy_price > 0) {
      const buyVal = pick.buy_value || pick.buy_price * pick.shares;
      totalInvested += buyVal;
      const price = livePrices[pick.ticker];
      liveInvested += price != null ? price * pick.shares : buyVal;
    }
  }

  return Math.max(0, openValue - totalInvested) + liveInvested;
}

// ── KPIs ──────────────────────────────────────────────────────────────────────

function renderKpis(portfolio, session, livePrices) {
  const initial   = Number(portfolio.initial_investment || 10000);
  const openValue = session?.portfolio_open_value ?? initial;

  // Portfolio value
  let currentValue;
  let isLive = false;
  if (session?.portfolio_close_value != null) {
    currentValue = session.portfolio_close_value;
  } else {
    const liveVal = computeLivePortfolioValue(session, livePrices);
    if (liveVal != null) {
      currentValue = liveVal;
      isLive       = true;
    } else {
      const eq = portfolio.equity_curve || [];
      currentValue = eq.length ? eq.at(-1).portfolio_value : initial;
    }
  }

  const totalUsd = currentValue - initial;
  const totalPct = (totalUsd / initial) * 100;

  // Today's return is only meaningful when the active session is actually today.
  const today = new Date().toISOString().slice(0, 10);
  const sessionIsToday = session?.date === today;
  const todayUsd = sessionIsToday ? currentValue - openValue : 0;
  const todayPct = sessionIsToday && openValue > 0 ? (todayUsd / openValue) * 100 : 0;

  $('kpiPortfolioValue').textContent = fmtUsd(currentValue);
  $('kpiPortfolioValue').className   = 'kpi-value';
  $('kpiVsInitial').textContent      = `vs ${fmtUsd(initial)} initial${isLive ? ' · live' : ''}`;

  $('kpiTotalReturnUsd').textContent = fmtUsd(totalUsd);
  $('kpiTotalReturnUsd').className   = 'kpi-value ' + colorClass(totalUsd);
  $('kpiTotalReturnPct').textContent = fmtPct(totalPct);
  $('kpiTotalReturnPct').className   = 'kpi-sub ' + colorClass(totalPct);

  $('kpiTodayReturnUsd').textContent = fmtUsd(todayUsd);
  $('kpiTodayReturnUsd').className   = 'kpi-value ' + colorClass(todayUsd);
  $('kpiTodayReturnPct').textContent = fmtPct(todayPct);
  $('kpiTodayReturnPct').className   = 'kpi-sub ' + colorClass(todayPct);
}

// ── Chart ─────────────────────────────────────────────────────────────────────

let chartInstance = null;

function buildChartDatasets(portfolio, session, livePrices) {
  const initial   = Number(portfolio.initial_investment || 10000);
  const startDate = portfolio.start_date;
  const sessions  = portfolio.sessions || [];

  const sessionValues = {};
  for (const s of sessions) {
    if (s.portfolio_close_value != null) sessionValues[s.date] = s.portfolio_close_value;
  }

  const today   = new Date().toISOString().slice(0, 10);
  const liveVal = computeLivePortfolioValue(session, livePrices);
  if (liveVal != null) sessionValues[today] = liveVal;

  const dateSet = new Set([
    ...(startDate ? [startDate] : []),
    ...Object.keys(sessionValues),
    ...(portfolio.equity_curve || []).map(p => p.date),
  ]);
  const allDates = [...dateSet].sort();

  if (!allDates.length) return null;

  const portfolioPoints = [];
  let lastVal = initial;
  for (const date of allDates) {
    if (date < (startDate || '')) { portfolioPoints.push(null); continue; }
    if (date === startDate)        lastVal = initial;
    if (sessionValues[date] != null) lastVal = sessionValues[date];
    portfolioPoints.push(lastVal);
  }

  return { labels: allDates, portfolioPoints };
}

function renderChart(portfolio, session, livePrices) {
  const canvas = $('equityChart');
  if (!canvas) return;

  const data = buildChartDatasets(portfolio, session, livePrices);
  if (!data) {
    const wrap = canvas.parentElement;
    if (wrap) wrap.innerHTML = '<p style="color:var(--muted);text-align:center;padding:2rem 0">Chart will appear after the first session closes.</p>';
    return;
  }

  if (chartInstance) chartInstance.destroy();

  const sparse = data.labels.length <= 20;

  chartInstance = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: data.labels,
      datasets: [
        {
          label: 'Portfolio',
          data: data.portfolioPoints,
          borderColor: '#22d3a0',
          backgroundColor: 'rgba(34,211,160,0.08)',
          borderWidth: 2.5,
          pointRadius: sparse ? 4 : 0,
          pointHoverRadius: 5,
          fill: true,
          tension: 0.3,
          spanGaps: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1e2438',
          borderColor: 'rgba(255,255,255,0.1)',
          borderWidth: 1,
          titleColor: '#e2e8f0',
          bodyColor: '#94a3b8',
          padding: 10,
          callbacks: { label: ctx => ` ${ctx.dataset.label}: $${fmt(ctx.parsed.y)}` },
        },
      },
      scales: {
        x: { grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: '#8892a4', font: { size: 11 } } },
        y: {
          grid: { color: 'rgba(255,255,255,0.04)' },
          ticks: { color: '#8892a4', font: { size: 11 }, callback: v => '$' + fmt(v, 0) },
        },
      },
    },
  });
}

// ── Picks table ───────────────────────────────────────────────────────────────

function finvizUrl(t) { return `https://finviz.com/quote.ashx?t=${t}`; }
function tvUrl(t)     { return `https://www.tradingview.com/symbols/${t}/`; }

function updatePriceCell(pick, livePrice) {
  const row = document.getElementById(`row-${pick.ticker}`);
  if (!row) return;

  const hasClosed = pick.close_price != null;
  const hasBuy    = pick.buy_price > 0;
  const price     = hasClosed ? pick.close_price : livePrice;
  const isLive    = !hasClosed && price != null;

  const retPct = hasClosed ? pick.day_return_pct
    : (price != null && hasBuy ? ((price - pick.buy_price) / pick.buy_price) * 100 : null);
  const retUsd = hasClosed ? pick.day_return_usd
    : (price != null && hasBuy && pick.shares > 0 ? (price - pick.buy_price) * pick.shares : null);

  const badge = isLive ? '<span class="live-badge">LIVE</span>'
    : hasClosed ? '<span class="closed-badge">CLOSE</span>' : '';

  row.querySelector('.price-cell').innerHTML =
    (price != null ? fmtUsd(price) : '<span class="neutral">—</span>') + ' ' + badge;
  row.querySelector('.ret-pct').className = `ret-pct ${colorClass(retPct)}`;
  row.querySelector('.ret-pct').textContent = fmtPct(retPct);
  row.querySelector('.ret-usd').className = `ret-usd ${colorClass(retUsd)}`;
  row.querySelector('.ret-usd').textContent = retUsd != null ? fmtUsd(retUsd) : '—';
}

async function renderPicksTable(session) {
  _session = session;
  const tbody = $('picksBody');
  tbody.innerHTML = '';

  if (!session?.picks?.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty">No picks for this session.</td></tr>';
    return;
  }

  const isClosed = session.portfolio_close_value != null;
  $('todayTitle').textContent = fmtDate(session.date) + ' Picks';
  $('todayMeta').textContent =
    `Mode: ${session.mode || '—'}  |  Open: ${fmtUsd(session.portfolio_open_value)}` +
    (isClosed ? `  |  Close: ${fmtUsd(session.portfolio_close_value)}` : '  |  Live prices loading…');

  session.picks.forEach(pick => {
    const hasBuy = pick.buy_price > 0;
    const tr = document.createElement('tr');
    tr.id = `row-${pick.ticker}`;
    tr.innerHTML = `
      <td>
        <div class="ticker-cell">
          <a href="${finvizUrl(pick.ticker)}" target="_blank" rel="noopener" class="ticker-link">${pick.ticker}</a>
          <a href="${tvUrl(pick.ticker)}" target="_blank" rel="noopener" class="tv-link" title="TradingView">&#9654;</a>
        </div>
      </td>
      <td>${pick.score}/10</td>
      <td>
        <div class="alloc-bar-wrap">
          ${fmt(pick.allocation_pct, 1)}%
          <div class="alloc-bar-bg">
            <div class="alloc-bar-fill" style="width:${Math.min(pick.allocation_pct, 100)}%"></div>
          </div>
        </div>
      </td>
      <td>${pick.shares > 0 ? pick.shares : '—'}</td>
      <td>${hasBuy ? fmtUsd(pick.buy_price) : '<span class="neutral">—</span>'}</td>
      <td class="price-cell">
        ${pick.close_price != null
          ? fmtUsd(pick.close_price) + ' <span class="closed-badge">CLOSE</span>'
          : '<span class="loading-price">fetching…</span>'}
      </td>
      <td class="ret-pct ${colorClass(pick.day_return_pct)}">${fmtPct(pick.day_return_pct)}</td>
      <td class="ret-usd ${colorClass(pick.day_return_usd)}">${pick.day_return_usd != null ? fmtUsd(pick.day_return_usd) : '—'}</td>
      <td class="reason-cell">${pick.reason || '—'}</td>
    `;
    tbody.appendChild(tr);
  });

  // Fetch prices; update row + KPIs + chart as each price arrives
  for (const pick of session.picks) {
    if (pick.close_price != null) {
      _livePrices[pick.ticker] = pick.close_price;
      cacheSet(pick.ticker, pick.close_price);
      continue;
    }
    const price = await fetchPrice(pick.ticker);
    if (price != null) _livePrices[pick.ticker] = price;
    updatePriceCell(pick, price);
    if (_portfolio) {
      renderKpis(_portfolio, _session, _livePrices);
      renderChart(_portfolio, _session, _livePrices);
    }
    await sleep(REQUEST_DELAY_MS);
  }

  const time = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  $('todayMeta').textContent =
    `Mode: ${session.mode || '—'}  |  Open: ${fmtUsd(session.portfolio_open_value)}` +
    (isClosed ? `  |  Close: ${fmtUsd(session.portfolio_close_value)}` : `  |  Prices as of ${time} — refreshing every 60s`);
}

// ── Refresh (every 60s) ───────────────────────────────────────────────────────

async function refreshPrices() {
  if (!_portfolio) return;
  const openPicks = (_session?.picks || []).filter(p => p.close_price == null);

  for (const pick of openPicks) {
    const price = await fetchPrice(pick.ticker);
    if (price != null) _livePrices[pick.ticker] = price;
    updatePriceCell(pick, price);
    await sleep(REQUEST_DELAY_MS);
  }

  renderKpis(_portfolio, _session, _livePrices);
  renderChart(_portfolio, _session, _livePrices);

  if (openPicks.length) {
    const time = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
    const meta = $('todayMeta');
    if (meta) meta.textContent = meta.textContent.replace(/Prices as of .+$/, `Prices as of ${time} — refreshing every 60s`);
  }
}

// ── Session history ───────────────────────────────────────────────────────────

function renderHistoryTable(sessions) {
  const tbody = $('historyBody');
  tbody.innerHTML = '';
  if (!sessions?.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No sessions yet.</td></tr>';
    return;
  }
  [...sessions].reverse().forEach(s => {
    const closed = s.portfolio_close_value != null;
    const n = s.picks?.length ?? 0;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${fmtDate(s.date)}</td>
      <td><span class="badge" style="font-size:.7rem">${s.mode || '—'}</span></td>
      <td>${fmtUsd(s.portfolio_open_value)}</td>
      <td>${closed ? fmtUsd(s.portfolio_close_value) : '<span class="neutral">open</span>'}</td>
      <td class="${colorClass(s.session_return_pct)}">
        ${closed ? fmtPct(s.session_return_pct) + ' (' + fmtUsd(s.session_return_usd) + ')' : '—'}
      </td>
      <td>${n} stock${n !== 1 ? 's' : ''}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────────

async function renderPortfolio(portfolio) {
  _portfolio  = portfolio;
  _livePrices = {};

  $('modeBadge').textContent = portfolio.sessions?.length
    ? (portfolio.sessions.at(-1).mode || 'aggressive') : 'paper';
  $('updatedAt').textContent = portfolio.updated_at
    ? 'Updated ' + new Date(portfolio.updated_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : '';

  const sessions = portfolio.sessions || [];
  const today    = new Date().toISOString().slice(0, 10);
  _session = sessions.find(s => s.date === today) ?? sessions.at(-1) ?? null;

  // Initial paint with stored values
  renderKpis(portfolio, _session, {});
  renderChart(portfolio, _session, {});
  renderHistoryTable(sessions);
  $('year').textContent = new Date().getFullYear();

  await renderPicksTable(_session);

  renderKpis(portfolio, _session, _livePrices);
  renderChart(portfolio, _session, _livePrices);
}

async function loadFromServer() {
  try {
    const resp = await fetch(DATA_PATH + `?v=${Date.now()}`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    await renderPortfolio(await resp.json());
  } catch (e) {
    console.error('Failed to load portfolio.json:', e);
    $('picksBody').innerHTML   = `<tr><td colspan="9" class="empty">Could not load data: ${e.message}</td></tr>`;
    $('historyBody').innerHTML = `<tr><td colspan="6" class="empty">Could not load data.</td></tr>`;
    $('modeBadge').textContent = 'error';
  }
}

function enableLocalFileMode() {
  $('localNotice').classList.remove('hidden');
  $('pickBtn').addEventListener('click', () => $('fileInput').click());
  $('fileInput').addEventListener('change', async () => {
    const f = $('fileInput').files?.[0];
    if (!f) return;
    try {
      $('localNotice').classList.add('hidden');
      await renderPortfolio(JSON.parse(await f.text()));
    } catch (e) {
      $('picksBody').innerHTML = `<tr><td colspan="9" class="empty">Invalid JSON: ${e.message}</td></tr>`;
    }
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

if (location.protocol === 'file:') {
  enableLocalFileMode();
} else {
  loadFromServer();
  setInterval(refreshPrices, PRICE_REFRESH_MS);
}
