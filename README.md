# Northstar · 美股策略与实盘交易平台

本地可运行的 React / TypeScript + FastAPI 模块化交易平台。以 **Alpaca 真实行情生成信号、Robinhood 执行交易** 为主流程，具备独立巡检、实盘账本、风控、审计与顺序回放。当前本机已接通免费 IEX 与 Robinhood 独立授权；实盘自动交易默认关闭，由用户在设置页开启。

设置页直接显示 V2，点击「开启实盘」后，平台自动检查账户，按 V2 生成股票范围和当前账户额度，再展示一次真实资金确认。无需选择策略或填写股票、单笔金额、每日金额及持仓数。手动「订单预览」功能已移除；每笔策略委托仍自动完成券商预检、风控和 Alpaca / Robinhood 盘口交叉校验。见 [实盘启用与运行](docs/LIVE_TRADING.md)。

平台仅保留 **V2 基本面过滤与反弹退出**：使用独立的 SIP 日线及 SEC 披露资料每日生成信号，下一交易日 09:35–09:40 ET 允许新买，已有持仓按反弹修复及风险规则退出。启用前会按固定 190 只股票池和账户净值生成交易范围供确认。原「趋势突破」及其演示、回测入口已下架，历史记录和底层研究模块保留。详见 [V2 平台接入](docs/REVERSAL_LIVE_V2.md) 与 [每日数据服务](docs/REVERSAL_RUNTIME_V2.md)。

**软件流程验证不等于策略盈利验证。** 策略盈利状态仍为「待验证」。实盘提交、撤单、审批与恢复使用模拟券商响应测试，尚未进行真实资金交易。后台保留隔离的模拟及合成回放能力，用于研究和程序验证。

## 快速启动

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Node.js 22。

```bash
cd /path/to/trading
uv sync --python 3.12
npm --prefix frontend ci
npm --prefix frontend run build
uv run alembic upgrade head
uv run python scripts/manage.py start
```

访问 **http://127.0.0.1:8010**，API 文档 **http://127.0.0.1:8010/docs**。概览、持仓、订单、信号及日志显示独立实盘数据；筛选和研究使用策略行情。设置页提供 V2 实盘启停、自动生成的交易规则摘要及运行详情。

```bash
uv run python scripts/manage.py status
uv run python scripts/manage.py stop
uv run pytest -q
```

管理脚本启动三个独立进程：API、行情巡检 worker、研究 worker。关闭浏览器或启动终端不会停止它们；睡眠、断网、操作系统退出仍会中断本地监控。`data/*.log` 是运行日志，审计账本保存在数据库。

开发时可分别启动：

```bash
uv run uvicorn trading.app:app --host 127.0.0.1 --port 8010
uv run python -m trading.worker
uv run python -m trading.research_worker
npm --prefix frontend run dev
```

Vite 访问地址由终端显示，`/api` 代理到 8010。不要同时运行管理脚本与重复的 API 服务。worker 有本机进程锁及扫描租约。

## 数据来源与账本隔离

- 主界面使用 Robinhood 账户快照及独立实盘账本。券商已有持仓可查看，但不会自动交给策略管理；无法确定的市值或盈亏显示未知，不使用模拟账户数据或零值替代。
- `MARKET_SOURCE=alpaca`：读取真实行情。未配置授权或行情不合格时阻断策略委托。`DATABASE_URL` 下的本地纸面模拟账本仅用于隔离研究。
- `MARKET_SOURCE=demo`：后台保留 `data/demo.db` 合成回放；仅显式请求时推进，重复相同回放不会新增重复订单。合成数据和模拟账户控制不在主操作界面展示。
- 后台 PaperBroker 默认研究余额为 $10,000，与 Robinhood 的资金和交易权限无关。自动实盘策略仅整股、只做多、无杠杆；数量和金额以 Decimal 运算、十进制字符串持久化。

当前本机 `.env` 已创建且权限为 `0600`，直接填写密钥，无需重新复制模板。首次在其他目录安装时可复制 `.env.example` 为 `.env`。密钥仅在服务端；不要使用 `VITE_` 变量保存密钥。切换数据源或更新密钥后重启服务。真实数据纸面账本与合成账本不共用。

## 接入 Alpaca 真实行情

盘中执行报价沿用 **IEX**；V2 日线研究和每日信号单独使用 **SIP 历史行情**，需要对应历史数据权限，两者的成交量不会混用。在 [Alpaca Dashboard](https://app.alpaca.markets/) 选择 Paper Trading 账户，找到 API Keys，生成或取用该账户的 Key ID 和 Secret Key，填写本机 `.env`：

```dotenv
MARKET_SOURCE=alpaca
ALPACA_API_KEY=你的Paper账户Key_ID
ALPACA_SECRET_KEY=同一组Secret_Key
ALPACA_DATA_FEED=iex
ALPACA_TRADING_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets
```

密钥填写后执行：

```bash
uv run python scripts/manage.py stop
uv run python scripts/manage.py start
```

行情配置属于部署维护，设置页不再展示「行情连接」面板。可运行 `uv run python -m trading.alpaca_verify`，验证报告保存在 `data/alpaca-verification.json`。验证任务只读，不改变策略或发送订单；缺少密钥时报告待配置，不代表真实授权链路已通过。

IEX 为单场所行情，其成交量不代表全美市场。SIP 全市场实时行情需相应权限；公司行动接口也会单独实测，权限失败会阻断扫描。本地 PaperBroker 的余额、订单和持仓独立于 Alpaca Paper 账户；Alpaca 接入仅使用只读 GET 请求，所有模拟订单仍在本地执行。完整密钥获取步骤、权限和失败处理见 [Alpaca 接入与验证指南](docs/ALPACA_SETUP.md)。

## 架构

Robinhood 初次连接：访问 **http://127.0.0.1:8010/#settings → 连接账户**，在官方页面登录并授权。实盘开启流程会自动刷新账户、持仓、订单、成交与审批设置。平台独立保存和续期授权，不读取其他 MCP 客户端的令牌，也不需要在聊天或网页粘贴密钥。详见 [连接指南](docs/ROBINHOOD.md) 和 [启用步骤](docs/LIVE_TRADING.md)。

实盘启停在设置页「实盘自动交易」完成。页面直接显示 V2，无需选择策略或手填股票范围、单笔额度、每日买入额度和最多持仓。点击「开启实盘」后，平台按 V2 规则自动检查真实账户并生成交易条件，再由用户确认使用真实资金。手动「订单预览」功能已移除，自动交易内部的券商预检继续执行；开启后点击「停止实盘」即可停止自动提交。API 或交易 worker 每次重启均恢复为关闭，需要重新确认。旧变量 `ROBINHOOD_LIVE_ENABLED` 已弃用；已有 `.env` 中保留它也不会启用或阻止实盘。

```text
React UI → FastAPI（控制、查询、SSE、持久任务队列）
                     │
         SQLAlchemy ledger + operational state
                     │
     independent market worker → MarketData + XNYS calendar
       → Screener → Strategy → RiskManager → OrderManager
       → PaperBroker / [separate, default-off RobinhoodTradingAdapter]
       → verified Fill → Portfolio + Settlement + append-only Audit

     independent research worker → sequential ReplayBacktest
       → shared strategy/risk → subsequent-quote simulation
```

主文件：`backend/trading/engine.py`（编排）、`strategy.py`、`risk.py`、`paper.py`、`market.py`、`mcp_broker.py`、`backtest.py`。数据库实体及迁移在 `models.py`、`migrations/`。行情 worker 不在 Web 请求或浏览器定时器中运行；回测不占用巡检进程。

## 功能与验收说明

- 主界面以实盘为中心：总览、股票筛选、持仓、信号、订单、策略与回测、审计、设置；提供中英文、ET/本地时间、详情链路、日志导出。
- 订单意图先持久化，接受与成交分开；UNKNOWN 保留资金并先对账，禁止直接重发。
- 买单资金和卖单数量占用；撤单申请不立即释放；支持后续报价部分成交及撤单期间的成交。
- 日线阈值不含当天；只处理已完成 5 分钟 K 线。离开候选池仍监控持仓。保护线只能上调，属于本地服务规则，**不代表券商保护单**。
- 实盘由设置页启停；停止阻止尚未发出的自动提交，不清仓，也不会撤回已送达券商的订单。后台保留隔离的模拟控制能力，主界面不再展示模拟账户控制。
- 顺序回测保存真实计算结果、费用、回撤、逐笔记录和数据/参数哈希。缺少分钟数据或存在前视证据时阻断。

详细规则、数据授权、部署与尚未完成事项见：

- [行情与研究限制](docs/DATA_AND_RESEARCH.md)
- [短期行业相对反转策略与真实历史回测](docs/REVERSAL_RESEARCH.md)
- [Alpaca 接入与验证指南](docs/ALPACA_SETUP.md)
- [订单与策略规则](docs/EXECUTION.md)
- [Robinhood MCP 接入](docs/ROBINHOOD.md)
- [部署说明](docs/DEPLOYMENT.md)

## 当前外部联调边界

Alpaca 接口及只读验证工具已按官方 API 实现并使用 HTTP 合约测试。2026-10-27 已用用户配置的 Paper 密钥实测通过认证、IEX 报价、日线、1/5 分钟线与公司行动接口；AAPL、SPY 各覆盖 276 个完整交易日日线。当时为休市，**盘中时效尚待常规交易时段验证**；详见 `data/alpaca-verification.json`。IEX 是单一场所，不能视作全市场成交量；SIP 需要相应权限。公司行为采用保守排除/阻断，尚未实现完整调整账本和历史退市证券库。

Robinhood 已完成 OAuth 授权，实际发现 87 个工具，七项账户只读检查和指定样例券商预检通过。已有真实持仓不纳入策略，也不导入模拟账本。独立实盘链路支持持久意图、审批关联、逐笔成交去重、不确定提交只读恢复与即时停止；真实提交/撤单仅用 mock 验证，长期 OAuth 续期仍待实证。实盘默认关闭，本次未代用户启用。

## 源码仓库与本机数据

仓库包含源码、测试、示例配置及 V2 固定股票池的公共证券元数据。`.env`、券商授权、账户与交易账本、行情缓存、日志、账户验证记录和截图保留在本机，不随代码上传。请从 `.env.example` 创建自己的 `.env` 并配置授权；不要提交真实凭证。

`data/reversal/alpaca_sip_20261027_proxy200_v2/security_master.csv` 与 `data/reversal/v2_identity_review.json` 是参与完整性校验的静态资料，需保持配套；审查记录中的来源目录仅是生成时的溯源说明。每日行情与 SEC 资料需在自己的环境按 [每日数据服务](docs/REVERSAL_RUNTIME_V2.md) 重新获取。新环境不会继承原账户或实盘开启状态。
