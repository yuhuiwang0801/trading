# 行情、交易日历与研究回放

## 已实现的真实行情适配器

`backend/trading/market.py` 使用 Alpaca 官方 REST 接口，只有读取能力，与交易执行完全分开。开发时于 2026-10-27 重新查阅官方文档：

| 能力 | 实现接口与文档 |
| --- | --- |
| 证券主数据、稳定标识、交易状态 | [`GET /v2/assets`](https://docs.alpaca.markets/us/reference/get-v2-assets-1)，使用 Alpaca asset UUID 保存证券身份，symbol 是可变标签 |
| 批量日线、5 分钟历史 K 线 | [`GET /v2/stocks/bars`](https://docs.alpaca.markets/us/reference/stockbars)，显式 `feed`、`adjustment=raw`、`asof`，遍历 `next_page_token` |
| 最新买卖报价 | [`GET /v2/stocks/quotes/latest`](https://docs.alpaca.markets/us/reference/stocklatestquotes-1) |
| 公司行为 | [`GET /v1/corporate-actions`](https://docs.alpaca.markets/us/reference/corporateactions-1)，批量分页读取拆股、股息、名称变更及其他事件；保留原始事件字段 |
| 权限、频率及延迟说明 | [About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api)、[Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) |

服务端配置：

```dotenv
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_DATA_FEED=iex
ALPACA_TRADING_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets
ALPACA_REQUESTS_PER_MINUTE=180
# 为空则扫描当前证券列表中符合类型条件的证券；设置后为指定范围，不是全市场。
MARKET_SYMBOLS=AAPL,MSFT,NVDA,AMZN,META,GOOGL
```

兼容 `ALPACA_API_SECRET` 与 `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`。凭证只用于服务端请求头，不写入状态响应与错误日志。此交付没有获得用户 Alpaca 数据凭证，因此真实美股行情认证、实际订阅权限、全市场扫描耗时仍待外部联调。测试使用 HTTPX MockTransport 验证官方请求协议，不把 mock 测试描述成真实行情联调。

`MarketService.from_env().status()` 显示配置、最近成功/失败、数据权限和能力限制；`readiness()` 只有在实际有效扫描完成后才为 ready。未配置时返回 `quality=unavailable, reason=credentials_required`，不会自动替换成演示数据。

## 数据口径与安全边界

- `iex` 为单一 IEX 交易所数据，成交量不是全美成交量；流动性筛选显示 `single_venue_IEX`。同样的 2,000 万美元阈值应用到 IEX 会筛掉更多股票，并不能复现合并行情的全市场筛选。需要 SIP 权限才能获得合并口径。
- 官方文档当前列出 Basic 的实时覆盖为 IEX，历史限制为最新 15 分钟，历史 API 为每分钟 200 次；具体 feed、端点及账户权限以实际响应为准。服务端默认主动限速 180 次/分钟；403 不自动换 feed，不把延迟数据标为实时。
- 读取超时 12 秒、连接超时 5 秒，最多额外重试 2 次；只对连接故障、429、5xx 重试，退避最多 10 秒。按 100 个 symbol 批量获取；完整分页有循环检测和数量上限。证券缓存 1 小时，日线查询缓存 1 小时，公司行为缓存 15 分钟，分钟历史缓存 5 秒；最新报价不缓存。
- 日线只用前一完整交易日及以前数据，默认至少 120 个已观测交易日且最后一根必须属于前一交易日；最近所需交易日缺一日就不计算指标。上市交易日阈值可配置，但计算 60 日收益仍需至少 61 个收盘点，因此实际预热下限为 `max(61, min_listing_sessions)`。MA20、MA60、20 日高点均不包含当天；ATR14 是最近 14 个 true range 的简单平均。
- 日线和分钟线均采用原始价格；发现回看区间内的拆股或其他可能改变价格口径的事件时，该股票退出候选计算，等待明确的口径转换。公司行为权限缺失时整轮不产生指令。现金股息不会假装成价格复权；其历史总回报处理由研究数据事件完成。
- 官方公司行为接口提示事件发布可能延迟，成功查询也不等于完整或实时。适配器不将证券主数据中的 `tradable` 解释成实时未停牌证明；没有独立实时停牌 feed。过期、无效、未来报价或缺失已收盘 K 线会阻断执行。
- `us_equity` 还包括 ETF 等证券；第一版通过供应商名称中 common / ordinary 字样保守纳入，并排除 ETF、基金、优先股、认股权证等名称。名称筛选有覆盖缺口，不伪装成完整权威普通股分类。上线全市场前需接入权威子类型和历史证券主数据。
- 分钟供应商时间是开盘边界。适配器转换为 **已结束的 5 分钟区间终点**，仅接纳正常交易时段完整区间；每条记录有 source、feed、received_at、timestamp、granularity、quality、adjustment。
- `quote.last` 明确标注 `last_source=last_closed_5m_bar`，不是伪造的最新逐笔成交。买卖撮合依据 bid/ask；volume 明确是最近完整 5 分钟的观察值。买卖档位大小单独保存。
- 在候选池之外的持仓 symbol 可以通过 `fetch_scan(now, symbols=[...])` 传入，继续拉取行情和前一日指标；`holding_indicators` 为退出模块提供 MA20。
- 缓存属于当前服务进程；原始证券数据的完整历史仓库、公司行为持续订阅和点时证券主数据不是已完成能力。

## 美东日历

`calendar.py` 依赖 `exchange_calendars` 的 XNYS 日历，显式建立请求年份前后两年的日期范围。返回 UTC 的 open、close、next_open，交易时区固定为 America/New_York。开盘时刻包含在常规时段，收盘时刻不再允许提交新策略订单。盘前、盘后只显示阶段，不会允许常规策略提交。

验证包含：2026-03-06 开盘 14:30 UTC 与 2026-03-09 开盘 13:30 UTC 的夏令时变化；2026-07-03 独立日补休日；2026-11-27 感恩节后提前收市 18:00 UTC。无时区时间输入会被拒绝。

交易所临时休市及供应商异常依赖日历库后续维护；生产运行需定期更新并比对券商/数据源当日时钟，不能假定静态日历能预知紧急事件。

## 可重复的策略回放

```python
from trading.backtest import ReplayBacktest
from trading.demo import dataset
report = ReplayBacktest(lock_path='data/research-seals.json').run(dataset(), split='test')
```

回放共用 `strategy.entry_signal`、`strategy.exit_signal`、`strategy.limit_price_for`、`risk.size_entry`、`risk.validate_quote`，按时间顺序执行，退出规则维护已处理 K 线水位。先撮合旧意图，再检查退出和统一排序买入信号，再生成新意图。资金与未完成买单占用使用 Decimal 计算。

回放使用与模拟账户相同的保守已结算资金模型：卖出所得计入总现金，在下一个 XNYS 交易日才释放为已结算购买力。该模型模拟多数美股当前的 T+1 机制（[SEC 投资者公告](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/new-t1-settlement-cycle-what-investors-need-know-investor-bulletin)），不代替真实券商对账户、资金、银行假日及个别交易的结算判断。没有写死 PDT 数额。

输入包含：

```text
kind: synthetic | historical
source, version, research_id

granularity: 5Min
snapshots: [
  timestamp, session_date, quality, market_open, market_filter,
  candidates: [{symbol, security_id, rank, high20, atr14, ma20, known_at, asof_session, ...}],
  quotes: {symbol: {bid, ask, last, volume, timestamp, ...}},
  bars: {symbol: [{timestamp: 区间终点, close, ...}]},
  holding_indicators: {symbol: {ma20, ...}}
]
splits: {train: [开始日, 结束日], validation: [...], test: [...]}
benchmark: [{timestamp, close}]
```

区间日期包含首尾日，三段必须按时间前后排列且不得重叠。历史数据必须携带候选信息的 known_at，信息必须在当前交易日开盘前已经可得，指标 asof_session 必须等于前一交易日；允许前一日数据在收市后或次日盘前完成发布。未来 K 线可存在于完整数据容器中，但策略只接触当前时点已完成的 K 线；未来报价直接判为不合法。已可观察 K 线必须属于正常交易时段并位于合法 5 分钟边界，历史快照必须明确提供 quality。日线数据不能代替盘中验证，返回 `INTRADAY_DATA_REQUIRED` 且不计算收益。

匹配规则：信号确认后，只有后续时点的新鲜报价可撮合；买入使用 ask 加滑点、卖出使用 bid 减滑点，仍必须满足限价。限价按共用规则保留整美分，买入检查相对信号价的最大偏离。成交量受参与率、可用现金和持仓约束；相同 `volume_timestamp` 不能重复使用同一份可观察流动性，未传该字段时按报价时间区分。K 线触及限价不构成成交。超时按 TTL 过期，不自动追价；样本结束时不虚构全部清仓。剩余持仓按已观察到的报价估值。

同股每天最多一个入场意图；首次信号被报价或风控暂时阻止时，在同一突破条件仍存在且信号 TTL 未过期时可以重新评估。已有意图或首次信号过期后不在当日重新武装。退出触发在 TTL 内保持，暂时没有可用报价不会抹去已发生的退出事件。

回放模拟器采用已确认的本地取消，生产撤单竞争由订单引擎处理；回放不是历史订单簿排队模型。用户自己传入的 volume 必须说明频率与累计口径，并为反复引用的同一 K 线设置相同 `volume_timestamp`；Alpaca 适配器会自动设置。实际盘口队列和成交概率仍是模型限制。

报告保存参数、数据哈希、版本、净值和回撤、逐笔成交、完整退出批次、决策、订单意图及未完成订单。收益扣除费用并包含观察价差与设定滑点。提供交易次数、胜率、盈亏比、profit factor、换手率及持仓时长。少于 20 个交易日不输出年化 Sharpe。缺少对齐 SPY 数据时，基准收益返回 null，不补造曲线。

## 公司行为的研究输入

回放接受经过验证的标准化事件，要求 `corporate_action_price_basis='as_of_snapshot'`，即每个快照的指标、历史 K 线和当前报价已在该时点保持一致价格口径。支持：

- 拆股：`{id,type:'split',symbol,effective_at,ratio}`；数量按 ratio 调整，成本、入场 ATR、保护线、最高收盘价按反比例调整，总成本保持不变。
- 现金股息：`{id,type:'cash_dividend',symbol,ex_at,pay_at,cash_per_share}`；除息时按此前持仓建立应收股息，到支付日才变动现金，应收计入净值。
- 代码变更：`{id,type:'symbol_change',symbol,new_symbol,effective_at}`；保存原有持仓并切换标签，旧代码不可重新进入。
- 有明确现金对价的退市：`{id,type:'cash_delisting',symbol,effective_at,cash_per_share}`；以事件证据结算并记录，绝不默认退市股票价格为零或等于最近收盘价。

拆股/改名/退市发生时取消相关未完成模拟意图。未知类型、未知退市对价和未声明价格口径直接阻断运行。原始 Alpaca 公司行为响应不能直接作为上述标准化会计事件；完整原始事件到会计事件的生产归一化仍需外部数据验证。退出批次盈亏不分摊股息，应收/已付股息已计入总净值。

## 研究结果的诚实标识

演示数据固定标为 `kind=synthetic, source=demo`。演示回放只输出 `software_replay_only`，任何模拟盈亏都不能证明投资收益。历史运行即使通过顺序回放，也保持 `profitability_validated=false`。

历史验证需要确认点时选股池及退市样本、公司行为完整性、数据原始可获取时间，并提供预先划分的训练/验证/最终测试区间。缺少任一关键条件时 `efficacy_status=blocked_missing_data`。完整条件也只进入独立评估待办，不能自动宣称稳定盈利。

最终测试使用 research_id、数据版本与测试区间生成封存键，并记录参数哈希；同一测试集的参数变化将被拒绝。配置 lock_path 后封存通过文件锁与原子替换跨进程持久化。未配置时只在该回放实例生效。更换名字或外部复制测试集可以绕过软件登记，因此研究纪律仍需人工审查与数据版本治理。

## 实际集成核对

同一个演示交易日分别通过持久化 Engine 与 ReplayBacktest 的 train 区间运行：8 个观察点的现金与净值逐点相等，4 笔成交的数量、价格、费用和 UTC 时间逐笔相等。最终总现金/净值为 $9,983.3303096872，已结算现金 $8,275.5127745206，未结算卖出所得 $1,707.8175351666。这里是确定性软件样例的实际结果，不是投资评估。回放 JSON 的 side 大小写、UTC 格式和 Decimal 尾零与数据库表示不同，比较前进行表示标准化；经济数值完全相同。回归测试会持续验证上述一致性。
