# Polymarket 自动做市机器人

这是一个 Polymarket CLOB 被动报价机器人，参考同级
`Predictfun marketmaker` 的部署方式重新实现。当前策略对选定的一个 outcome token
挂 **post-only BUY** 单：读取真实盘口，在最优买价后留出若干 tick，定时撤单重挂，
并限制挂单数、订单大小、已有持仓和总挂单名义金额。

项目已预配置以下市场的 `Yes` outcome，启动时通过官方 Gamma API 动态解析 token，
不会把可能变化的 token 信息硬编码进策略：

<https://polymarket.com/event/clarity-act-signed-into-law-in-2026>

默认 `dry_run = false`，启动后会进入实盘预检，并在预检通过后签名和提交订单。如只想
模拟，必须先手动改成 `dry_run = true`。预测市场可能产生全部本金损失；本程序不保证
盈利。

## 已实现的实盘保护

- 实盘启动前检查 VPS 出口 IP 的 Polymarket geoblock、EOA signer/funder、L2 凭据、
  pUSD 余额和 CLOB allowance；任一失败都不下单。
- 所有订单使用 GTC、post-only，并使用订单簿返回的 tick size、最小订单量和
  neg-risk 属性。
- User WebSocket 快速接收 order/trade 更新，同时持续用 REST 对账，避免 WebSocket
  断线导致漏报。
- Data API 定期同步已有仓位；发现任何已有仓位或部分成交后，默认停止该 token 并
  可靠撤销其剩余订单。
- 每次下单后原子写入权限为 `0600` 的恢复日志。实盘重启时默认先撤掉这个账户在所配
  token 上的全部订单，再确认订单列表为空，以覆盖“CLOB 已接单、进程尚未来得及写
  日志”这一崩溃窗口。
- 撤单采用指数退避并通过 open-orders 再确认。正常退出也会撤单；不能确认撤单时以
  失败状态退出，让 systemd 重启后继续执行启动撤单。
- dry-run 不读取或覆盖实盘恢复日志。

注意：`cancel_all_on_start = true` 会撤销该账户在所配置 token 上的**手工订单**，不只
是机器人创建的订单。建议为机器人使用独立热钱包。

## 普通独立 EOA 需要什么

你的账户是普通独立 EOA，因此使用：

```dotenv
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER_ADDRESS=
POLYMARKET_SIGNATURE_TYPE=0
```

当前官方 Python CLOB 客户端在创建订单时需要本地私钥签名，所以本实现的实盘进程
**必须能够读取该 EOA 私钥**。仅有 API key/secret/passphrase 不够，因为它们负责 L2
请求认证，订单本身仍需要钱包签名。`FUNDER_ADDRESS` 对 EOA 可留空；若填写，预检会
强制它等于私钥导出的地址。

你不需要提供 Polymarket 登录密码、邮箱或助记词，也不应把私钥发给开发者或粘贴到
聊天中。直接在 VPS 的 `/opt/polymarket-mm-bot/.env` 写入私钥即可。最好新建一个只放
小额资金的独立 EOA，不要使用主钱包。

实盘还需要：

- EOA 中有足够的 pUSD 余额；
- 已向 Polymarket 所需 exchange 合约授权足够的 pUSD allowance；
- 若尚需在链上执行首次授权，钱包中留少量 Polygon POL 支付 gas；
- VPS 出口 IP 位于 Polymarket 允许下单的地区。

API 三件套是可选的。留空时程序用私钥调用官方 SDK 的
`create_or_derive_api_key()`；如果已有凭据，也可填入：

```dotenv
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
# Enables the local web console; username is admin.
POLYMARKET_CONSOLE_PASSWORD=use-a-long-random-password
```

## 本地运行

需要 Python 3.11 或更高版本：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp config.example.toml config.toml
cp .env.example .env
.venv/bin/python -m poly_mm.main --config config.toml
```

如果暂时不希望下单，请先把 `config.toml` 中的 `dry_run` 改成 `true`，观察模拟日志并
确认市场、outcome、价格、最小订单量与风险参数。也可执行只读实盘预检；它绝不会
提交订单：

```bash
.venv/bin/python -m poly_mm.main --config config.toml --preflight-only
```

默认配置已经是 `dry_run = false`。第一笔实盘建议把 `quote_size`、
`max_order_size`、`max_position_per_token` 和 `max_total_open_notional` 都维持在
交易所允许的最小值附近。

## 与 Predictfun 共用一台 VPS

可以共用。本项目只在 loopback 上监听 `8081`，不会与原机器人使用的 `8080` 冲突，
也不会开放公网服务。安全上，同一台 VPS 被攻破会同时危及两个热钱包，因此部署脚本
会使用完全独立的资源：

| 项目 | Predictfun | Polymarket |
|---|---|---|
| Linux 用户 | `predictmm` | `polymm` |
| 目录 | `/opt/predict-mm-bot` | `/opt/polymarket-mm-bot` |
| systemd 服务 | `predict-mm-bot` | `polymarket-mm-bot` |
| 恢复状态 | 原项目状态 | `/var/lib/polymarket-mm-bot/orders.json` |

把本项目上传到 VPS 后执行：

```bash
sudo ./deploy/install-vps.sh
sudo nano /opt/polymarket-mm-bot/.env
sudo nano /opt/polymarket-mm-bot/config.toml
cd /opt/polymarket-mm-bot
sudo -u polymm .venv/bin/python -m poly_mm.main --config config.toml --preflight-only
sudo systemctl start polymarket-mm-bot
sudo journalctl -u polymarket-mm-bot -f
```

安装脚本首次安装只会 `enable`，不会自动启动，防止意外实盘。如果服务升级前已在
运行，脚本会先通过 SIGTERM 让旧进程撤单，再安装并重启。常用命令：

```bash
sudo systemctl status polymarket-mm-bot
sudo systemctl stop polymarket-mm-bot
sudo systemctl restart polymarket-mm-bot
sudo journalctl -u polymarket-mm-bot -n 200 --no-pager
```

### 网页控制台

安装脚本会自动生成一个随机控制台密码并保存在
`/opt/polymarket-mm-bot/.env`，不会将密码打印进安装日志。控制台只监听 VPS 的
`127.0.0.1:8081`；配置解析器会拒绝把它绑定到公网地址。

在自己的电脑建立 SSH 隧道：

```bash
ssh -N -L 8081:127.0.0.1:8081 your-user@your-vps
```

然后访问 <http://127.0.0.1:8081>，用户名固定为 `admin`。在 VPS 上查看自动生成的
密码：

```bash
sudo sed -n 's/^POLYMARKET_CONSOLE_PASSWORD=//p' /opt/polymarket-mm-bot/.env
```

控制台显示引擎状态、实盘/模拟模式、User WebSocket、最近盘口、仓位、活跃订单和
实盘预检结果，并提供：

- 暂停报价并撤销机器人跟踪的订单；
- 恢复报价；
- 紧急撤销该账户在所有配置 token 上的订单，包括手工订单。

页面和状态 API 不返回私钥、API secret 或 passphrase。所有路由均使用 HTTP Basic
Auth，控制操作还要求同源自定义请求头以降低 CSRF 风险。

不要让两个服务共享 `.env`、Linux 用户或钱包私钥。VPS 还应禁用密码 SSH、只开放
必要端口、持续打安全补丁，并避免在 shell history、日志或备份中泄露 `.env`。

## 配置说明

市场配置支持 `https://polymarket.com/event/...` 或 `/market/...` URL，并通过
`outcome = "Yes"`/`"No"` 选择 token。一个 event 含多个 market 时还需设置
`market_slug`。关键运行参数：

- `preflight_enabled`：实盘启动保护，不建议关闭。
- `websocket_enabled`：启用用户订单/成交实时事件；REST 对账不会因此关闭。
- `position_poll_interval_seconds`：已有仓位同步间隔。
- `cancel_after_seconds`：单笔挂单多少秒后撤销并等待下一轮重挂。
- `cancel_retry_count` / `cancel_retry_base_seconds`：可靠撤单重试。
- `console_enabled` / `console_host` / `console_port`：本机鉴权控制台；host 必须是
  loopback 地址，默认 `127.0.0.1:8081`。
- `halt_on_fill`：任何部分成交或已有仓位出现后停止该 token。
- `cancel_all_on_start`：启动时撤销配置 token 的账户订单并确认清空。
- `cancel_all_on_shutdown`：SIGTERM/SIGINT 时撤销所有跟踪订单。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
bash -n deploy/install-vps.sh
```

## 官方文档依据

- [认证、EOA signature type 与 funder](https://docs.polymarket.com/api-reference/authentication)
- [Python CLOB L2、订单、余额与 allowance](https://docs.polymarket.com/trading/clients/l2)
- [Post-only 与订单状态](https://docs.polymarket.com/trading/orders/overview)
- [按市场/token 撤单](https://docs.polymarket.com/api-reference/trade/cancel-orders-for-a-market)
- [User WebSocket channel](https://docs.polymarket.com/market-data/websocket/user-channel)
- [查询用户持仓](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user)
- [VPS 出口 IP 地区检查](https://docs.polymarket.com/api-reference/geoblock)
