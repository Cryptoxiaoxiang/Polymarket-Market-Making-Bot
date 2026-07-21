const byId = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));

let expiryFormDirty = false;
let accountFormDirty = false;

function showView(name) {
  document.querySelectorAll('.view').forEach((view) => view.classList.toggle('active', view.id === name));
  document.querySelectorAll('.nav button[data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === name));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showNotice(message, isError = false) {
  const notice = byId('notice');
  notice.textContent = message;
  notice.className = `notice${isError ? ' error' : ''}`;
  notice.hidden = false;
  window.clearTimeout(showNotice.timeout);
  showNotice.timeout = window.setTimeout(() => { notice.hidden = true; }, 5000);
}

function formatDuration(total) {
  const seconds = Math.max(0, Number(total) || 0);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = Math.floor(seconds % 60);
  return `${hours}小时 ${minutes}分 ${remainder}秒`;
}

function syncExpiryControls() {
  const enabled = byId('expiry-enabled').checked;
  byId('expiry-hours').disabled = !enabled;
  byId('expiry-minutes').disabled = !enabled;
}

function marketCell(label, detail = '') {
  const rawTitle = String(label || '未命名市场');
  const short = escapeHtml(rawTitle.slice(0, 2).toUpperCase());
  return `<div class="market-cell"><span class="token">${short}</span><span><strong>${escapeHtml(rawTitle)}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ''}</span></div>`;
}

function render(status) {
  const account = status.account || {};
  const configuration = status.configuration || {};
  const preflight = status.preflight;
  const running = Boolean(status.running);
  const marketCount = Number(configuration.market_count ?? status.markets.length ?? 0);

  const phaseLabels = {
    running: '运行中', starting: '启动检查中', stopping: '停止中', stopped: '已停止', error: '运行错误', created: '待启动',
  };
  const phaseLabel = phaseLabels[status.phase] || String(status.phase || '未知');
  byId('run-status').textContent = phaseLabel;
  byId('run-status').className = `metric-value ${status.phase === 'running' ? 'positive' : status.phase === 'error' ? 'negative' : 'warning'}`;
  byId('run-status-note').textContent = running ? (status.paused ? '任务已暂停，不会创建新挂单' : '机器人正在管理报价') : (status.last_error || '网页服务正常，可从页面启动任务');
  byId('mode-badge').textContent = status.dry_run ? '模拟模式' : '实盘模式';
  byId('mode-badge').className = `pill${status.dry_run ? '' : ' live'}`;

  byId('open-order-value').textContent = String(status.orders.length);
  byId('open-order-note').textContent = status.orders.length ? '机器人正在管理开放订单' : '暂无机器人管理的挂单';
  byId('open-orders-summary').textContent = status.orders.length ? `${status.orders.length} 笔开放订单` : '暂无挂单';
  byId('market-value').textContent = `${marketCount} 个市场`;
  byId('configured-note').textContent = status.dry_run ? '当前为模拟模式' : '当前为实盘模式';
  byId('market-count').textContent = String(marketCount);

  byId('balance-value').textContent = preflight ? `${preflight.collateral_balance} pUSD` : '尚未预检';
  byId('balance-note').textContent = preflight ? `最小 allowance ${preflight.minimum_allowance}` : '预检后显示可用抵押余额';
  byId('account-state').textContent = account.ready ? '账户已连接' : '配置不完整';
  byId('ws-state').textContent = status.websocket_connected ? '已连接' : (status.dry_run ? '模拟模式未启用' : '未连接');
  byId('fill-guard').textContent = configuration.halt_on_fill ? '已启用' : '未启用';
  byId('guard-badge').innerHTML = `<span class="dot"></span>${account.ready ? '保护已加载' : '等待账户配置'}`;
  byId('guard-badge').className = `status-badge${account.ready ? '' : ' warning'}`;

  const preflightText = preflight
    ? `Signer ${preflight.signer_address} · pUSD ${preflight.collateral_balance} · 最小 allowance ${preflight.minimum_allowance} · ${preflight.country || '—'}/${preflight.region || '—'}`
    : '尚未运行预检。保存账户后，可在账户设置页执行不会下单的实盘检查。';
  byId('preflight-summary').textContent = preflightText;
  byId('preflight-detail').textContent = preflightText;
  byId('preflight-status').textContent = preflight ? '预检已通过' : '尚未检查';

  const labelsByToken = new Map(status.markets.map((market) => [market.token_id, market.label]));
  byId('open-orders-list').innerHTML = status.orders.length
    ? status.orders.map((order) => `<tr><td>${marketCell(labelsByToken.get(order.token_id) || order.token_id)}</td><td><span class="side-badge">${escapeHtml(order.side)}</span></td><td class="order-price">${escapeHtml(order.price)}</td><td>${escapeHtml(order.size)}</td><td>${escapeHtml(order.age_seconds)} 秒</td></tr>`).join('')
    : '<tr class="empty-row"><td colspan="5">机器人当前没有管理中的挂单。</td></tr>';

  byId('markets-list').innerHTML = status.markets.length
    ? status.markets.map((market) => `<tr><td>${marketCell(market.label, market.token_id || '')}</td><td>${escapeHtml(market.position)}</td><td>${escapeHtml(market.book.best_bid || '—')}</td><td>${escapeHtml(market.book.best_ask || '—')}</td><td>${escapeHtml(market.book.spread || '—')}</td><td><span class="side-badge${market.halted ? ' warning' : ''}">${market.halted ? '已停止' : '正常'}</span></td></tr>`).join('')
    : '<tr class="empty-row"><td colspan="6">当前没有已配置市场。</td></tr>';
  byId('markets-summary').textContent = `${status.markets.length} 个 outcome`;

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

  const canStart = !running && (status.dry_run || account.ready);
  byId('start-button').disabled = !canStart;
  byId('stop-button').disabled = !running;
  byId('pause-button').disabled = !running;
  byId('resume-button').disabled = !running;
  byId('apply-expiry').disabled = !running;
  byId('save-account').disabled = running;
  byId('run-preflight').disabled = running || !account.private_key_set;

  const quoteTask = status.quote_task || {};
  if (quoteTask.expired) byId('expiry-state').textContent = '已到期 · 挂单已停止';
  else if (quoteTask.deadline_at) byId('expiry-state').textContent = `剩余 ${formatDuration(quoteTask.remaining_seconds)} · ${new Date(quoteTask.deadline_at * 1000).toLocaleString()}`;
  else byId('expiry-state').textContent = '不限时';
  if (!expiryFormDirty) {
    byId('expiry-enabled').checked = Boolean(quoteTask.deadline_at);
    if (quoteTask.deadline_at && !quoteTask.expired) {
      const totalMinutes = Math.max(1, Math.ceil(Number(quoteTask.remaining_seconds || 0) / 60));
      byId('expiry-hours').value = String(Math.floor(totalMinutes / 60));
      byId('expiry-minutes').value = String(totalMinutes % 60);
    }
    syncExpiryControls();
  }
  byId('task-mode').textContent = status.dry_run ? '模拟模式' : '实盘模式';
  byId('max-order-size').textContent = configuration.max_order_size ?? '—';
  byId('max-position').textContent = configuration.max_position_per_token ?? '—';
  byId('max-notional').textContent = configuration.max_total_open_notional ?? '—';
  byId('cancel-seconds').textContent = configuration.cancel_after_seconds ?? '—';

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

async function action(path, { body, confirmText } = {}) {
  if (confirmText && !window.confirm(confirmText)) return false;
  document.querySelectorAll('button').forEach((button) => { button.disabled = true; });
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
    await refresh();
  }
}

document.querySelectorAll('.nav button[data-view]').forEach((button) => { button.addEventListener('click', () => showView(button.dataset.view)); });
document.querySelectorAll('[data-view-target]').forEach((button) => { button.addEventListener('click', () => showView(button.dataset.viewTarget)); });
byId('start-button').addEventListener('click', () => action('/api/start', { confirmText: '确认启动挂单任务？实盘预检通过后会提交真实订单。' }));
byId('stop-button').addEventListener('click', () => action('/api/stop'));
byId('pause-button').addEventListener('click', () => action('/api/pause'));
byId('resume-button').addEventListener('click', () => action('/api/resume'));
byId('run-preflight').addEventListener('click', () => action('/api/preflight'));

byId('account-form').addEventListener('input', () => { accountFormDirty = true; });
byId('account-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const saved = await action('/api/account', { body: {
    private_key: byId('private-key').value,
    signature_type: Number(byId('signature-type').value),
    funder_address: byId('funder-address').value,
  } });
  byId('private-key').value = '';
  if (saved) accountFormDirty = false;
});

byId('expiry-hours').innerHTML = Array.from({ length: 169 }, (_, index) => `<option value="${index}">${index}</option>`).join('');
byId('expiry-hours').value = '1';
byId('expiry-minutes').innerHTML = Array.from({ length: 60 }, (_, index) => `<option value="${index}">${index}</option>`).join('');
byId('expiry-enabled').addEventListener('change', () => { expiryFormDirty = true; syncExpiryControls(); });
byId('expiry-hours').addEventListener('change', () => { expiryFormDirty = true; });
byId('expiry-minutes').addEventListener('change', () => { expiryFormDirty = true; });
byId('apply-expiry').addEventListener('click', async () => {
  expiryFormDirty = false;
  if (byId('expiry-enabled').checked) {
    await action('/api/expiry', { body: { hours: Number(byId('expiry-hours').value), minutes: Number(byId('expiry-minutes').value) } });
  } else {
    await action('/api/expiry/clear');
  }
});

syncExpiryControls();
refresh();
window.setInterval(refresh, 2000);
