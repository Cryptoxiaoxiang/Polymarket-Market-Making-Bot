from __future__ import annotations

import asyncio
import json
import logging
import threading
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Coroutine
from urllib.parse import urlsplit

logger = logging.getLogger("poly-mm")


class _ConsoleHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class ConsoleServer:
    """Loopback-only operator console with same-origin control actions."""

    def __init__(
        self,
        controller: object,
        *,
        host: str,
        port: int,
        enabled: bool,
    ) -> None:
        self.controller = controller
        self.host = host
        self.port = port
        self.enabled = enabled
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server: _ConsoleHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        if self._server is None:
            return None
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self.enabled:
            logger.info("Local web console is disabled")
            return
        self.loop = loop
        handler = partial(_ConsoleHandler, console=self)
        self._server = _ConsoleHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="poly-mm-console",
            daemon=True,
        )
        self._thread.start()
        logger.info("Local web console listening on http://%s:%s", *self.address)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def run(self, coroutine: Coroutine, timeout: float = 30) -> dict:
        if self.loop is None or not self.loop.is_running():
            coroutine.close()
            raise RuntimeError("Engine event loop is unavailable")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=timeout)


class _ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "PolyMMConsole/1"

    def __init__(self, *args, console: ConsoleServer, **kwargs) -> None:
        self.console = console
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, DASHBOARD_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/api/status":
            try:
                self._send_json(
                    HTTPStatus.OK,
                    self.console.run(self.console.controller.snapshot(), 5),
                )
            except Exception as error:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        elif path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._valid_control_request():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid control request"})
            return
        path = urlsplit(self.path).path
        if path == "/api/account":
            self._controller_payload_action("save_account", timeout=45)
            return
        if path == "/api/start":
            self._controller_action("start_bot", timeout=45)
            return
        if path == "/api/stop":
            self._controller_action("stop_bot")
            return
        if path == "/api/preflight":
            self._controller_action("run_preflight", timeout=45)
            return
        if path == "/api/expiry":
            try:
                payload = self._read_json_body()
                result = self.console.run(
                    self.console.controller.set_quote_expiry(
                        payload.get("hours"), payload.get("minutes")
                    )
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "status": result})
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception as error:
                logger.warning("Console expiry action failed: %s", error)
                self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        if path == "/api/expiry/clear":
            try:
                result = self.console.run(self.console.controller.clear_quote_expiry())
                self._send_json(HTTPStatus.OK, {"ok": True, "status": result})
            except Exception as error:
                logger.warning("Console clear-expiry action failed: %s", error)
                self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return
        actions = {
            "/api/pause": self.console.controller.pause_quotes,
            "/api/resume": self.console.controller.resume_quotes,
            "/api/cancel-all": self.console.controller.emergency_cancel,
        }
        action = actions.get(path)
        if action is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            result = self.console.run(action())
            self._send_json(HTTPStatus.OK, {"ok": True, "status": result})
        except Exception as error:
            logger.warning("Console action %s failed: %s", path, error)
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})

    def _controller_action(self, name: str, timeout: float = 30) -> None:
        action = getattr(self.console.controller, name, None)
        if action is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            result = self.console.run(action(), timeout)
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:
            logger.warning("Console controller action %s failed: %s", name, error)
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})

    def _controller_payload_action(self, name: str, timeout: float = 30) -> None:
        try:
            payload = self._read_json_body(maximum=8192)
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        action = getattr(self.console.controller, name, None)
        if action is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            result = self.console.run(action(payload), timeout)
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:
            logger.warning("Console controller payload action %s failed", name)
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})

    def _read_json_body(self, maximum: int = 2048) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request length") from error
        if not 1 <= length <= maximum:
            raise ValueError("Invalid request body")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ValueError("Invalid JSON body") from error
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _valid_control_request(self) -> bool:
        if self.headers.get("X-Requested-With") != "poly-mm-console":
            return False
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        return not origin or (bool(host) and origin == f"http://{host}")

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args) -> None:
        logger.debug("Console %s - %s", self.address_string(), format % args)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Polymarket 做市控制台</title>
  <style>
    :root { color-scheme: dark; --bg:#080b12; --panel:#111827; --line:#263247;
      --muted:#91a0b8; --text:#eef4ff; --cyan:#45d9c5; --amber:#ffbd5b; --red:#ff6b78; }
    * { box-sizing:border-box } body { margin:0; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
      color:var(--text); background:radial-gradient(circle at 12% 0,#13243a 0,transparent 34%),var(--bg); }
    main { max-width:1180px; margin:auto; padding:28px 20px 52px } header { display:flex; gap:16px;
      align-items:flex-end; justify-content:space-between; margin-bottom:22px } h1 { margin:0; font-size:25px }
    .subtitle,.muted { color:var(--muted) } .grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px }
    .card,.panel { border:1px solid var(--line); background:rgba(17,24,39,.88); border-radius:14px;
      box-shadow:0 14px 40px rgba(0,0,0,.18) } .card { padding:15px } .label { color:var(--muted); font-size:12px }
    .value { margin-top:7px; font-size:18px; font-weight:700 } .ok { color:var(--cyan) } .warn { color:var(--amber) }
    .bad { color:var(--red) } .panel { margin-top:14px; padding:18px } .panel-head { display:flex;
      align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px } h2 { margin:0; font-size:16px }
    .actions,.duration { display:flex; gap:8px; flex-wrap:wrap; align-items:center } button,select,input { border:1px solid var(--line); border-radius:9px;
      padding:9px 13px; color:var(--text); background:#182338; cursor:pointer; font:inherit } button:hover { border-color:#5c708f }
    button.primary { color:#061713; background:var(--cyan); border-color:var(--cyan) } button.danger { color:#250509;
      background:var(--red); border-color:var(--red) } button:disabled { opacity:.45; cursor:wait }
    table { width:100%; border-collapse:collapse } th,td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line) }
    th { color:var(--muted); font-size:11px; text-transform:uppercase } td.token { max-width:250px; overflow:hidden;
      text-overflow:ellipsis; white-space:nowrap } .empty { padding:18px 8px; color:var(--muted) }
    input { cursor:text; min-width:0 } .form-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px }
    .field { display:flex; flex-direction:column; gap:6px; color:var(--muted) } .field small { font-size:11px }
    .account-status { margin-top:12px; display:grid; grid-template-columns:repeat(3,1fr); gap:10px }
    .account-status div { padding:10px; border:1px solid var(--line); border-radius:9px; overflow-wrap:anywhere }
    .details { margin-top:12px; color:var(--muted); font-size:12px } .error { display:none; margin-bottom:14px; padding:11px 14px; border:1px solid #71313a; border-radius:10px;
      color:#ffacb4; background:#261117 } .foot { margin-top:18px; color:var(--muted); font-size:12px }
    @media(max-width:800px){.grid{grid-template-columns:repeat(2,1fr)}.form-grid,.account-status{grid-template-columns:1fr} header{align-items:flex-start;flex-direction:column}}
    @media(max-width:520px){.grid{grid-template-columns:1fr}.panel{overflow:auto}main{padding:20px 12px}}
  </style>
</head>
<body><main>
  <header><div><h1>POLYMARKET / MAKER</h1><div class="subtitle">本机操作控制台 · 127.0.0.1</div></div>
    <div id="updated" class="muted">正在连接…</div></header>
  <div id="error" class="error"></div>
  <section class="panel">
    <div class="panel-head"><div><h2>账户设置</h2><div class="muted">密钥只保存到 VPS，不会从状态接口回显</div></div>
      <span id="account-ready" class="warn">配置检查中</span></div>
    <form id="account-form" autocomplete="off">
      <div class="form-grid">
        <label class="field"><span>钱包 Private Key</span>
          <input id="private-key" name="private_key" type="password" autocomplete="new-password" spellcheck="false" placeholder="留空则保留已保存的私钥">
          <small>EOA 实盘签单需要；不会写入日志或返回浏览器</small></label>
        <label class="field"><span>钱包类型 / Signature Type</span>
          <select id="signature-type" name="signature_type">
            <option value="0">0 · 独立 EOA</option><option value="1">1 · POLY_PROXY</option>
            <option value="2">2 · POLY_GNOSIS_SAFE</option><option value="3">3 · POLY_1271 Deposit Wallet</option>
          </select><small>普通独立 EOA 请选择 0</small></label>
        <label class="field"><span>Funder / 资金钱包地址</span>
          <input id="funder-address" name="funder_address" autocomplete="off" spellcheck="false" placeholder="EOA 留空；代理或存款钱包填写地址">
          <small>类型 0 会自动使用私钥导出的地址</small></label>
      </div>
      <div class="account-status">
        <div><span class="label">签名钱包</span><br><span id="signer-address">—</span></div>
        <div><span class="label">资金钱包</span><br><span id="funder-status">—</span></div>
        <div><span class="label">L2 API 凭据</span><br><span id="credential-status">—</span></div>
      </div>
      <div class="details">保存时会在 VPS 内存中校验私钥，并通过 Polymarket 官方 SDK 创建或派生 L2 API key、secret 和 passphrase。页面永远不会读取已有私钥或显示 L2 secret。</div>
      <div class="actions" style="margin-top:12px"><button id="save-account" class="primary" type="submit">保存账户设置</button>
        <button id="run-preflight" type="button">运行实盘预检（不会下单）</button></div>
    </form>
  </section>
  <section class="grid">
    <div class="card"><div class="label">引擎状态</div><div id="phase" class="value">—</div></div>
    <div class="card"><div class="label">交易模式</div><div id="mode" class="value">—</div></div>
    <div class="card"><div class="label">报价开关</div><div id="paused" class="value">—</div></div>
    <div class="card"><div class="label">用户 WebSocket</div><div id="ws" class="value">—</div></div>
  </section>
  <section class="panel">
    <div class="panel-head"><h2>操作</h2><div class="actions">
      <button id="start" class="primary">启动挂单任务</button><button id="stop">停止任务并撤单</button>
      <button id="pause">暂停并撤销机器人订单</button><button id="resume" class="primary">恢复报价</button>
      <button id="cancel" class="danger">紧急清空配置市场订单</button></div></div>
    <div class="muted">紧急清场也会撤销当前账户在配置 token 上的手工订单。</div>
  </section>
  <section class="panel">
    <div class="panel-head"><h2>挂单任务有效期</h2><span id="expiry-state" class="muted">不限时</span></div>
    <div class="duration"><label><input id="expiry-enabled" type="checkbox"> 启用有效期</label>
      <select id="expiry-hours" aria-label="小时" disabled></select><span>小时</span>
      <select id="expiry-minutes" aria-label="分钟" disabled></select><span>分钟</span>
      <button id="apply-expiry" class="primary">应用有效期设置</button></div>
    <div class="muted" style="margin-top:10px">到期后自动暂停任务并撤销所有机器人挂单；有效期在服务重启后仍然保留。</div>
  </section>
  <section class="panel"><div class="panel-head"><h2>市场与仓位</h2></div>
    <table><thead><tr><th>市场</th><th>仓位</th><th>Bid</th><th>Ask</th><th>Spread</th><th>状态</th></tr></thead>
      <tbody id="markets"></tbody></table></section>
  <section class="panel"><div class="panel-head"><h2>活跃订单</h2><span id="order-count" class="muted"></span></div>
    <table><thead><tr><th>Order ID</th><th>方向</th><th>价格</th><th>数量</th><th>已成交</th><th>存活秒数</th></tr></thead>
      <tbody id="orders"></tbody></table></section>
  <section class="panel"><div class="panel-head"><h2>实盘预检</h2></div><div id="preflight" class="muted">尚无预检结果</div></section>
  <div class="foot">页面每 2 秒刷新。控制台仅监听本机；账户密钥只写入权限为 0600 的服务器 .env，不会回显、记录到控制台日志或提交到 GitHub。</div>
</main><script>
const $=id=>document.getElementById(id); const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let expiryFormDirty=false,accountFormDirty=false;
function state(el,text,kind){el.textContent=text;el.className='value '+kind}
function render(s){
 state($('phase'),s.phase,s.phase==='running'?'ok':s.phase==='error'?'bad':'warn');
 state($('mode'),s.dry_run?'DRY-RUN':'LIVE',s.dry_run?'warn':'bad');
 state($('paused'),s.paused?'已暂停':'正在报价',s.paused?'warn':'ok');
 state($('ws'),s.websocket_connected?'已连接':(s.dry_run?'未启用':'未连接'),s.websocket_connected?'ok':'warn');
 const a=s.account||{};$('account-ready').textContent=a.ready?'账户已就绪':'账户配置不完整';$('account-ready').className=a.ready?'ok':'warn';
 $('signer-address').textContent=a.signer_address||'尚未保存有效私钥';$('funder-status').textContent=a.funder_address||'—';
 $('credential-status').textContent=a.api_credentials_set?'已派生并保存在服务器':(a.api_credentials_partial?'配置不完整':'尚未派生');
 if(!accountFormDirty){$('signature-type').value=String(a.signature_type??0);$('funder-address').value=a.funder_configured?(a.funder_address||''):''}
 const running=Boolean(s.running);$('start').disabled=running||!a.ready;$('stop').disabled=!running;$('pause').disabled=!running;$('resume').disabled=!running;
 $('save-account').disabled=running;$('run-preflight').disabled=running||!a.private_key_set;
 $('markets').innerHTML=s.markets.length?s.markets.map(m=>`<tr><td>${esc(m.label)}</td><td>${esc(m.position)}</td><td>${esc(m.book.best_bid||'—')}</td><td>${esc(m.book.best_ask||'—')}</td><td>${esc(m.book.spread||'—')}</td><td class="${m.halted?'bad':'ok'}">${m.halted?'HALTED':'ACTIVE'}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">无市场</td></tr>';
 $('orders').innerHTML=s.orders.length?s.orders.map(o=>`<tr><td class="token" title="${esc(o.order_id)}">${esc(o.order_id)}</td><td>${esc(o.side)}</td><td>${esc(o.price)}</td><td>${esc(o.size)}</td><td>${esc(o.filled_size)}</td><td>${esc(o.age_seconds)}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">当前没有活跃订单</td></tr>';
 $('order-count').textContent=`${s.orders.length} orders`;
 const q=s.quote_task||{}; if(q.expired){$('expiry-state').textContent='已到期 · 挂单已停止';$('expiry-state').className='bad'}
 else if(q.deadline_at){$('expiry-state').textContent=`剩余 ${formatDuration(q.remaining_seconds)} · ${new Date(q.deadline_at*1000).toLocaleString()}`;$('expiry-state').className='ok'}
 else {$('expiry-state').textContent='不限时';$('expiry-state').className='muted'}
 if(!expiryFormDirty){$('expiry-enabled').checked=Boolean(q.deadline_at);if(q.deadline_at&&!q.expired){const totalMinutes=Math.max(1,Math.ceil(Number(q.remaining_seconds||0)/60));$('expiry-hours').value=String(Math.floor(totalMinutes/60));$('expiry-minutes').value=String(totalMinutes%60)}syncExpiryControls()}
 const p=s.preflight; $('preflight').textContent=p?`Signer ${p.signer_address} · pUSD ${p.collateral_balance} · 最小 allowance ${p.minimum_allowance} · ${p.country||'—'}/${p.region||'—'}`:'尚无预检结果';
 $('error').style.display=s.last_error?'block':'none'; $('error').textContent=s.last_error||'';
 $('updated').textContent='更新于 '+new Date().toLocaleTimeString();
}
function formatDuration(total){total=Math.max(0,Number(total)||0);const h=Math.floor(total/3600),m=Math.floor(total%3600/60),s=Math.floor(total%60);return `${h}小时 ${m}分 ${s}秒`}
function syncExpiryControls(){const enabled=$('expiry-enabled').checked;$('expiry-hours').disabled=!enabled;$('expiry-minutes').disabled=!enabled}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error(`HTTP ${r.status}`);render(await r.json())}catch(e){$('error').style.display='block';$('error').textContent='控制台连接失败：'+e.message}}
async function action(path,confirmText,body){if(confirmText&&!confirm(confirmText))return;document.querySelectorAll('button').forEach(b=>b.disabled=true);try{const headers={'X-Requested-With':'poly-mm-console'};if(body)headers['Content-Type']='application/json';const r=await fetch(path,{method:'POST',headers,body:body?JSON.stringify(body):undefined});const j=await r.json();if(!r.ok)throw Error(j.error||`HTTP ${r.status}`);if(j.status)render(j.status);if(j.message)alert(j.message)}catch(e){alert(e.message)}finally{refresh()}}
$('account-form').oninput=()=>{accountFormDirty=true};
$('account-form').onsubmit=async event=>{event.preventDefault();const body={private_key:$('private-key').value,signature_type:Number($('signature-type').value),funder_address:$('funder-address').value};await action('/api/account',null,body);$('private-key').value='';accountFormDirty=false};
$('start').onclick=()=>action('/api/start','确认启动挂单任务？默认配置为实盘，预检通过后会提交订单。');
$('stop').onclick=()=>action('/api/stop');$('run-preflight').onclick=()=>action('/api/preflight');
$('pause').onclick=()=>action('/api/pause'); $('resume').onclick=()=>action('/api/resume');
$('cancel').onclick=()=>action('/api/cancel-all','确认紧急清空配置市场的全部订单？这也会撤销手工订单。');
$('expiry-hours').innerHTML=Array.from({length:169},(_,i)=>`<option value="${i}">${i}</option>`).join('');$('expiry-hours').value='1';
$('expiry-minutes').innerHTML=Array.from({length:60},(_,i)=>`<option value="${i}">${i}</option>`).join('');
$('expiry-enabled').onchange=()=>{expiryFormDirty=true;syncExpiryControls()};
$('expiry-hours').onchange=$('expiry-minutes').onchange=()=>{expiryFormDirty=true};
$('apply-expiry').onclick=()=>{expiryFormDirty=false;if($('expiry-enabled').checked)action('/api/expiry',null,{hours:Number($('expiry-hours').value),minutes:Number($('expiry-minutes').value)});else action('/api/expiry/clear')};
syncExpiryControls();
refresh();setInterval(refresh,2000);
</script></body></html>"""
