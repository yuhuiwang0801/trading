# Alpaca 真实美股行情接入（保持本地模拟交易）

当前选择 **免费 IEX 行情 + 本地 PaperBroker**。项目根目录的 `.env` 已创建，权限为 `0600`，只有文件所有者可读写；密钥现已配置。2026-10-27 北京时间 14:20 已实测通过 Paper 认证、IEX 行情读取及公司行动权限；验证期间休市，盘中时效仍待验证。结果见 [本次脱敏报告](alpaca-verification-2026-10-27.json)。

Alpaca 提供股票行情，本平台负责筛选、风控及模拟成交。这里的 PaperBroker 是本地模拟账户，账本保存在本项目数据库中；它与 Alpaca 网页中的 Paper Trading 账户、余额、订单和持仓彼此独立。接入器与验证程序仅向 Alpaca 发出只读 `GET` 请求，不向 Alpaca 发送、修改或取消订单，也不会启用实盘。

## 1. 获取 Paper Trading 密钥

1. 登录 [Alpaca Dashboard](https://app.alpaca.markets/)，选择 **Paper Trading** 账户。
2. 找到 **API Keys** 区域，选择 **Generate New Keys**，保存生成的 **Key ID** 和 **Secret Key**。如果已存在可用密钥，直接使用该账户的现有密钥；丢失 Secret 时才重新生成。
3. 确认账户对应的 API 地址是 `https://paper-api.alpaca.markets`。Paper 与 Live 使用不同密钥，不能混用。

获取按钮名称和步骤来自 [Alpaca 官方入门说明](https://docs.alpaca.markets/us/docs/getting-started-with-alpaca-market-data)。Paper 账户与 Live 账户密钥分离、Paper Only 账户可使用 IEX 行情的规定见 [Paper Trading 官方说明](https://docs.alpaca.markets/us/docs/paper-trading)。

## 2. 填写本地配置

用本机编辑器打开 `/path/to/trading/.env`。保留其中其他设置，填写以下两项：

| Alpaca 页面字段 | 项目 `.env` 变量 |
| --- | --- |
| Paper Trading 的 Key ID | `ALPACA_API_KEY` |
| 同一账户、同一组密钥的 Secret Key | `ALPACA_SECRET_KEY` |

配置应保持：

```dotenv
MARKET_SOURCE=alpaca
ALPACA_API_KEY=填入你的Paper账户Key_ID
ALPACA_SECRET_KEY=填入同一组Secret_Key
ALPACA_DATA_FEED=iex
ALPACA_TRADING_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets
```

上面的中文内容是占位说明，需替换为真实值。无需 OAuth Client ID、OAuth Client Secret 或 Robinhood 密钥。项目变量会转换为 Alpaca 所需的 `APCA-API-KEY-ID` 和 `APCA-API-SECRET-KEY` 请求头。Paper 账户读取行情同样使用 `data.alpaca.markets`；官方地址及请求头见 [Authentication](https://docs.alpaca.markets/us/docs/authentication)。

不要把密钥发到聊天、放入截图、提交到 Git，或写入 `VITE_` 前缀的前端环境变量。现有 `.env` 已受权限保护，直接编辑即可，不要用模板覆盖其他配置。适配器只接受 Alpaca 官方 HTTPS 地址，并禁止携带密钥跟随重定向。

## 3. 所需数据权限

| 数据 | 本次选择与验证要求 |
| --- | --- |
| 最新买卖报价、日线、五分钟线 | 使用 `feed=iex`，验证所选账户是否实际有权访问，以及返回时间戳、历史长度和报价质量是否满足策略要求。 |
| 股票资产目录 | 使用 Paper API 的只读资产目录，取得股票代码、资产 UUID 与当前可交易标志。 |
| 公司行动 | 单独调用公司行动接口验证；不能因为股票行情成功就假定此权限可用。若返回 `403`，本平台阻断相关扫描，不绕过检查。 |

**IEX 只覆盖单一交易场所。** 它的成交量和成交金额不代表全美市场总量，成交活跃度筛选也仅反映该数据源；部分股票可能因历史缺失、报价稀疏或新鲜度检查不满足而被排除。Basic 提供免费 IEX 实时覆盖；全市场 SIP 实时覆盖需要账户具备相应订阅和权限。本次保持 `ALPACA_DATA_FEED=iex`，不会自动切换 SIP，也不会把受限 SIP 请求悄悄降级为 IEX。[官方行情套餐说明](https://docs.alpaca.markets/us/docs/about-market-data-api)

公司行动数据采用保守检查；官方明确说明其收集和处理可能有延迟，因此权限验证通过也不代表信息能保证即时完整。平台请求 `data_quality=all`，对于原始价格回看区间中的价格调整事件保守排除，缺少权限时阻断。[官方公司行动接口说明](https://docs.alpaca.markets/us/reference/corporateactions-1)

## 4. 重启并验证

填写密钥后，在项目目录执行：

```bash
cd /path/to/trading
uv run python scripts/manage.py stop
uv run python scripts/manage.py start
```

重启让 API 和后台 worker 重新读取 `.env`。随后打开 [本地平台](http://127.0.0.1:8010)，进入 **设置 → 行情连接 → 验证 Alpaca 行情**。后台任务完成后查看各项结果。验证任务只检查接入，不修改策略参数、不发送订单。

也可以在项目目录运行同一验证工具：

```bash
uv run python -m trading.alpaca_verify
```

验证报告保存在 `data/alpaca-verification.json`。报告用于区分密钥配置、认证、数据权限和数据质量；只保存脱敏诊断，不保存密钥或完整账户响应。已有测试通过代表软件检查通过，不能代替这次使用你本人密钥的实测。

## 5. 判断结果与处理失败

- **缺少密钥**：检查 `.env` 中两项是否填写、是否仍是占位文字，并重启服务。此时没有真实授权验证结果。
- **HTTP 401**：检查 Key ID 与 Secret 是否为同一组、是否来自当前 Paper 账户，以及是否已被重新生成的密钥替代。
- **HTTP 403**：查看具体失败接口。最新行情可能涉及所选 feed 的权限；公司行动失败需要单独处理。系统不会通过忽略该失败继续自动交易。
- **HTTP 429 或连接失败**：检查速率、网络与服务状态。工具使用有限重试；失败不会生成替代行情。
- **休市或盘前**：可以检查认证、权限与已完成历史数据，但不能据此宣布交易时段实时行情验证完成。常规交易时段还需检查报价新鲜度和已完成五分钟线；休市不会标记为可执行交易。
- **权限通过但候选池为空**：先查看历史覆盖、公司行动排除、IEX 成交额和策略条件。候选数量不是接入成功的唯一标准，不能为得到候选而跳过数据质量检查。

成功接入后，真实行情驱动本地模拟交易，实盘入口仍保持禁用。软件及行情验证不证明策略具备盈利能力。
