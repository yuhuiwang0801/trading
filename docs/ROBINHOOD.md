# Robinhood Trading MCP 接入

最新执行能力与开启步骤见 [实盘启用与运行](LIVE_TRADING.md)。此前已完成实际订单预览及不使用真实资金的执行联调。平台现仅提供 V2 自动交易，手动订单预览已移除；实盘由用户在设置页确认开启。

2026-10-27 重新核实官方文档：

- [外部 Agent 接入](https://robinhood.com/us/en/support/articles/onboarding-an-external-agent/)
- [交易与工具能力](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)
- 官方端点 `https://agent.robinhood.com/mcp/trading`

官方区分可读取账户与专用 Agentic/MCP 交易账户。外部 MCP 的交易审批默认关闭，部分交易仍可能要求审批；本平台不会更改券商审批设置。文档提供的工具名称只作参考，实际请求必须以授权后的 `tools/list` schema 为准。

## 首次连接

1. 打开 `http://127.0.0.1:8010/#settings`，在「实盘自动交易」的 Robinhood 行点击「连接账户」。当前回调固定为该本机地址；请勿从 localhost、其他端口或远程地址启动授权。
2. 浏览器跳到 Robinhood 官方页面。自行完成登录、多因素验证、账户选择及授权；若 Robinhood 提示建立专用 Agentic/MCP 账户，按券商页面处理。
3. 返回平台后确认 Robinhood 显示「已连接」。点击「开启实盘」时，平台自动检查账户权限、资金、持仓、订单、成交及审批事项，并按 V2 生成交易范围和额度；这一步不会发送真实订单或撤单。
4. 核对平台生成的账户与 V2 规则摘要，完成一次真实资金确认。无需选择策略、填写范围或额度，也没有手动订单预览步骤；每笔自动委托仍须通过内部券商预检和风控。账户号只显示末四位，账户与开启检查的时效由平台验证。

不需要在聊天、前端或 `.env` 粘贴 Robinhood token。其他客户端的 MCP 授权不属于本平台的后台凭证。移除本地授权会删除本平台保存的令牌，不会取消订单，也不等于在 Robinhood 撤销全部授权；券商端授权管理请在 Robinhood 完成。

## 已实现

`RobinhoodOAuth` 使用官方资源/授权元数据、动态客户端注册、PKCE S256、8 分钟单次 state 与 HttpOnly 回调 cookie、issuer 校验。服务端令牌以 `0600` 权限保存在 `data/robinhood-oauth.json`，刷新轮换通过文件锁和原子写入持久化；前端不接触 token。回调 URL 中的 code/state 不进入 API 访问日志。

`MCPClient` 独立于开发助手：Streamable HTTP JSON-RPC initialize、会话 ID、协议版本、分页 tools/list、JSON/SSE 响应、inputSchema 校验、有限超时、脱敏错误。工具调用不盲重试，仅向精确的官方端点发送凭证；认证失败立即清除连接状态。网页连接只使用本平台 OAuth，不回退到旧环境 token。

`RobinhoodMCPBroker` 内部接口：accounts/balances/positions/orders/fills/tradability/preview/submit/cancel，另提供 BrokerAdapter 的 get_accounts/get_account/get_positions/get_orders/get_fills/get_tradability/preflight/submit_order/cancel_order/find_order 方法名。

2026-10-27 已完成用户授权，实际发现 **87 个工具**。专用账户尾号 **9773**，现金账户、状态 active；七项只读检查全部通过。正式执行使用新的 `RobinhoodTradingAdapter` 与独立 `LiveExecutionService`，不使用下方通用映射。当前服务端开关关闭，**不发送真实订单/撤单**。OAuth 刷新有契约测试与安全持久化，长期续期可靠性未宣称实证。

`robinhood_verify.py` 只调用固定的账户只读工具，并同时校验真实 inputSchema/outputSchema；忽略返回 guide 中的动作建议。只选择唯一允许当前连接交易且状态正常的账户，绝不以默认账户或昵称推断执行账户。支持不透明游标分页、去环和页数上限，订单状态未知、币种不符、非法数值或成交数量不一致均阻断。真实账户数据不写入 PaperBroker。

## 旧通用适配器映射（非当前实盘路径）

默认映射为空，表示未验证，不能臆造请求参数。授权后的能力发现只读取工具信息；真实 schema 留在服务端供联调。服务端 `ROBINHOOD_CAPABILITY_MAP` 指向经审核 JSON：

```json
{
  "balances": {
    "tool": "<exact name returned by tools/list>",
    "arguments": {"<exact account parameter from inputSchema>": "$account_id"},
    "verified": true
  }
}
```

此例是占位格式，**不能直接用来调用券商**。输入映射必须通过 jsonschema。账户只读验证已使用实际 schema 和响应；通用执行适配器仍未接入 Engine，不能将任意 MCP 返回视为可执行 BrokerAdapter 响应。

| 内部能力 | 当前状态 |
| --- | --- |
| 账户、权限、余额、购买力 | 已实际只读核验；购买力取 get_portfolio，缺失不默认为 0 |
| 持仓、订单、历史成交 | 已实际读取；独立服务使用完整分页补拉系统成交，mock 重启恢复通过 |
| 可交易性、订单预检 | 用户指定 F 买入 1 股、限价 $11.62，实际预览 READY，无真实提交 |
| 下单、撤单 | 新适配器默认关闭，模拟响应验证通过，未进行真实资金验证 |
| 客户端订单 ID、推送、改单、沙盒、碎股 | schema 已确认 ref_id 幂等及碎股限制；实际提交去重未测试。推送、改单、沙盒未验证 |
| 审批状态 | 真实读取通过；审批关联/回流/被编辑阻断已 mock 验证，平台不自动批准 |
| 自动续期、断线恢复 | OAuth 回调与持久化刷新已实现；用户账户续期与长期无人值守仍待实证 |

## 仍需实证的边界

1. 在设置页使用官方授权流程连接，验证用户专用账户与实际刷新令牌政策。
2. 验证本平台安全存储、令牌轮换及重新登录恢复，不复制浏览器或开发助手的会话。
3. 账户和预览实际读取已完成；提交结果、审批回流、逐笔成交、UNKNOWN 恢复已 mock 验证，尚未向真实账户提交验证单。
4. 执行范围和额度由 V2 自动生成，绑定策略参数版本，并在页面完成真实资金确认后启用。旧 `ROBINHOOD_EXECUTION_ACCOUNT` 不参与新执行路径。
5. 外部手工交易或公司行为导致无法解释的账户变化时阻断，尚未自动分类并调账；长期续期仍待验证。
6. 开启步骤见 [实盘启用与运行](LIVE_TRADING.md)。本次没有通过真实资金交易来验证代码。

遇到 401、会话过期或续期失败应暂停执行，按发现的官方元数据重新连接。外部账户是否支持长期后台运行、刷新轮换及审批自动化仍需实证。当前代码不会宣称已经完成这些验证。

协议实现依据：[MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)、[MCP Authorization](https://modelcontextprotocol.io/specification/2025-03-26/basic/authorization)。

公开 OAuth 元数据已于 2026-10-27 实测 HTTP 200：

- [资源元数据](https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading)
- [授权服务器元数据](https://agent.robinhood.com/.well-known/oauth-authorization-server/mcp/trading)

官方声明授权端点 `https://robinhood.com/oauth`，注册端点 `https://agent.robinhood.com/oauth/trading/register`，令牌端点 `https://api.robinhood.com/oauth2/token/`；支持 authorization_code、refresh_token、S256 和 `none` 客户端认证。注册成功不等于用户已授权。

## 实际交易 schema 的约束

`place_equity_order` 支持账户内唯一 `ref_id`；必须在网络调用前持久化。返回 `order` 只是已提交，返回 `approval` 表示尚未下单，需分别建模。审批后订单的 ref_id 可能改为 approval_id，因此需保留本地意图、原始 ref_id、approval_id、order.id 和 executions.id 的完整关联。

成交明细位于 `get_equity_orders.orders[].executions`，没有独立股票 fills 工具。`created_at_gte` 筛的是订单创建时间，不能当作成交增量水位，否则会漏掉旧 GTC 的新成交。账户核验与新独立执行服务均读取完整分页；已有系统记录在停用提交后仍持续只读对账。

`review_equity_order` 已按用户指定样例实际调用。它声明不会下单，但缺少 readOnlyHint，因此只为此工具设置明确的预览例外，不放宽其他工具。每笔审批要求仍按实际预览/提交结果处理，不能根据账户开关推断全部订单免审批。
