const byId = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));
const numberValue = (value) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

function formatCountdown(seconds) {
  const remaining = Math.max(0, Math.floor(Number(seconds) || 0));
  const days = Math.floor(remaining / 86400);
  const hours = Math.floor((remaining % 86400) / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  const secs = remaining % 60;
  const clock = [hours, minutes, secs].map((value) => String(value).padStart(2, '0')).join(':');
  return days ? `${days}天 ${clock}` : clock;
}

function formatConfiguredDuration(seconds) {
  const totalMinutes = Math.max(0, Math.floor(Number(seconds) / 60));
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  return [days && `${days}天`, hours && `${hours}小时`, minutes && `${minutes}分钟`].filter(Boolean).join(' ') || '0分钟';
}

let latestStatus = null;

function updateExpiryMetric(status = latestStatus) {
  if (!status) return;
  const configuredSeconds = Number(status.configuration?.run_duration_seconds || 0);
  const quoteTask = status.quote_task || {};
  const value = byId('expiry-countdown-value');
  const note = byId('expiry-countdown-note');
  if (configuredSeconds <= 0) {
    value.textContent = '无限期';
    note.textContent = '当前未设置任务有效期';
    return;
  }
  if (quoteTask.expired) {
    value.textContent = '已到期';
    note.textContent = `本次有效期 ${formatConfiguredDuration(configuredSeconds)}`;
    return;
  }
  if (!status.running || !quoteTask.deadline_at) {
    value.textContent = '待启动';
    note.textContent = `启动后倒计时 ${formatConfiguredDuration(configuredSeconds)}`;
    return;
  }
  const remaining = Math.max(0, Number(quoteTask.deadline_at) - Date.now() / 1000);
  value.textContent = formatCountdown(remaining);
  note.textContent = status.paused ? '任务已暂停，倒计时继续' : `本次有效期 ${formatConfiguredDuration(configuredSeconds)}`;
}

function setTaskState(id, text, tone = '') {
  const node = byId(id);
  node.textContent = text;
  node.className = tone;
}

const setupForm = byId('setup-form');
const setupMarketsList = byId('setup-markets-list');
const marketTemplate = byId('market-template');
const runDurationEnabled = setupForm.elements.namedItem('run_duration_enabled');
const runDurationHours = setupForm.elements.namedItem('run_duration_hours');
const runDurationMinutes = setupForm.elements.namedItem('run_duration_minutes');
let setupFormDirty = false;
let accountFormDirty = false;
let actionInFlight = false;
const logPanels = [...document.querySelectorAll('.log-preview, .full-log')];
const logRefreshStatus = byId('log-refresh-status');
const logRefreshStatusText = byId('log-refresh-status-text');
let logInteractionPaused = false;
let logRefreshPaused = false;
let logRefreshPending = false;
let logRequestInFlight = false;

function showView(name) {
  document.querySelectorAll('.view').forEach((view) => view.classList.toggle('active', view.id === name));
  document.querySelectorAll('.nav button[data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === name));
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (name === 'logs-view') refreshLogs({ force: true });
}

function showNotice(message, isError = false) {
  const notice = byId('notice');
  notice.textContent = message;
  notice.className = `notice${isError ? ' error' : ''}`;
  notice.hidden = false;
  window.clearTimeout(showNotice.timeout);
  showNotice.timeout = window.setTimeout(() => { notice.hidden = true; }, 5000);
}

function syncDurationControls() {
  const enabled = runDurationEnabled.checked;
  runDurationHours.disabled = !enabled;
  runDurationMinutes.disabled = !enabled;
}

function selectedMarketText(row) {
  const selected = row.querySelector('[data-field="selected_market"]');
  const label = row.querySelector('[data-field="label"]').value;
  const outcome = row.querySelector('[data-field="outcome"]').value;
  selected.textContent = label && outcome ? `已选择：${label}` : '';
  selected.hidden = !selected.textContent;
}

function applyMarketChoice(row, market, selectedOutcome) {
  const outcomeSelect = row.querySelector('[data-field="outcome"]');
  outcomeSelect.innerHTML = market.outcomes.map((outcome) => `<option value="${escapeHtml(outcome.name)}">${escapeHtml(outcome.name)}</option>`).join('');
  outcomeSelect.disabled = false;
  outcomeSelect.value = selectedOutcome.name;
  row.querySelector('[data-field="market_slug"]').value = market.market_slug;
  row.querySelector('[data-field="condition_id"]').value = market.condition_id;
  row.querySelector('[data-field="token_id"]').value = selectedOutcome.token_id;
  row.querySelector('[data-field="label"]').value = `${market.question} — ${selectedOutcome.name}`;
  selectedMarketText(row);
}

function addSetupMarket(market = {}) {
  const empty = setupMarketsList.querySelector('.empty-setup');
  if (empty) empty.remove();
  const row = marketTemplate.content.firstElementChild.cloneNode(true);
  row.querySelector('[data-field="url"]').value = market.url || '';
  row.querySelector('[data-field="quote_size"]').value = market.quote_size || '';
  ['market_slug', 'token_id', 'condition_id', 'label'].forEach((field) => {
    row.querySelector(`[data-field="${field}"]`).value = market[field] || '';
  });
  const outcome = row.querySelector('[data-field="outcome"]');
  if (market.outcome) {
    outcome.innerHTML = `<option value="${escapeHtml(market.outcome)}">${escapeHtml(market.outcome)}</option>`;
    outcome.disabled = false;
  }
  selectedMarketText(row);
  row.querySelector('.remove-market').addEventListener('click', () => {
    row.remove();
    setupFormDirty = true;
    updateConfiguredMarketSummary();
  });
  row.querySelector('.resolve-market').addEventListener('click', () => resolveMarketRow(row));
  row.querySelector('[data-field="url"]').addEventListener('input', () => {
    ['market_slug', 'token_id', 'condition_id', 'label'].forEach((field) => {
      row.querySelector(`[data-field="${field}"]`).value = '';
    });
    outcome.innerHTML = '<option value="">请重新识别网址</option>';
    outcome.disabled = true;
    selectedMarketText(row);
  });
  setupMarketsList.append(row);
  updateConfiguredMarketSummary();
}

function renderSetupMarkets(markets = [], saved = false) {
  setupMarketsList.replaceChildren();
  if (!markets.length) {
    setupMarketsList.innerHTML = '<p class="empty-setup">尚未添加市场。点击右上角“添加市场”后，粘贴 Polymarket 网址并识别。</p>';
  } else {
    markets.forEach(addSetupMarket);
  }
  if (saved && markets.length) byId('configured-market-summary').textContent = `已保存 ${markets.length} 个市场`;
  else updateConfiguredMarketSummary();
}

function updateConfiguredMarketSummary() {
  const count = setupMarketsList.querySelectorAll('.market-row').length;
  byId('configured-market-summary').textContent = count ? `${count} 个市场待保存` : '尚未配置市场';
}

function renderMarketChoices(row, result) {
  const container = row.querySelector('[data-field="market_lookup"]');
  container.replaceChildren();
  container.hidden = false;
  const message = document.createElement('p');
  message.textContent = result.message;
  container.append(message);
  result.markets.forEach((market) => {
    const group = document.createElement('div');
    group.className = 'market-match-group';
    const title = document.createElement('p');
    title.textContent = market.question;
    group.append(title);
    market.outcomes.forEach((outcome) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'market-match';
      button.textContent = `选择「${outcome.name}」挂单`;
      button.addEventListener('click', () => {
        applyMarketChoice(row, market, outcome);
        container.hidden = true;
        setupFormDirty = true;
      });
      group.append(button);
    });
    container.append(group);
  });
}

async function resolveMarketRow(row) {
  const url = row.querySelector('[data-field="url"]').value.trim();
  if (!/^https:\/\/(www\.)?polymarket\.com\/(event|market)\//i.test(url)) {
    showNotice('请粘贴完整的 Polymarket 市场网址。', true);
    return;
  }
  const button = row.querySelector('.resolve-market');
  button.disabled = true;
  button.textContent = '识别中…';
  try {
    const response = await fetch('/api/resolve-market', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'poly-mm-console'},
      body: JSON.stringify({url}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    renderMarketChoices(row, result);
  } catch (error) {
    showNotice(`市场识别失败：${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = '识别网址';
  }
}

function collectSetupMarkets() {
  return [...setupMarketsList.querySelectorAll('.market-row')].map((row) => ({
    url: row.querySelector('[data-field="url"]').value.trim(),
    outcome: row.querySelector('[data-field="outcome"]').value,
    market_slug: row.querySelector('[data-field="market_slug"]').value,
    quote_size: row.querySelector('[data-field="quote_size"]').value.trim(),
  }));
}

function marketCell(label, detail = '') {
  const rawTitle = String(label || '未命名市场');
  const short = escapeHtml(rawTitle.slice(0, 2).toUpperCase());
  return `<div class="market-cell"><span class="token">${short}</span><span><strong>${escapeHtml(rawTitle)}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ''}</span></div>`;
}

function render(status) {
  latestStatus = status;
  const account = status.account || {};
  const configuration = status.configuration || {};
  const preflight = status.preflight;
  const running = Boolean(status.running);
  const orders = Array.isArray(status.orders) ? status.orders : [];
  const markets = Array.isArray(status.markets) ? status.markets : [];
  const marketCount = Number(configuration.market_count ?? markets.length ?? 0);

  const phaseLabels = {
    running: '运行中', starting: '启动检查中', stopping: '停止中', stopped: '已停止', error: '运行错误', created: '待启动',
  };
  const phaseLabel = phaseLabels[status.phase] || String(status.phase || '未知');
  byId('sidebar-run-status').textContent = phaseLabel;
  byId('mode-badge').textContent = status.dry_run ? '模拟模式' : '实盘模式';
  byId('mode-badge').className = `mode-tag ${status.dry_run ? 'dry' : 'live'}`;
  byId('mode-badge').insertAdjacentHTML('afterbegin', '<span class="dot"></span>');

  const openNotional = orders.reduce((total, order) => {
    const remaining = Math.max(0, numberValue(order.size) - numberValue(order.filled_size));
    return total + numberValue(order.price) * remaining;
  }, 0);
  byId('open-order-value').textContent = String(orders.length);
  byId('open-order-note').textContent = orders.length ? '机器人正在管理开放订单' : '当前活跃挂单数量';
  const walletBalance = Number(preflight?.collateral_balance);
  byId('wallet-balance-value').textContent = Number.isFinite(walletBalance)
    ? walletBalance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 })
    : '—';
  byId('wallet-balance-note').textContent = Number.isFinite(walletBalance)
    ? 'Polymarket 返回的 pUSD 余额'
    : '预检或启动任务后更新';
  byId('open-notional-value').textContent = `$${openNotional.toFixed(2)}`;
  updateExpiryMetric(status);
  byId('open-orders-summary').textContent = orders.length ? `${orders.length} 笔开放订单` : '暂无挂单';
  byId('market-count').textContent = String(marketCount);

  const strategyConfigured = numberValue(configuration.cancel_after_seconds) > 0;
  const riskConfigured = numberValue(configuration.max_position_per_token) > 0
    && numberValue(configuration.max_total_open_shares) > 0;
  const accountReady = status.dry_run || Boolean(account.ready);
  const ready = accountReady && strategyConfigured && riskConfigured && marketCount > 0;
  setTaskState('task-signer-state', account.ready ? '已配置' : status.dry_run ? '模拟模式' : '未配置', accountReady ? 'positive' : 'warning');
  setTaskState('task-rpc-state', status.websocket_connected ? '已连接' : '未连接', status.websocket_connected ? 'positive' : '');
  setTaskState('task-strategy-state', strategyConfigured ? '已配置' : '未配置', strategyConfigured ? 'positive' : 'warning');
  setTaskState('task-risk-state', riskConfigured ? '已配置' : '未配置', riskConfigured ? 'positive' : 'warning');
  setTaskState('task-market-state', marketCount ? `${marketCount} 个市场` : '未配置', marketCount ? 'positive' : 'warning');
  setTaskState('task-ready-state', running ? phaseLabel : ready ? '可启动' : '未就绪', running || ready ? 'positive' : 'warning');
  byId('guard-badge').innerHTML = `<span class="dot"></span>${running ? phaseLabel : ready ? '已就绪' : '未就绪'}`;
  byId('guard-badge').className = `status-badge${running || ready ? '' : ' warning'}`;

  const preflightText = preflight
    ? `Signer ${preflight.signer_address} · pUSD ${preflight.collateral_balance} · 最小 allowance ${preflight.minimum_allowance} · ${preflight.country || '—'}/${preflight.region || '—'}`
    : '尚未运行预检。保存账户后，可在账户设置页执行不会下单的实盘检查。';
  byId('preflight-detail').textContent = preflightText;
  byId('preflight-status').textContent = preflight ? '预检已通过' : '尚未检查';

  const labelsByToken = new Map(markets.map((market) => [market.token_id, market.label]));
  byId('open-orders-list').innerHTML = orders.map((order) => `<tr><td>${marketCell(labelsByToken.get(order.token_id) || order.token_id)}</td><td><span class="side-badge">${escapeHtml(order.side)}</span></td><td class="order-price">${escapeHtml(order.price)}</td><td>${escapeHtml(order.size)}</td><td>${escapeHtml(order.age_seconds)} 秒</td></tr>`).join('');
  byId('open-orders-table').hidden = orders.length === 0;
  byId('open-orders-empty').hidden = orders.length > 0;

  byId('account-badge').textContent = account.ready ? '账户已连接' : '账户配置不完整';
  byId('account-badge').className = `pill${account.ready ? '' : ' live'}`;
  byId('secret-status').textContent = [account.private_key_set && '私钥已保存', account.api_credentials_set && 'L2 凭据已派生'].filter(Boolean).join(' · ') || '尚未保存实盘密钥';
  byId('connection-status').textContent = account.ready ? '账户已就绪' : (account.error || '配置不完整');
  byId('signer-address').textContent = account.signer_address || '尚未保存有效私钥';
  byId('funder-status').textContent = account.funder_address || '—';
  byId('credential-status').textContent = account.api_credentials_set ? '已派生并保存在服务器' : (account.api_credentials_partial ? '配置不完整' : '尚未派生');
  byId('sidebar-account').textContent = account.signer_address ? `${account.signer_address.slice(0, 8)}…${account.signer_address.slice(-4)}` : '账户尚未配置';
  if (!accountFormDirty) {
    byId('signature-type').value = String(account.signature_type ?? 0);
    byId('funder-address').value = account.funder_configured ? (account.funder_address || '') : '';
  }

  const canStart = !running && ready;
  byId('start-button').disabled = !canStart;
  byId('stop-button').disabled = !running;
  byId('pause-button').disabled = !running;
  byId('resume-button').disabled = !running;
  byId('save-account').disabled = running;
  byId('run-preflight').disabled = running || !account.private_key_set;

  if (!setupFormDirty) {
    setupForm.elements.namedItem('max_position_per_token').value = configuration.max_position_per_token ?? '';
    setupForm.elements.namedItem('max_total_open_shares').value = configuration.max_total_open_shares ?? '';
    setupForm.elements.namedItem('cancel_after_seconds').value = configuration.cancel_after_seconds ?? '';
    setupForm.elements.namedItem('sell_on_fill').checked = configuration.sell_on_fill !== false;
    setupForm.elements.namedItem('dry_run').checked = Boolean(status.dry_run);
    const durationMinutes = Math.floor(Number(configuration.run_duration_seconds || 0) / 60);
    runDurationEnabled.checked = durationMinutes > 0;
    runDurationHours.value = String(Math.floor(durationMinutes / 60));
    runDurationMinutes.value = String(durationMinutes % 60);
    syncDurationControls();
    renderSetupMarkets(configuration.markets || [], true);
  }

  if (status.last_error) showNotice(status.last_error, true);
}

async function refresh() {
  try {
    const response = await fetch('/api/status', { cache: 'no-store' });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    render(payload);
  } catch (error) {
    showNotice(`控制台连接失败：${error.message}`, true);
  }
}

function selectionIsInsideLogs() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return false;
  return logPanels.some((panel) => (
    (selection.anchorNode && panel.contains(selection.anchorNode))
    || (selection.focusNode && panel.contains(selection.focusNode))
  ));
}

function updateLogRefreshPauseState() {
  const paused = logInteractionPaused || selectionIsInsideLogs();
  if (paused === logRefreshPaused) return;
  logRefreshPaused = paused;
  logPanels.forEach((panel) => panel.classList.toggle('paused', paused));
  logRefreshStatus.classList.toggle('paused', paused);
  logRefreshStatusText.textContent = paused
    ? '已暂停（点击日志外恢复）'
    : '每 2 秒更新';
  if (!paused) refreshLogs({ force: true });
}

async function refreshLogs({ force = false } = {}) {
  if (!force && logRefreshPaused) {
    logRefreshPending = true;
    return;
  }
  if (logRequestInFlight) {
    logRefreshPending = true;
    return;
  }
  logRequestInFlight = true;
  try {
    const response = await fetch('/api/logs', { cache: 'no-store' });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    if (!force && logRefreshPaused) {
      logRefreshPending = true;
      return;
    }
    const lines = Array.isArray(payload.lines) ? payload.lines : [];
    const logs = byId('logs');
    logs.textContent = lines.length ? lines.join('\n') : '暂无运行日志。';
    logs.scrollTop = logs.scrollHeight;
    const preview = byId('dashboard-logs');
    preview.textContent = lines.length ? lines.slice(-8).join('\n') : '暂无活动记录\n系统活动将显示在此处。';
    preview.classList.toggle('empty', lines.length === 0);
    preview.scrollTop = preview.scrollHeight;
    logRefreshPending = false;
  } catch (error) {
    showNotice(`日志读取失败：${error.message}`, true);
  } finally {
    logRequestInFlight = false;
    if (!logRefreshPaused && logRefreshPending) {
      logRefreshPending = false;
      queueMicrotask(() => refreshLogs({ force: true }));
    }
  }
}

async function action(path, { body, confirmText, trigger } = {}) {
  if (confirmText && !window.confirm(confirmText)) return false;
  if (actionInFlight) return false;
  actionInFlight = true;
  if (trigger) trigger.disabled = true;
  try {
    const headers = { 'X-Requested-With': 'poly-mm-console' };
    if (body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { method: 'POST', headers, body: body ? JSON.stringify(body) : undefined });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    if (payload.status) render(payload.status);
    if (payload.message) showNotice(payload.message);
    return true;
  } catch (error) {
    showNotice(error.message, true);
    return false;
  } finally {
    actionInFlight = false;
    if (trigger) trigger.disabled = false;
    await refresh();
  }
}

document.querySelectorAll('.nav button[data-view]').forEach((button) => { button.addEventListener('click', () => showView(button.dataset.view)); });
document.querySelectorAll('[data-view-target]').forEach((button) => { button.addEventListener('click', () => showView(button.dataset.viewTarget)); });
logPanels.forEach((panel) => {
  panel.addEventListener('focus', () => {
    logInteractionPaused = true;
    updateLogRefreshPauseState();
  });
  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    window.getSelection()?.removeAllRanges();
    logInteractionPaused = false;
    panel.blur();
    updateLogRefreshPauseState();
  });
});
document.addEventListener('pointerdown', (event) => {
  if (event.target.closest?.('.log-preview, .full-log')) {
    logInteractionPaused = true;
    updateLogRefreshPauseState();
    return;
  }
  logInteractionPaused = false;
  requestAnimationFrame(updateLogRefreshPauseState);
});
document.addEventListener('selectionchange', updateLogRefreshPauseState);
byId('start-button').addEventListener('click', (event) => action('/api/start', { confirmText: '确认启动挂单任务？实盘预检通过后会提交真实订单。', trigger: event.currentTarget }));
byId('stop-button').addEventListener('click', (event) => action('/api/stop', { trigger: event.currentTarget }));
byId('pause-button').addEventListener('click', (event) => action('/api/pause', { trigger: event.currentTarget }));
byId('resume-button').addEventListener('click', (event) => action('/api/resume', { trigger: event.currentTarget }));
byId('run-preflight').addEventListener('click', (event) => action('/api/preflight', { trigger: event.currentTarget }));
byId('refresh-logs').addEventListener('click', () => refreshLogs({ force: true }));
byId('dashboard-add-market').addEventListener('click', () => {
  showView('tasks');
  addSetupMarket();
  setupFormDirty = true;
});

byId('account-form').addEventListener('input', () => { accountFormDirty = true; });
byId('account-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const saved = await action('/api/account', { body: {
    private_key: byId('private-key').value,
    signature_type: Number(byId('signature-type').value),
    funder_address: byId('funder-address').value,
  }, trigger: event.submitter });
  byId('private-key').value = '';
  if (saved) accountFormDirty = false;
});

runDurationHours.innerHTML = Array.from({ length: 169 }, (_, index) => `<option value="${index}">${index} 小时</option>`).join('');
runDurationMinutes.innerHTML = Array.from({ length: 60 }, (_, index) => `<option value="${index}">${index} 分钟</option>`).join('');
byId('add-market-button').addEventListener('click', () => {
  addSetupMarket();
  setupFormDirty = true;
});
setupForm.addEventListener('input', () => { setupFormDirty = true; });
setupForm.addEventListener('change', () => { setupFormDirty = true; });
runDurationEnabled.addEventListener('change', () => {
  if (runDurationEnabled.checked && runDurationHours.value === '0' && runDurationMinutes.value === '0') runDurationHours.value = '1';
  syncDurationControls();
});
setupForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const markets = collectSetupMarkets();
  if (markets.some((market) => !market.url || !market.outcome || !market.market_slug)) {
    showNotice('请先识别每个市场网址并选择要挂单的 outcome。', true);
    return;
  }
  const saved = await action('/api/setup', { body: {
    markets,
    max_position_per_token: setupForm.elements.namedItem('max_position_per_token').value,
    max_total_open_shares: setupForm.elements.namedItem('max_total_open_shares').value,
    cancel_after_seconds: setupForm.elements.namedItem('cancel_after_seconds').value,
    sell_on_fill: setupForm.elements.namedItem('sell_on_fill').checked,
    run_duration_enabled: runDurationEnabled.checked,
    run_duration_hours: Number(runDurationHours.value),
    run_duration_minutes: Number(runDurationMinutes.value),
    dry_run: setupForm.elements.namedItem('dry_run').checked,
  }, trigger: event.submitter });
  if (saved) setupFormDirty = false;
});
byId('clear-setup').addEventListener('click', async (event) => {
  if (!window.confirm('确认清空全部挂单市场？机器人将保持停止，且不会创建订单。')) return;
  const saved = await action('/api/setup', { body: {
    markets: [],
    max_position_per_token: setupForm.elements.namedItem('max_position_per_token').value,
    max_total_open_shares: setupForm.elements.namedItem('max_total_open_shares').value,
    cancel_after_seconds: setupForm.elements.namedItem('cancel_after_seconds').value,
    sell_on_fill: setupForm.elements.namedItem('sell_on_fill').checked,
    run_duration_enabled: false,
    run_duration_hours: 0,
    run_duration_minutes: 0,
    dry_run: setupForm.elements.namedItem('dry_run').checked,
  }, trigger: event.currentTarget });
  if (saved) setupFormDirty = false;
});

syncDurationControls();
refresh();
refreshLogs();
window.setInterval(() => { refresh(); refreshLogs(); }, 2000);
window.setInterval(() => { updateExpiryMetric(); }, 1000);
