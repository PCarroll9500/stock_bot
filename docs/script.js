// script.js — Inf Money Stock Bot dashboard
'use strict';

const DATA_PATH = './data/portfolio.json';

// CORS proxies tried in order — first success wins
const PROXIES = [
  'https://corsproxy.io/?',
  'https://api.allorigins.win/raw?url=',
  'https://api.codetabs.com/v1/proxy?quest=',
];

const PRICE_REFRESH_MS = 60_000;
const REQUEST_DELAY_MS = 200;

// ── Global state ──────────────────────────────────────────────────────────────
let _portfolio  = null;   // full portfolio.json object
let _session    = null;   // current/latest session object
let _livePrices = {};     // { ticker: livePrice } populated as Yahoo prices arrive
let _qqqData    = null;   // { curve: [{date, price, indexed}], startPrice, livePrice }

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

// ── Yahoo Finance helpers ─────────────────────────────────────────────────────

async function fetchPrice(ticker) {
  const yUrl = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?interval=1d&range=5d`;
  for (const proxy of PROXIES) {
    try {
      const data   = await fetchWithTimeout(proxy + encodeURIComponent(yUrl), 6000);
      const result = data?.chart?.result?.[0];
      if (!result) continue;
      const live = result.meta?.regularMarketPrice;
      if (live != null && live > 0) return Number(live);
      const closes = result.indicators?.quote?.[0]?.close ?? [];
      for (let i = closes.length - 1; i >= 0; i--) {
        if (closes[i] != null) return Number(closes[i]);
      }
    } catch (e) {
      console.warn(`[${ticker}] proxy ${proxy} failed:`, e.message);
    }
  }
  return null;
}

// ── QQQ historical data ───────────────────────────────────────────────────────
// Fetches daily QQQ closes from startDate to today, indexed to `initial` dollars.

async function fetchQqqHistory(startDate, initial) {
  const startTs = Math.floor(new Date(startDate + 'T00:00:00').getTime() / 1000);
  const endTs   = Math.floor(Date.now() / 1000) + 86400;
  const yUrl    = `https://query1.finance.yahoo.com/v8/finance/chart/QQQ?interval=1d&period1=${startTs}&period2=${endTs}`;

  for (const proxy of PROXIES) {
    try {
      const data   = await fetchWithTimeout(proxy + encodeURIComponent(yUrl), 8000);
      const result = data?.chart?.result?.[0];
      if (!result) continue;

      const timestamps = result.timestamp || [];
      const closes     = result.indicators?.quote?.[0]?.close || [];
      const meta       = result.meta || {};
      const startPrice = closes.find(c => c != null);
      if (!startPrice) continue;

      const curve = [];
      for (let i = 0; i < timestamps.length; i++) {
        if (closes[i] == null) continue;
        const date    = new Date(timestamps[i] * 1000).toISOString().slice(0, 10);
        const indexed = (closes[i] / startPrice) * initial;
        curve.push({ date, price: closes[i], indexed });
      }

      // Splice in live price for today if market is open
      const livePrice = meta.regularMarketPrice;
      const today     = new Date().toISOString().slice(0, 10);
      if (livePrice && livePrice > 0) {
        const last        = curve.at(-1);
        const liveIndexed = (livePrice / startPrice) * initial;
        if (last && last.date === today) {
          last.price   = livePrice;
          last.indexed = liveIndexed;
        } else {
          curve.push({ date: today, price: livePrice, indexed: liveIndexed });
        }
      }

      return { curve, startPrice, livePrice: livePrice || closes.at(-1) };
    } catch (e) {
      console.warn('[QQQ] proxy failed:', e.message);
    }
  }
  return null;
}

// ── Live portfolio value ──────────────────────────────────────────────────────
// Returns null if session is already closed (caller should use stored close value).

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
      // Fall back to buy price so value doesn't disappear before prices load
      liveInvested += price != null ? price * pick.shares : buyVal;
    }
  }

  const cash = Math.max(0, openValue - totalInvested);
  return cash + liveInvested;
}

// ── KPIs ──────────────────────────────────────────────────────────────────────

function renderKpis(portfolio, session, livePrices, qqqData) {
  const initial   = Number(portfolio.initial_investment || 10000);
  const openValue = session?.portfolio_open_value ?? initial;

  // Portfolio value: stored close if available, else live computation
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
  const todayUsd = currentValue - openValue;
  const todayPct = openValue > 0 ? (todayUsd / openValue) * 100 : 0;

  // QQQ — live fetched data preferred, equity_curve as fallback
  let qqqIndexedNow = null;
  let qqqRetPct     = null;
  if (qqqData?.startPrice && qqqData?.livePrice) {
    qqqIndexedNow = (qqqData.livePrice / qqqData.startPrice) * initial;
    qqqRetPct     = ((qqqData.livePrice - qqqData.startPrice) / qqqData.startPrice) * 100;
  } else {
    const eq = portfolio.equity_curve || [];
    qqqIndexedNow = eq.length ? eq.at(-1).qqq_indexed : null;
  }

  const vsQqqUsd = qqqIndexedNow != null ? currentValue - qqqIndexedNow : null;
  const vsQqqPct = qqqIndexedNow != null && qqqIndexedNow > 0
    ? (vsQqqUsd / qqqIndexedNow) * 100 : null;

  // Portfolio value
  $('kpiPortfolioValue').textContent = fmtUsd(currentValue);
  $('kpiPortfolioValue').className   = 'kpi-value';
  $('kpiVsInitial').textContent      = `vs ${fmtUsd(initial)} initial${isLive ? ' · live' : ''}`;

  // Total return
  $('kpiTotalReturnUsd').textContent = fmtUsd(totalUsd);
  $('kpiTotalReturnUsd').className   = 'kpi-value ' + colorClass(totalUsd);
  $('kpiTotalReturnPct').textContent = fmtPct(totalPct);
  $('kpiTotalReturnPct').className   = 'kpi-sub ' + colorClass(totalPct);

  // Today's return
  $('kpiTodayReturnUsd').textContent = fmtUsd(todayUsd);
  $('kpiTodayReturnUsd').className   = 'kpi-value ' + colorClass(todayUsd);
  $('kpiTodayReturnPct').textContent = fmtPct(todayPct);
  $('kpiTodayReturnPct').className   = 'kpi-sub ' + colorClass(todayPct);

  // vs NASDAQ — shows dollar alpha, sub shows QQQ's own return so user sees market trend
  if (vsQqqUsd != null) {
    $('kpiVsQqq').textContent    = fmtUsd(vsQqqUsd);
    $('kpiVsQqq').className      = 'kpi-value ' + colorClass(vsQqqUsd);
    const sub = qqqRetPct != null
      ? `QQQ ${fmtPct(qqqRetPct)} · you ${fmtPct(vsQqqPct)} alpha`
      : fmtPct(vsQqqPct) + ' alpha vs QQQ';
    $('kpiVsQqqSub').textContent = sub;
    $('kpiVsQqqSub').className   = 'kpi-sub ' + colorClass(vsQqqUsd);
  } else {
    $('kpiVsQqq').textContent    = '—';
    $('kpiVsQqqSub').textContent = qqqData === null ? 'fetching NASDAQ data…' : 'no NASDAQ data';
    $('kpiVsQqqSub').className   = 'kpi-sub neutral';
  }
}

// ── Chart ─────────────────────────────────────────────────────────────────────

let chartInstance = null;

function buildChartDatasets(portfolio, session, livePrices, qqqData) {
  const initial   = Number(portfolio.initial_investment || 10000);
  const startDate = portfolio.start_date;
  const sessions  = portfolio.sessions || [];

  // Map date → portfolio close value from session history
  const sessionValues = {};
  for (const s of sessions) {
    if (s.portfolio_close_value != null) sessionValues[s.date] = s.portfolio_close_value;
  }

  // Add today's live computed value if session is open
  const today   = new Date().toISOString().slice(0, 10);
  const liveVal = computeLivePortfolioValue(session, livePrices);
  if (liveVal != null) sessionValues[today] = liveVal;

  // Date axis: use QQQ curve dates (dense daily) or fall back to session dates
  let allDates;
  if (qqqData?.curve?.length) {
    allDates = qqqData.curve.map(p => p.date);
    if (startDate && !allDates.includes(startDate)) {
      allDates = [startDate, ...allDates].sort();
    }
  } else {
    const dateSet = new Set([
      ...(startDate ? [startDate] : []),
      ...Object.keys(sessionValues),
      ...(portfolio.equity_curve || []).map(p => p.date),
    ]);
    allDates = [...dateSet].sort();
  }

  if (!allDates.length) return null;

  // Portfolio line: $initial at start_date, carry-forward session closes
  const portfolioPoints = [];
  let lastVal = initial;
  for (const date of allDates) {
    if (date < (startDate || '')) { portfolioPoints.push(null); continue; }
    if (date === startDate)        lastVal = initial;
    if (sessionValues[date] != null) lastVal = sessionValues[date];
    portfolioPoints.push(lastVal);
  }

  // QQQ line: live Yahoo data or equity_curve fallback
  let qqqPoints = [];
  if (qqqData?.curve?.length) {
    const qqqMap = Object.fromEntries(qqqData.curve.map(p => [p.date, p.indexed]));
    qqqPoints = allDates.map(d => qqqMap[d] ?? null);
  } else {
    const eq     = portfolio.equity_curve || [];
    const qqqMap = Object.fromEntries(eq.map(p => [p.date, p.qqq_indexed]));
    qqqPoints = allDates.map(d => qqqMap[d] ?? null);
  }

  return { labels: allDates, portfolioPoints, qqqPoints };
}

function renderChart(portfolio, session, livePrices, qqqData) {
  const canvas = $('equityChart');
  if (!canvas) return;

  const data = buildChartDatasets(portfolio, session, livePrices, qqqData);
  if (!data) {
    const wrap = canvas.parentElement;
    if (wrap) wrap.innerHTML = '<p style="color:var(--muted);text-align:center;padding:2rem 0">Chart will appear after the first session closes.</p>';
    return;
  }

  if (chartInstance) chartInstance.destroy();

  const hasQqq = data.qqqPoints.some(v => v != null);
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
        {
          label: 'QQQ (indexed)',
          data: data.qqqPoints,
          borderColor: '#fbbf24',
          backgroundColor: 'rgba(251,191,36,0.04)',
          borderWidth: 2,
          pointRadius: sparse ? 4 : 0,
          pointHoverRadius: 5,
          fill: false,
          tension: 0.3,
          borderDash: [5, 3],
          spanGaps: true,
          hidden: !hasQqq,
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

  // Build all rows immediately with price cell showing "fetching…"
  session.picks.forEach(pick => {
    const hasBuy = pick.buy_price > 0;
    const tr = document.createElement('tr');
    tr.id = `row-${pick.ticker}`;
    tr.innerHTML = `
      <td>
        <div class="ticker-cell">
          <a href="${finvizUrl(pick.ticker)}" target="_blank" rel="noopener" class="ticker-link">${pick.ticker}</a>
          <span class="score-badge">${pick.score}</span>
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

  // Fetch prices one by one — update row + KPIs + chart as each price arrives
  for (const pick of session.picks) {
    if (pick.close_price != null) {
      _livePrices[pick.ticker] = pick.close_price;
      continue;
    }
    const price = await fetchPrice(pick.ticker);
    if (price != null) _livePrices[pick.ticker] = price;
    updatePriceCell(pick, price);
    if (_portfolio) {
      renderKpis(_portfolio, _session, _livePrices, _qqqData);
      renderChart(_portfolio, _session, _livePrices, _qqqData);
    }
    await sleep(REQUEST_DELAY_MS);
  }

  const time = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  $('todayMeta').textContent =
    `Mode: ${session.mode || '—'}  |  Open: ${fmtUsd(session.portfolio_open_value)}` +
    (isClosed ? `  |  Close: ${fmtUsd(session.portfolio_close_value)}` : `  |  Prices as of ${time} — refreshing every 60s`);
}

// ── Price + QQQ refresh (runs every 60s) ──────────────────────────────────────

async function refreshPrices() {
  if (!_portfolio) return;
  const openPicks = (_session?.picks || []).filter(p => p.close_price == null);

  for (const pick of openPicks) {
    const price = await fetchPrice(pick.ticker);
    if (price != null) _livePrices[pick.ticker] = price;
    updatePriceCell(pick, price);
    await sleep(REQUEST_DELAY_MS);
  }

  // Refresh QQQ so chart + vs-NASDAQ KPI stay current
  const startDate = _portfolio.start_date || new Date().toISOString().slice(0, 10);
  const initial   = Number(_portfolio.initial_investment || 10000);
  const fresh = await fetchQqqHistory(startDate, initial);
  if (fresh) _qqqData = fresh;

  renderKpis(_portfolio, _session, _livePrices, _qqqData);
  renderChart(_portfolio, _session, _livePrices, _qqqData);

  if (openPicks.length) {
    const time = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
    const meta = $('todayMeta');
    if (meta) meta.textContent = meta.textContent.replace(/Prices as of .+$/, `Prices as of ${time} — refreshing every 60s`);
  }
}

// ── Session history table ─────────────────────────────────────────────────────

function renderHistoryTable(sessions) {
  const tbody = $('historyBody');
  tbody.innerHTML = '';
  if (!sessions?.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No sessions yet.</td></tr>';
    return;
  }
  [...sessions].reverse().forEach(s => {
    const closed = s.portfolio_close_value != null;
    const vsQqq  = s.session_return_pct != null && s.qqq_day_return_pct != null
      ? s.session_return_pct - s.qqq_day_return_pct : null;
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
      <td class="${colorClass(vsQqq)}">${vsQqq != null ? fmtPct(vsQqq, true) + ' alpha' : '—'}</td>
      <td>${n} stock${n !== 1 ? 's' : ''}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────────

async function renderPortfolio(portfolio) {
  _portfolio  = portfolio;
  _livePrices = {};
  _qqqData    = null;

  $('modeBadge').textContent = portfolio.sessions?.length
    ? (portfolio.sessions.at(-1).mode || 'aggressive') : 'no sessions';
  $('updatedAt').textContent = portfolio.updated_at
    ? 'Updated ' + new Date(portfolio.updated_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : '';

  const sessions = portfolio.sessions || [];
  const today    = new Date().toISOString().slice(0, 10);
  _session = sessions.find(s => s.date === today) ?? sessions.at(-1) ?? null;

  // Initial paint with stored values (no live data yet)
  renderKpis(portfolio, _session, {}, null);
  renderChart(portfolio, _session, {}, null);
  renderHistoryTable(sessions);
  $('year').textContent = new Date().getFullYear();

  // Fetch QQQ history and pick prices in parallel
  const startDate = portfolio.start_date || today;
  const initial   = Number(portfolio.initial_investment || 10000);

  const [qqqResult] = await Promise.all([
    fetchQqqHistory(startDate, initial),
    renderPicksTable(_session),   // populates _livePrices as each price arrives
  ]);

  // Final update with everything — QQQ + all live prices
  if (qqqResult) _qqqData = qqqResult;
  renderKpis(portfolio, _session, _livePrices, _qqqData);
  renderChart(portfolio, _session, _livePrices, _qqqData);
}

async function loadFromServer() {
  try {
    const resp = await fetch(DATA_PATH + `?v=${Date.now()}`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    await renderPortfolio(await resp.json());
  } catch (e) {
    console.error('Failed to load portfolio.json:', e);
    $('picksBody').innerHTML   = `<tr><td colspan="9" class="empty">Could not load data: ${e.message}</td></tr>`;
    $('historyBody').innerHTML = `<tr><td colspan="7" class="empty">Could not load data.</td></tr>`;
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
