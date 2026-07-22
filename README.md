# Polymarket 自动做市机器人

这是一个 Polymarket CLOB 被动报价机器人，参考同级
`Predictfun marketmaker` 的部署方式重新实现。当前策略对选定的一个 outcome token
挂 **post-only BUY** 单：读取真实盘口，在最优买价后留出若干 tick，定时撤单重挂，
并限制挂单数、订单大小、已有持仓和总挂单名义金额。

项目不预置任何市场。网页控制器可以在零市场状态下运行；启动挂单任务前，需要在
网页“挂单设置”中添加并识别至少一个市场，然后明确保存。程序通过官方 Gamma API
动态解析 token，不会把可能变化的 token 信息硬编码进策略。

默认 `dry_run = false`。systemd 启动的是常驻网页控制器，不会自动启动挂单任务；用户
需要在页面中明确点击“启动挂单任务”，随后程序才会执行实盘预检，并在通过后签名和
提交订单。如只想模拟，必须先手动改成 `dry_run = true`。预测市场可能产生全部本金
损失；本程序不保证盈利。

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
聊天中。可通过 SSH 隧道打开网页控制器，在“账户设置”中直接保存；私钥和自动派生的
L2 凭据只写入 VPS 的 `/opt/polymarket-mm-bot/.env`。也可继续在 VPS 上手工编辑该
文件。最好新建一个只放小额资金的独立 EOA，不要使用主钱包。

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
sudo nano /opt/polymarket-mm-bot/config.toml
sudo journalctl -u polymarket-mm-bot -f
```

安装脚本会启动网页控制器，但不会自动启动交易引擎，因此不会因为安装而直接挂实盘
订单。如果服务升级前有挂单任务在运行，脚本会先通过 SIGTERM 让旧进程撤单，再安装
并重启网页控制器。常用命令：

```bash
sudo systemctl status polymarket-mm-bot
sudo systemctl stop polymarket-mm-bot
sudo systemctl restart polymarket-mm-bot
sudo journalctl -u polymarket-mm-bot -n 200 --no-pager
```

### 网页控制台

控制台不需要用户名或密码，只监听 VPS 的 `127.0.0.1:8081`；配置解析器会拒绝把它
绑定到公网地址。因此不要使用反向代理将控制台暴露到公网，只通过 SSH 隧道访问。

在自己的电脑建立 SSH 隧道：

```bash
ssh -N -L 8081:127.0.0.1:8081 your-user@your-vps
```

然后直接访问 <http://127.0.0.1:8081>，无需登录。

控制台显示引擎状态、实盘/模拟模式、User WebSocket、最近盘口、仓位、活跃订单和
实盘预检结果，并提供：

- 在网页中保存 Private Key、钱包签名类型和 funder 地址；普通独立 EOA 选择类型
  `0` 并让 funder 留空；
- 保存时在 VPS 内存中校验私钥，并通过官方 SDK `create_or_derive_api_key()` 自动派生
  L2 API key、secret 和 passphrase；页面和状态 API 只返回“是否已配置”，从不回显
  私钥或 L2 secret；
- 单独运行不会下单的实盘预检；
- 明确启动或停止挂单任务；网页服务本身重启后不会自动恢复实盘任务；
- 默认不设置有效期；在“挂单设置”中勾选后，可通过“小时”和“分钟”下拉框设置
  1 分钟至 7 天的挂单任务有效期，每次启动时重新倒计时；
- 有效期到达后自动暂停新挂单并可靠撤销全部机器人订单；
- 暂停报价并撤销机器人跟踪的订单；
- 恢复报价。
- 查看最多 300 行经过敏感信息脱敏的内存运行日志；复制或选择日志文字时会暂停刷新。

有效期到期后如需重新挂单，请再次启动任务，新的有效期会从本次启动时重新计算。

账户文件由独立的 `polymm` 用户持有且权限为 `0600`。页面和状态 API 不返回私钥、
API secret 或 passphrase，服务日志也不会记录提交内容。控制操作仍要求同源自定义
请求头以降低 CSRF 风险。由于页面没有额外登录层，必须保持 loopback 监听并只通过
SSH 隧道访问。

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
- `console_enabled` / `console_host` / `console_port`：本机控制台；host 必须是
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
