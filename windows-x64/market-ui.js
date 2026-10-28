'use strict';
const tabs = [...document.querySelectorAll('[role="tab"]')];
function selectTab(tab) {
  for (const item of tabs) {
    const active = item === tab;
    item.setAttribute('aria-selected', String(active));
    item.tabIndex = active ? 0 : -1;
    $(item.getAttribute('aria-controls')).hidden = !active;
  }
  if (tab.id === 'price-tab') draw();
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1) : tabs[(index + (event.key === 'ArrowLeft' ? tabs.length - 1 : 1)) % tabs.length];
    selectTab(next); next.focus();
  });
});

function tableRow(values, className = '') {
  const row = document.createElement('tr');
  row.className = className;
  for (const value of values) {
    const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
  }
  return row;
}
function institutionProgress(o) {
  const done = `${o.side === 'buy' ? 'Bought' : 'Sold'} ${o.filled} of ${o.shares} shares`;
  if (o.startPrice === null) return `${done}; starts at ${(o.startMs / 1000).toFixed(1)} s`;
  if (o.averagePrice === null) return `${done} since ${(o.startMs / 1000).toFixed(1)} s`;
  const cost = (o.side === 'buy' ? o.averagePrice - o.startPrice : o.startPrice - o.averagePrice) / 100;
  return `${done} since ${(o.startMs / 1000).toFixed(1)} s, at ${usd.format(o.averagePrice / 100)} on average: ` +
    `${usd.format(Math.abs(cost))} a share ${cost >= 0 ? 'worse' : 'better'} than the mid when it began`;
}
const MARKET_ANCHOR = "Lazy maker";

function renderBook() {
  const book = marketRound.book;
  const state = marketRound.complete ? "Final results" : running ? "Live" : "Ready";
  $("bot-status").textContent = marketRound.complete ? "Round complete" : running ? "Trading" : "Ready";
  $("pnl-status").textContent = state;
  $("pnl-fair").textContent = usd.format((book.settlementPrice ?? prices.at(-1) * 100) / 100);
  const ranked = [...book.players].filter(p => p.name !== MARKET_ANCHOR).sort((a, b) => b.pnl - a.pnl || a.name.localeCompare(b.name));
  const anchor = book.players.filter(p => p.name === MARKET_ANCHOR);
  $("leaderboard").replaceChildren(...[...ranked, ...anchor].map((p, i) => {
    const row = tableRow([p.name === MARKET_ANCHOR ? '—' : i + 1, p.name === MARKET_ANCHOR ? `${p.name} (market anchor, unranked)` : p.name,
      usd.format(p.cash / 100), p.inventory, p.volume, p.pickedOff ?? 0, usd.format(p.pnl / 100)], p.pnl < 0 ? "sell-text" : "buy-text");
    if (p.institution) row.setAttribute("title", institutionProgress(p.institution));
    return row;
  }));
  for (const side of ['buy', 'sell']) {
    const orders = book.orders.filter(o => o.side === side).sort((a, b) =>
      (side === 'buy' ? b.price - a.price : a.price - b.price) || a.id - b.id);
    $(`${side}-count`).textContent = `(${orders.length})`;
    $(`${side}-orders`).replaceChildren(...(orders.length ? orders.map(o =>
      tableRow([usd.format(o.price / 100), o.quantity, o.bot, `#${o.id}`], `${side}-text`)) : [tableRow(['No orders', '', '', ''])]));
  }
  $('reference-price').textContent = usd.format(prices.at(-1));
  $('last-fill').textContent = book.lastPrice === null ? '—' : usd.format(book.lastPrice / 100);
  $('volume').textContent = book.volume;
  $('order-log').replaceChildren(...(book.events.length ? book.events.slice(-200).reverse().map(e =>
    tableRow([`${(e.time / 1000).toFixed(6)}s`, e.type, e.details], e.type === 'FILL' ? 'buy-text' : '')) : [tableRow(['—', '—', 'Start a round to see orders and fills.'])]));
}
$('export-log').addEventListener('click', async () => {
  let log;
  try { log = await api('/api/log'); }
  catch (error) { $('error').textContent = error.message; return; }
  const blob = new Blob([JSON.stringify(log, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a'); link.href = url; link.download = 'stock_151-order-log.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
renderBook();
