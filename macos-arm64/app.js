'use strict';
const DURATION_MS = 60000, INITIAL_PRICE = 100;
const $ = id => document.getElementById(id);
const canvas = $('chart');
const ctx = canvas.getContext('2d');
const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
let prices = [INITIAL_PRICE];
let running = false;
let marketRound = { complete: false, book: { orders: [], players: [], events: [], settlementPrice: null, lastPrice: null, volume: 0 } };
let requesting = 0;
let roundGeneration = 0;
const CONTROLS = ['start', 'sigma', 'seed', 'processing', 'model-mode', 'board-link', 'pace', 'price-interval',
  'customer-interval', 'heartbeat-interval', 'order-budget', 'downlink', 'link-latency', 'feed-queue', 'run-mode', 'window',
  'board-16bit', 'fair-snipers', 'use-link-measured'];
const STANDARD = { price: 400, heartbeat: 100, budget: 64, downlink: 30, latency: 1 };
const PACES = { standard: { price: 400, heartbeat: 100, budget: 64, downlink: 30, feed: false },
  fast: { price: 60, heartbeat: 15, budget: 64, downlink: 100, feed: true },
  'very-fast': { price: 10, heartbeat: 2.5, budget: 64, downlink: 100, feed: true } };
const MAX_HEARTBEATS = 4095;
const SIXTEEN_BIT_KEY = 'stock151.board16bitHeartbeats';
function remember(key, value) { try { localStorage.setItem(key, value ? '1' : ''); } catch { /* private window: forget */ } }
function recall(key) { try { return localStorage.getItem(key) === '1'; } catch { return false; } }
const TAGGED = new Set(['cycle', 'fpga']);
const STRATEGIES = [['sniper', 'Sniper'], ['maker', 'Market maker'], ['momentum', 'Momentum'],
  ['mean_reversion', 'Mean reversion'], ['fpga', 'FPGA strategy'], ['institution', 'Institution'],
  ['informed_institution', 'Informed institution']];
const MAX_BOTS = 94, STEP_NS = 100;
let presetMode = true;
let profileReady = false;
let bots = [];
const PARAMS = { maker: ['Half-spread (¢ at 0.4 s)', '40'], momentum: ['Move to trade on (¢ at 0.4 s)', '1'],
  mean_reversion: ['Move to trade on (¢ at 0.4 s)', 'σ'], institution: ['Shares', '200'], informed_institution: ['Shares', '200'] };
let presets = [];
let botControls = [];
const MAX_BOARDS = 30;
let boards = [];
let boardControls = [];
let botsSynced = false;

async function api(path, options) {
  const response = await fetch(path.replace(/^\/+/, ''), options);
  const body = await response.text();
  let state;
  try { state = JSON.parse(body); }
  catch {
    if (response.status === 403) {
      throw new Error('Access denied (HTTP 403). The preview or server returned Forbidden instead of exchange data. Open http://127.0.0.1:8765 in Chrome or Edge.');
    }
    throw new Error(`The server returned ${response.ok ? 'non-JSON data' : `HTTP ${response.status}`} instead of exchange data. Open http://127.0.0.1:8765 from the running simulator server.`);
  }
  if (!response.ok) throw new Error(state?.error || `The exchange returned HTTP ${response.status}.`);
  return state;
}

function showState(state) {
  marketRound = state;
  prices = state.prices;
  running = state.running;
  const seconds = Math.ceil(Math.max(0, DURATION_MS - state.elapsedMs) / 1000);
  $('timer').textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  $('status').textContent = state.complete ? 'Round complete' : running ? 'Live' : state.stopped ? 'Round stopped' : 'Ready';
  $('status').classList.toggle('live', running);
  $('start').textContent = running ? 'Round in progress' : state.complete || state.stopped ? 'New round' : 'Start round';
  for (const id of CONTROLS) $(id).disabled = running;
  $('start').disabled = running || !profileReady;
  $('stop').disabled = !running;
  if (!botsSynced && (!presetMode || state.settings?.scenario)) {
    botsSynced = true;
    if (state.settings?.bots) {
      bots = state.settings.bots.map(b => ({ strategy: b.strategy, latencyNs: b.latency_ns, param: b.param || null }));
      renderBots();
    }
    if (state.settings?.boards) {
      boards = state.settings.boards.map(b => ({ ...b }));
      renderBoards();
    }
  }
  lockBots(running);
  lockBoards(running);
  renderParticipants(state.participants);
  $('golden-status').textContent = participantStatus(state);
  $('latency-status').textContent = [lagStatus(state.lagMs), realtimeStatus(state.realtime), boardModeStatus(state),
    adaptiveLinkStatus(state.adaptiveLink), latencyStatus(state.latency, state.settings?.model_mode === 'fpga')].filter(Boolean).join('\n');
  showMeasuredLink(state);
  renderQuote();
  renderBook();
}

function participantStatus(state) {
  const board = state.settings?.model_mode === 'fpga' ? state.board : null;
  if (board && !board.connected) return `Board disconnected at ${(board.disconnectedMs / 1000).toFixed(1)} s · its orders were canceled; it no longer trades`;
  if (state.golden.halted) return `Halted (reason ${state.golden.haltReason})`;
  if (board) return `Board answering · ${board.answered} frames · heartbeat ${state.golden.heartbeat} / ${heartbeatTotal(state.settings)}`;
  return `Connected · heartbeat ${state.golden.heartbeat} / ${heartbeatTotal(state.settings)}`;
}

const priceMsOf = settings => settings?.price_interval_ms ?? STANDARD.price;
const heartbeatTotal = settings => Math.round(DURATION_MS / (settings?.heartbeat_interval_ms ?? STANDARD.heartbeat)) + 1;
const updatesTotal = settings => Math.floor(DURATION_MS / priceMsOf(settings) + 1e-9);
const autoCustomerMs = priceMs => priceMs * Math.max(1, Math.round(Math.sqrt(STANDARD.price / priceMs)));
const formatMs = ms => ms >= 1000 ? `${+(ms / 1000).toFixed(3)} s` : `${+ms.toFixed(3)} ms`;
function numberOr(id, fallback) {
  const v = $(id).value;
  return v === '' || v === undefined || v === null ? fallback : Number(v);
}

function paceHint() {
  const price = numberOr('price-interval', STANDARD.price), heartbeat = numberOr('heartbeat-interval', STANDARD.heartbeat);
  const customers = numberOr('customer-interval', autoCustomerMs(price));
  $('sigma-note').textContent = `${+(1000 / price).toFixed(2)} updates / second · 60 second round`;
  const heartbeats = DURATION_MS / heartbeat + 1, divides = Math.abs(heartbeats - Math.round(heartbeats)) < 1e-9;
  const parts = [`${Math.floor(DURATION_MS / price + 1e-9)} price updates`, `slower traders every ${formatMs(customers)}`,
    divides ? `${Math.round(heartbeats)} heartbeats` : 'the heartbeat interval must divide 60 s'];
  const tooMany = divides && heartbeats > MAX_HEARTBEATS && !$('board-16bit').checked;
  if (tooMany) parts.push(`over ${MAX_HEARTBEATS} heartbeats needs a 16-bit heartbeat count on every real board (docs/handoff-heartbeat-width.md in the model repository; tick "Boards count 16-bit heartbeats") and not a legacy strategy`);
  $('pace-hint').textContent = parts.join(' · ');
  $('pace-hint').classList.toggle('warn', tooMany || !divides);
}

function loadPace(name) {
  const pace = PACES[name];
  if (!pace) return;
  $('price-interval').value = String(pace.price); $('heartbeat-interval').value = String(pace.heartbeat);
  $('customer-interval').value = ''; $('order-budget').value = String(pace.budget); $('downlink').value = String(pace.downlink);
  $('feed-queue').checked = pace.feed;
  paceHint();
}

function paceSettings() {
  const out = {}, price = numberOr('price-interval', STANDARD.price), heartbeat = numberOr('heartbeat-interval', STANDARD.heartbeat);
  const budget = numberOr('order-budget', STANDARD.budget), downlink = numberOr('downlink', STANDARD.downlink);
  const latency = numberOr('link-latency', STANDARD.latency);
  if (price !== STANDARD.price) out.price_interval_ms = price;
  if ($('customer-interval').value !== '' && $('customer-interval').value !== undefined) out.customer_interval_ms = Number($('customer-interval').value);
  if (heartbeat !== STANDARD.heartbeat) out.heartbeat_interval_ms = heartbeat;
  if (budget !== STANDARD.budget) out.order_budget = budget;
  if (downlink !== STANDARD.downlink) out.downlink_pct = downlink;
  if (latency !== STANDARD.latency) out.propagation_ns = Math.round(latency * 1000);
  if ($('feed-queue').checked) out.feed_queue = true;
  if ($('board-16bit').checked) out.board_heartbeats_16bit = true;
  if ($('fair-snipers').checked) out.fair_snipers = true;
  if ($('run-mode').value === 'realtime') out.realtime_window_us = $('window').value === '' ? 'inf' : Number($('window').value);
  return out;
}
const realtimeAllowed = () => $('model-mode').value === 'fpga' || boards.length > 0;

function formatLatency(ns) { return ns < 1000 ? `${ns} ns` : `${(ns / 1000).toFixed(3)} µs`; }
const latencyOf = bot => bot.latencyNs ?? null;

function botRow(bot, index) {
  const control = (tag, label, className = '') => {
    const e = document.createElement(tag);
    e.className = className;
    e.setAttribute('aria-label', label);
    return e;
  };
  const row = document.createElement('div');
  row.className = 'bot-row';
  const strategy = control('select', `Bot ${index + 1} strategy`);
  for (const [value, label] of STRATEGIES) {
    const option = document.createElement('option');
    option.value = value; option.textContent = label;
    strategy.append(option);
  }
  strategy.value = bot.strategy;
  const param = control('input', '', 'bot-param');
  Object.assign(param, { type: 'number', min: '1', max: '100000', step: '1' });
  const showParam = () => {
    const meaning = PARAMS[bot.strategy];
    param.hidden = !meaning;
    param.setAttribute('aria-label', `Bot ${index + 1} ${meaning ? meaning[0].toLowerCase() : 'setting'}`);
    param.placeholder = meaning ? `${meaning[0]}: ${meaning[1]}` : '';
    param.value = bot.param ? String(bot.param) : '';
  };
  param.addEventListener('change', () => {
    if (param.value === '') bot.param = null;
    else if (param.checkValidity()) bot.param = Number(param.value);
    else { param.reportValidity(); return; }
  });
  strategy.addEventListener('change', () => { bot.strategy = strategy.value; bot.param = null; showParam(); });
  const latency = control('input', `Bot ${index + 1} latency (µs)`);
  Object.assign(latency, { type: 'number', min: '0', max: '100000', step: '0.001', placeholder: 'default' });
  const delta = control('span', `Bot ${index + 1} latency`, 'delta');
  const show = () => {
    const ns = latencyOf(bot);
    latency.value = ns === null ? '' : String(ns / 1000);
  };
  latency.addEventListener('change', () => {
    if (latency.value === '') bot.latencyNs = null;
    else if (latency.checkValidity()) bot.latencyNs = Math.round(Number(latency.value) * 1000);
    else { latency.reportValidity(); return; }
    show();
  });
  const step = sign => {
    const button = control('button', `${sign < 0 ? 'Decrease' : 'Increase'} bot ${index + 1}'s latency by ${STEP_NS} ns`, 'secondary step');
    button.type = 'button';
    button.textContent = sign < 0 ? '−' : '+';
    button.addEventListener('click', () => {
      const ns = latencyOf(bot);
      if (ns !== null) { bot.latencyNs = Math.max(0, ns + sign * STEP_NS); show(); }
    });
    return button;
  };
  const slower = step(1), faster = step(-1);
  const remove = control('button', `Remove bot ${index + 1}`, 'secondary step');
  remove.type = 'button';
  remove.textContent = '×';
  remove.addEventListener('click', () => { bots.splice(bots.indexOf(bot), 1); renderBots(); });
  show();
  showParam();
  row.append(strategy, faster, latency, slower, delta, param, remove);
  return { row, controls: [strategy, faster, latency, slower, param, remove] };
}

function renderBots() {
  const rows = bots.map(botRow);
  botControls = rows.flatMap(r => r.controls);
  const none = document.createElement('p');
  none.className = 'muted';
  none.textContent = 'No bots: FPGA board trades against the lazy maker and the customers only.';
  $('bots').replaceChildren(...(rows.length ? rows.map(r => r.row) : [none]));
  lockBots(running);
}

function lockBots(locked) {
  for (const c of botControls) c.disabled = locked || presetMode;
  $('scenario').disabled = locked;
  $('add-bot').disabled = locked || presetMode || bots.length >= MAX_BOTS;
}

function botSettings() {
  return bots.map(b => {
    const out = { strategy: b.strategy };
    if (latencyOf(b) !== null) out.latency_ns = latencyOf(b);
    if (b.param && PARAMS[b.strategy]) out.param = b.param;
    return out;
  });
}

function loadPreset(name) {
  const preset = presets.find(p => p.name === name);
  if (!preset) return;
  bots = preset.bots.map(b => ({ strategy: b.strategy, latencyNs: b.latency_ns, param: b.param ?? null }));
  $('sigma').value = String(preset.sigma);
  $('scenario-description').textContent = presetMode ? '' : preset.description || '';
  renderBots();
}

function boardRow(board, index) {
  const input = (label, className, value, set) => {
    const e = document.createElement('input');
    e.className = className;
    e.setAttribute('aria-label', `Board ${index + 1} ${label}`);
    e.value = value;
    e.addEventListener('change', () => set(e.value));
    return e;
  };
  const row = document.createElement('div');
  row.className = 'bot-row';
  const name = input('name', 'board-name', board.name, v => { board.name = v; });
  name.placeholder = 'Team name';
  const pid = input('pid', 'board-pid', String(board.pid), v => { board.pid = Number(v); });
  Object.assign(pid, { type: 'number', min: '1', max: '31', step: '1' });
  const link = input('link', 'board-address', board.link, v => { board.link = v; });
  link.placeholder = 'pcap:en9 or udp:127.0.0.1:9152';
  const remove = document.createElement('button');
  remove.className = 'secondary step';
  remove.type = 'button';
  remove.textContent = '×';
  remove.setAttribute('aria-label', `Remove board ${index + 1}`);
  remove.addEventListener('click', () => { boards.splice(boards.indexOf(board), 1); renderBoards(); });
  row.append(name, pid, link, remove);
  return { row, controls: [name, pid, link, remove] };
}

function renderBoards() {
  const rows = boards.map(boardRow);
  boardControls = rows.flatMap(r => r.controls);
  const none = document.createElement('p');
  none.className = 'muted';
  none.textContent = 'No other boards.';
  $('boards').replaceChildren(...(rows.length ? rows.map(r => r.row) : [none]));
  lockBoards(running);
}

function lockBoards(locked) {
  for (const c of boardControls) c.disabled = locked;
  $('add-board').disabled = locked || boards.length >= MAX_BOARDS;
}

function boardSettings() {
  return boards.map(b => ({ name: b.name.trim(), pid: Number(b.pid), link: b.link.trim() }));
}

function renderParticipants(participants) {
  const list = participants || [];
  const show = list.length > 1 || list.some(p => p.status?.latency);
  $('participants-wrap').hidden = !show;
  if (!show) return;
  $('participants').replaceChildren(...list.map(p => {
    const s = p.status || {};
    const state = !p.connected ? 'disconnected' : s.halted ? `halted (reason ${s.haltReason})` : 'answering';
    const median = x => x ? formatNs(x.median) : '—';
    const cells = [p.player, p.pid, state, p.answered, median(s.latency?.tagNs), median(s.latency?.observedNs)];
    const row = tableRow(cells, p.connected ? '' : 'sell-text');
    if (s.goneReason) row.setAttribute('title', s.goneReason);
    return row;
  }));
}

function lagStatus(lagMs) {
  if (!(lagMs >= 1000)) return '';
  return `Simulation running ${(lagMs / 1000).toFixed(1)} s behind the wall clock (results are unaffected)`;
}

function realtimeStatus(rt) {
  if (!rt) return '';
  const window = rt.windowNs < 0 ? '∞' : formatNs(rt.windowNs);
  return `Real time, window ${window}: ${rt.waits} waits for an answer (${formatNs(rt.waitedNs)}) · ${rt.late} late` +
    (rt.late ? ` (up to ${formatNs(rt.maxLateNs)})` : '') + ` · clock paused ${formatNs(rt.pausedNs)}`;
}

function boardModeStatus(state) {
  const mode = state.participants?.[0]?.status?.boardMode;
  if (!mode) return '';
  const free = 'the CPU runs freely';
  const line = `Board: ${mode.cpuWaitOff ? free : 'responses wait for the CPU'}` +
    ` · banks swap ${mode.swapOnCommit ? 'on commit' : 'at heartbeats'} (the round's setting: set SW[2] to match)`;
  return mode.cpuWaitOff && !state.realtime
    ? `${line}\nIn a lockstep round the board's CPU can fall behind, so rounds won't repeat. Use real time.`
    : line;
}

function formatNs(ns) {
  if (ns < 1000) return `${Math.round(ns)} ns`;
  if (ns < 1e6) return `${(ns / 1000).toFixed(ns < 1e5 ? 2 : 0)} µs`;
  return `${(ns / 1e6).toFixed(2)} ms`;
}

const EXCHANGE_LEAD_NS = 2 * 6880 + 1800, SUGGEST_STEP_US = 50;
let suggestedLinkUs = null;
function showMeasuredLink(state) {
  const trip = state.latency?.observedNs;
  if (!trip?.n) return;
  suggestedLinkUs = Math.max(SUGGEST_STEP_US, Math.ceil((trip.p99 - EXCHANGE_LEAD_NS) / 2000 / SUGGEST_STEP_US) * SUGGEST_STEP_US);
  const rt = state.realtime;
  const waits = !rt ? '' : ` · ${rt.waits} wait${rt.waits === 1 ? '' : 's'} for the board` + (!state.complete ? ''
    : rt.waits || rt.late ? ': raise the link latency for a round that repeats' : ': the same seed and link latency repeat this round');
  $('link-measured-value').textContent = `Your board last round: round trip ${formatNs(trip.median)} median, ${formatNs(trip.p99)} p99${waits}`;
  $('use-link-measured').textContent = `Use ${suggestedLinkUs} µs`;
  $('link-measured').hidden = false;
}
$('use-link-measured').addEventListener('click', () => {
  if (suggestedLinkUs !== null) $('link-latency').value = String(suggestedLinkUs);
});

function adaptiveLinkStatus(link) {
  if (!link || !link.samples) return '';
  return `Shared link propagation: ${formatNs(link.oneWayNs)} each way · range ${formatNs(link.minOneWayNs)}–${formatNs(link.maxOneWayNs)} · ${link.samples} FPGA replies`;
}

function latencyStatus(latency, board) {
  if (!latency) return '';
  const spread = s => s ? `median ${formatNs(s.median)} · p99 ${formatNs(s.p99)} · max ${formatNs(s.max)} (${s.n})` : 'no samples yet';
  const tagClock = latency.tagTickNs ? `sealed shell ticks (${latency.tagTickNs} ns each)` : `${latency.clockMhz} MHz`;
  const lines = [`FPGA tag latency (${tagClock}): ${spread(latency.tagNs)}; busy median ${latency.busyNs ? formatNs(latency.busyNs.median) : '—'}`];
  if (board) {
    lines.push(`Observed round trip, host to board and back: ${spread(latency.observedNs)}` +
      (latency.observedCaptureNs ? `; by capture timestamps median ${formatNs(latency.observedCaptureNs.median)}` : ''));
    lines.push(`Heartbeat frames (they also wait for the CPU's commit): ${spread(latency.heartbeatObservedNs)}`);
  }
  return lines.join('\n');
}

function showConnectionError(message) {
  running = false;
  $('error').textContent = message;
  $('status').textContent = 'Connection lost';
  $('status').classList.toggle('live', false);
  $('start').textContent = 'Restart round';
  for (const id of CONTROLS) $(id).disabled = false;
  $('start').disabled = !profileReady;
  $('stop').disabled = true;
  lockBots(false);
  lockBoards(false);
}

function draw() {
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width || !height) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const left = 10, right = width - 65, top = 20, bottom = height - 35;
  let min = Math.min(...prices), max = Math.max(...prices);
  const padding = Math.max(1, (max - min) * 0.18);
  min = Math.max(0, min - padding); max += padding;
  const times = priceTimes(), x = i => left + (times[i] / DURATION_MS) * (right - left), xt = ms => left + (ms / DURATION_MS) * (right - left);
  const y = p => bottom - ((p - min) / (max - min)) * (bottom - top);
  ctx.font = '10px Segoe UI, sans-serif';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const level = top + (bottom - top) * i / 4;
    ctx.strokeStyle = '#2b3831'; ctx.beginPath(); ctx.moveTo(left, level); ctx.lineTo(right, level); ctx.stroke();
    ctx.fillStyle = '#8b9d91'; ctx.textAlign = 'left'; ctx.fillText(usd.format(max - (max - min) * i / 4), right + 10, level + 3);
  }
  for (let second = 0; second <= 60; second += 15) {
    ctx.textAlign = second === 0 ? 'left' : second === 60 ? 'right' : 'center';
    ctx.fillText(`${second}s`, xt(second * 1000), height - 10);
  }
  ctx.setLineDash([4, 5]); ctx.strokeStyle = '#536d5e';
  ctx.beginPath(); ctx.moveTo(left, y(INITIAL_PRICE)); ctx.lineTo(right, y(INITIAL_PRICE)); ctx.stroke(); ctx.setLineDash([]);
  if (prices.length < 2) return;
  const gradient = ctx.createLinearGradient(0, top, 0, bottom);
  gradient.addColorStop(0, '#b5eccb28'); gradient.addColorStop(1, '#b5eccb00');
  ctx.beginPath(); prices.forEach((p, i) => i ? ctx.lineTo(x(i), y(p)) : ctx.moveTo(x(i), y(p)));
  ctx.lineTo(x(prices.length - 1), bottom); ctx.lineTo(left, bottom); ctx.closePath(); ctx.fillStyle = gradient; ctx.fill();
  ctx.beginPath(); prices.forEach((p, i) => i ? ctx.lineTo(x(i), y(p)) : ctx.moveTo(x(i), y(p)));
  ctx.strokeStyle = '#b5eccb'; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
  ctx.beginPath(); ctx.arc(x(prices.length - 1), y(prices.at(-1)), 4, 0, 2 * Math.PI); ctx.fillStyle = '#ceffdf'; ctx.fill();
}

function priceTimes() {
  const interval = priceMsOf(marketRound.settings), count = marketRound.priceStats?.count ?? prices.length;
  const step = count === prices.length ? 1 : Math.ceil(count / 600);
  return prices.map((_, j) => {
    const i = j === prices.length - 1 ? count - 1 : j * step;
    return i === 0 ? 0 : interval / 2 + (i - 1) * interval;
  });
}

function renderQuote() {
  const price = prices.at(-1), change = price - INITIAL_PRICE;
  $('price').textContent = usd.format(price);
  $('reference-price').textContent = usd.format(price);
  $('change').textContent = `${change < 0 ? '−' : '+'}${usd.format(Math.abs(change))} (${change < 0 ? '−' : '+'}${Math.abs(change / INITIAL_PRICE * 100).toFixed(2)}%)`;
  $('change').style.color = change < 0 ? '#f4aaa0' : '#b5eccb';
  const stats = marketRound.priceStats, updates = (stats?.count ?? prices.length) - 1;
  $('high').textContent = usd.format(stats?.high ?? Math.max(...prices));
  $('low').textContent = usd.format(stats?.low ?? Math.min(...prices));
  $('ticks').textContent = `${updates} / ${updatesTotal(marketRound.settings)}`;
  canvas.setAttribute('aria-label', `stock_151 price chart. ${updates} updates. Current price ${usd.format(price)}.`);
  draw();
}

$('start').addEventListener('click', async () => {
  for (const id of ['sigma', 'seed', 'processing', 'price-interval', 'customer-interval', 'heartbeat-interval',
    'order-budget', 'downlink', 'link-latency', 'window']) {
    if (!$(id).checkValidity()) { $(id).reportValidity(); return; }
  }
  if (!presetMode && $('run-mode').value === 'realtime' && !realtimeAllowed()) {
    $('error').textContent = 'Real time needs a real board: the FPGA board strategy, or other boards.';
    return;
  }
  requesting += 1;
  ++roundGeneration;
  $('start').disabled = true;
  const settings = presetMode
    ? { seed: Number($('seed').value), scenario: $('scenario').value,
        propagation_ns: Math.round(Number($('link-latency').value) * 1000) }
    : { sigma: Number($('sigma').value), seed: Number($('seed').value),
        processing_ns: Math.round(Number($('processing').value) * 1000), model_mode: $('model-mode').value,
        bots: botSettings() };
  if (!presetMode && boards.length) settings.boards = boardSettings();
  if (!presetMode) Object.assign(settings, paceSettings());
  if (presetMode || settings.model_mode === 'fpga') {
    const link = $('board-link').value.trim();
    settings.fpga_transport = link.startsWith('udp:') ? 'udp' : 'pcap';
    settings.fpga_address = link.replace(/^udp:/, '');
  }
  try {
    showState(await api('/api/start', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings) }));
    $('error').textContent = '';
  } catch (error) {
    $('error').textContent = error.message;
    $('start').disabled = false;
  } finally { requesting -= 1; }
});
$('stop').addEventListener('click', async () => {
  requesting += 1;
  ++roundGeneration;
  $('stop').disabled = true;
  try {
    showState(await api('/api/stop', { method: 'POST' }));
    $('error').textContent = '';
  } catch (error) {
    $('error').textContent = error.message;
  } finally { requesting -= 1; }
});
$('model-mode').addEventListener('change', () => {
  const tagged = TAGGED.has($('model-mode').value);
  if (tagged && $('processing').value === '1') $('processing').value = '1.8';
  if (!tagged && $('processing').value === '1.8') $('processing').value = '1';
  if (tagged && !bots.some(b => b.strategy === 'sniper')) {
    bots.unshift({ strategy: 'sniper', latencyNs: null, param: null });
    renderBots();
  }
});
$('scenario').addEventListener('change', () => loadPreset($('scenario').value));
$('pace').addEventListener('change', () => loadPace($('pace').value));
$('board-16bit').checked = recall(SIXTEEN_BIT_KEY);
$('board-16bit').addEventListener('change', () => { remember(SIXTEEN_BIT_KEY, $('board-16bit').checked); paceHint(); });
for (const id of ['price-interval', 'customer-interval', 'heartbeat-interval', 'order-budget', 'downlink', 'feed-queue']) {
  $(id).addEventListener('change', () => { $('pace').value = 'custom'; paceHint(); });
}
$('add-bot').addEventListener('click', () => {
  bots.push({ strategy: bots.at(-1)?.strategy ?? 'sniper', latencyNs: null, param: null });
  renderBots();
});
$('add-board').addEventListener('click', () => {
  const taken = new Set([3, ...boards.map(b => Number(b.pid))]);
  const pid = [...Array(31).keys()].map(i => i + 1).find(p => !taken.has(p)) ?? 1;
  boards.push({ name: `Board ${pid}`, pid, link: boards.at(-1)?.link ?? '' });
  renderBoards();
});
renderBots();
renderBoards();
paceHint();
Promise.all([api('/api/config'), api('/api/scenarios')]).then(([config, list]) => {
  presetMode = config.role === 'preset';
  document.body.classList.toggle('preset-mode', presetMode);
  $('market-profile-note').hidden = !presetMode;
  $('pace-hint').hidden = presetMode;
  if (presetMode) {
    $('model-mode').value = 'fpga';
    $('run-mode').value = 'realtime';
    $('feed-queue').checked = $('board-16bit').checked = $('fair-snipers').checked = true;
    $('price-interval').value = '20'; $('heartbeat-interval').value = '10';
    $('order-budget').value = '64'; $('downlink').value = '100'; $('link-latency').value = '500';
  } else {
    $('price-interval').value = '400'; $('heartbeat-interval').value = '100';
    $('order-budget').value = '64'; $('downlink').value = '30'; $('link-latency').value = '1';
    $('feed-queue').checked = false; $('board-16bit').checked = recall(SIXTEEN_BIT_KEY);
    $('fair-snipers').checked = true; $('run-mode').value = 'lockstep';
  }
  if (!Array.isArray(list)) return;
  presets = list;
  for (const p of list) {
    const option = document.createElement('option');
    option.value = p.name; option.textContent = `${p.title} (σ ${p.sigma}, ${p.bots.length} bots)`;
    $('scenario').append(option);
  }
  if (presetMode && presets.length) {
    const initialPreset = presets.find(p => p.name === 'busy') || presets[0];
    $('scenario').value = initialPreset.name;
    loadPreset(initialPreset.name);
  }
  profileReady = true;
  $('start').disabled = running;
  renderBots();
  lockBots(running);
}).catch(() => {});
new ResizeObserver(draw).observe(canvas.parentElement);
renderQuote();

async function poll() {
  if (!requesting) {
    const generation = roundGeneration;
    requesting += 1;
    try {
      const state = await api('/api/state');
      if (generation === roundGeneration) {
        showState(state);
        $('error').textContent = '';
      }
    } catch (error) {
      if (generation === roundGeneration) showConnectionError(error instanceof TypeError
        ? 'Cannot connect to the exchange. Run python -B backend/server.py and open http://127.0.0.1:8765 in your browser.'
        : error.message);
    } finally { requesting -= 1; }
  }
  setTimeout(poll, 100);
}
setTimeout(poll, 0);
