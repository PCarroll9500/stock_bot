// script.js — Inf Money Stock Bot dashboard
'use strict';

const DATA_PATH = './data/portfolio.json';

// CORS proxies tried in order per request — first success wins
const PROXIES = [
  'https://corsproxy.io/?',
  'https://api.allorigins.win/raw?url=',
  'https://api.codetabs.com/v1/proxy?quest=',
];

const PRICE_REFRESH_MS = 60_000;   // auto-refresh interval
const REQUEST_DELAY_MS = 200;       // delay between per-ticker requests (avoid rate limit)

// ── Helpers ──────────────────────────────────────────────────────────────────

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

// ── Price fetching ────────────────────────────────────────────────────────────
// Uses Yahoo Finance v8/chart which returns meta.regularMarketPrice
// (current price, updated continuously) without requiring a session crumb.
// Tries each CORS proxy in order until one returns a valid price.

async function fetchPrice(ticker) {
  // Use a 5-day range so we always get at least some bars even if market is closed today
  const yUrl = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?interval=1d&range=5d`;

  for (const proxy of PROXIES) {
    try {
      const data = await fetchWithTimeout(proxy + encodeURIComponent(yUrl), 6000);
      const result = data?.chart?.result?.[0];
      if (!result) continue;

      // meta.regularMarketPrice is the live/most-recent price
      const live = result.meta?.regularMarketPrice;
      if (live != null && live > 0) return Number(live);

      // fall back to last non-null close in the daily bars
      const closes = result.indicators?.quote?.[0]?.close ?? [];
      for (let i = closes.length - 1; i >= 0; i--) {
        if (closes[i] != null) return Number(closes[i]);
      }
    } catch (e) {
      console.warn(`[${ticker}] proxy ${proxy} failed:`, e.message);
    }
  }
  return null;   // all proxies failed for this ticker
}

// ── Chart ────────────────────────────────────────────────────────────────────

let chartInstance = null;

function renderChart(equityCurve) {
  const canvas = $('equityChart');
  if (!canvas) return;

  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: equityCurve.map(p => p.date),
      datasets: [
        {
          label: 'Portfolio',
          data: equityCurve.map(p => p.portfolio_value),
          borderColor: '#22d3a0',
          backgroundColor: 'rgba(34,211,160,0.08)',
          borderWidth: 2.5,
          pointRadius: equityCurve.length > 20 ? 0 : 4,
          pointHoverRadius: 5,
          fill: true,
          tension: 0.3,
        },
        {
          label: 'QQQ (indexed)',
          data: equityCurve.map(p => p.qqq_indexed),
          borderColor: '#fbbf24',
          backgroundColor: 'rgba(251,191,36,0.04)',
          borderWidth: 2,
          pointRadius: equityCurve.length > 20 ? 0 : 4,
          pointHoverRadius: 5,
          fill: false,
          tension: 0.3,
          borderDash: [5, 3],
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

// ── KPIs ─────────────────────────────────────────────────────────────────────

function renderKpis(portfolio, session) {
  const initial = Number(portfolio.initial_investment || 10000);
  let current = session?.portfolio_close_value ?? session?.portfolio_open_value ?? null;
  if (current == null) {
    const eq = portfolio.equity_curve || [];
    current = eq.length ? eq[eq.length - 1].portfolio_value : initial;
  }

  const totalUsd = current - initial;
  const totalPct = (totalUsd / initial) * 100;
  const todayUsd = session?.session_return_usd ?? null;
  const todayPct = session?.session_return_pct ?? null;

  const eq = portfolio.equity_curve || [];
  const lastEq = eq.length ? eq[eq.length - 1] : null;
  const qqqIdx = lastEq?.qqq_indexed ?? null;
  const vsQqq = qqqIdx != null ? current - qqqIdx : null;
  const vsQqqPct = qqqIdx != null ? ((current - qqqIdx) / qqqIdx) * 100 : null;

  $('kpiPortfolioValue').textContent = fmtUsd(current);
  $('kpiPortfolioValue').className = 'kpi-value';

  $('kpiTotalReturnUsd').textContent = fmtUsd(totalUsd);
  $('kpiTotalReturnUsd').className = 'kpi-value ' + colorClass(totalUsd);
  $('kpiTotalReturnPct').textContent = fmtPct(totalPct);
  $('kpiTotalReturnPct').className = 'kpi-sub ' + colorClass(totalPct);

  $('kpiTodayReturnUsd').textContent = fmtUsd(todayUsd);
  $('kpiTodayReturnUsd').className = 'kpi-value ' + colorClass(todayUsd);
  $('kpiTodayReturnPct').textContent = fmtPct(todayPct);
  $('kpiTodayReturnPct').className = 'kpi-sub ' + colorClass(todayPct);

  $('kpiVsQqq').textContent = fmtUsd(vsQqq);
  $('kpiVsQqq').className = 'kpi-value ' + colorClass(vsQqq);
  $('kpiVsQqqSub').textContent = vsQqqPct != null ? fmtPct(vsQqqPct) + ' vs QQQ' : 'indexed to $10,000';
  $('kpiVsQqqSub').className = 'kpi-sub ' + colorClass(vsQqqPct);
}

// ── Picks Table ───────────────────────────────────────────────────────────────

function finvizUrl(t) { return `https://finviz.com/quote.ashx?t=${t}`; }
function tvUrl(t)     { return `https://www.tradingview.com/symbols/${t}/`; }

// Render the price/return cells for one row (called once initially and on each refresh)
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

let _session = null;  // cached for refresh

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

  // Build all rows immediately with a loading spinner in the price cell
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

  // Fetch live prices one by one, updating each row as price arrives
  for (const pick of session.picks) {
    if (pick.close_price != null) continue;   // already have the stored close price
    const price = await fetchPrice(pick.ticker);
    updatePriceCell(pick, price);
    await sleep(REQUEST_DELAY_MS);             // be polite to Yahoo / proxies
  }

  // Update meta line with timestamp once all prices loaded
  const time = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  $('todayMeta').textContent =
    `Mode: ${session.mode || '—'}  |  Open: ${fmtUsd(session.portfolio_open_value)}` +
    (isClosed ? `  |  Close: ${fmtUsd(session.portfolio_close_value)}` : `  |  Prices as of ${time} — refreshing every 60s`);
}

// Only refresh the price cells — no full re-render
async function refreshPrices() {
  if (!_session?.picks) return;
  const openPicks = _session.picks.filter(p => p.close_price == null);
  if (!openPicks.length) return;

  for (const pick of openPicks) {
    const price = await fetchPrice(pick.ticker);
    updatePriceCell(pick, price);
    await sleep(REQUEST_DELAY_MS);
  }

  const time = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  const meta = $('todayMeta');
  if (meta) meta.textContent = meta.textContent.replace(/Prices as of .+$/, `Prices as of ${time} — refreshing every 60s`);
}

// ── Session History ───────────────────────────────────────────────────────────

function renderHistoryTable(sessions) {
  const tbody = $('historyBody');
  tbody.innerHTML = '';
  if (!sessions?.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No sessions yet.</td></tr>';
    return;
  }
  [...sessions].reverse().forEach(s => {
    const closed = s.portfolio_close_value != null;
    const vsQqq = s.session_return_pct != null && s.qqq_day_return_pct != null
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

// ── Boot ─────────────────────────────────────────────────────────────────────

async function renderPortfolio(portfolio) {
  $('modeBadge').textContent = portfolio.sessions?.length
    ? (portfolio.sessions.at(-1).mode || 'aggressive') : 'no sessions';
  $('updatedAt').textContent = portfolio.updated_at
    ? 'Updated ' + new Date(portfolio.updated_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : '';

  const sessions = portfolio.sessions || [];
  const today = new Date().toISOString().slice(0, 10);
  const session = sessions.find(s => s.date === today) ?? sessions.at(-1) ?? null;

  renderKpis(portfolio, session);

  const eq = portfolio.equity_curve || [];
  if (eq.length) {
    renderChart(eq);
  } else {
    const wrap = $('equityChart')?.parentElement;
    if (wrap) wrap.innerHTML = '<p style="color:var(--muted);text-align:center;padding:2rem 0">Chart will appear after the first session closes.</p>';
  }

  await renderPicksTable(session);
  renderHistoryTable(sessions);
  $('year').textContent = new Date().getFullYear();
}

async function loadFromServer() {
  try {
    const resp = await fetch(DATA_PATH + `?v=${Date.now()}`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    await renderPortfolio(await resp.json());
  } catch (e) {
    console.error('Failed to load portfolio.json:', e);
    $('picksBody').innerHTML  = `<tr><td colspan="9" class="empty">Could not load data: ${e.message}</td></tr>`;
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

// ── Init ─────────────────────────────────────────────────────────────────────

if (location.protocol === 'file:') {
  enableLocalFileMode();
} else {
  loadFromServer();
  setInterval(refreshPrices, PRICE_REFRESH_MS);
}
