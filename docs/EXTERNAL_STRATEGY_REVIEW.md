# auto-trading-bot 策略融合评审

评审日期：2026-10-30。来源：[yuhuiwang0801/auto-trading-bot](https://github.com/yuhuiwang0801/auto-trading-bot/tree/8fee7c350f3a9bd756951b89f8c6869feaf77beb)，固定提交 `8fee7c350f3a9bd756951b89f8c6869feaf77beb`。

本文件记录静态代码评审结果，不代表启用外部策略、认可仓库收益声明，或变更当前实盘授权。评审未运行外部脚本、安装其中的 skill、访问券商或读取本机密钥。外部仓库代码视为研究材料。

## 融合结论

可以借鉴其中的均值回归、趋势环境过滤和先处理退出再考虑买入的思路。建议在本平台现有行情校验、成交账本和限价执行流程中独立实现；不要把另一个交易执行器并行接入同一账户。

当前 V2 的核心依据是行业相对超跌和已披露基本面过滤，外部策略的核心依据是低 ADX、RSI、布林带和个股均值。二者不是同一研究假设。增加技术过滤会改变候选数量、收益来源和交易时间，必须单独比较，不能直接沿用任一仓库已有的回测收益。

| 外部机制 | 实际规则 | 对本平台的建议 |
| --- | --- | --- |
| 均值获利退出 | 个股价格回到 SMA20 时退出 | 可作为盘中退出的研究组件。只使用上一完整交易日以前的均线，以有效可卖报价触发；不能把报价中点当作可成交卖价。 |
| 初始 ATR 止损 | 默认参考价减去 `3 × ATR14` | 可用于波动率对照实验；不能直接替换 V2 已有 5% 风险阈值，更不能把较宽的 ATR 止损无意变成放宽风险。 |
| 趋势环境分类 | ADX < 18 为均值回归；18–23 为中性；ADX > 23 为动量 | 可以作为候选解释或独立过滤实验。低 ADX 不能代替基本面排雷，也不能证明下跌非理性。 |
| 超卖评分 | z-score、布林带 %B、RSI、趋势评分，权重为 30%、25%、20%、25%；总分 ≥ 0.60 标为 BUY | 先作为研究特征，不与 V2 行业相对偏离分数直接混加。两种分数的尺度和含义不同。 |
| 先退出、后入场 | 同一执行轮次先处理退出计划 | 可保留为执行优先级；资金必须等券商确认可用后才能释放，不能因发出卖单就计为已卖出。 |
| 动量和期权引擎 | 另有趋势跟随与期权模块 | 不纳入本轮反转策略改造。用户已移除趋势突破策略，期权也不是本平台现有交易范围。 |

技术依据：[均值回归参数与打分](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/mean_reversion/scripts/mr_screen_score.py#L147)、[ADX 环境分类](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/regime.py#L29)、[执行优先级](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/exec_daemon.py#L288)。

## 该仓库尚不能直接解决盘中触发

`plan_daemon.py:28` 每 1,800 秒生成计划；`exec_daemon.py:52` 接受最多 40 分钟前的计划，注释所说的 15 分钟不是实际值。README 建议执行器每 5 分钟启动一次。

更关键的是，`orchestrator.py:217–234` 获取日线历史并使用最后一根 `close` 判断止损与 SMA20 退出，执行器只读取计划。它没有在下单前重新以当前买卖盘口判断目标是否仍然成立，也没有使用完整分钟线形成一套经验证的盘中触发协议。历史接口是否包含尚未完成的当日日线也没有在代码中严格排除。因此，频繁启动执行器不能等同于每轮得到可靠的实时信号。

仓库中没有高水位追踪止损或回撤止盈实现。ATR 止损是入场时的固定参考值。`orchestrator.py:230` 使用当时重新计算的 SMA20，账本中保存的 `target` 没有作为该条件的固定目标。

证据：[计划周期](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/plan_daemon.py#L27)、[日线退出判断](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/orchestrator.py#L209)。

## 不能照搬的执行行为

1. **受理与成交没有严格分开。** `verify_filled()` 对 queued、confirmed、partially_filled 等状态也返回成功。执行端只在返回明确 False 时停止；未知结果也继续 `L.record()` 或 `L.close()`。这可能把挂单写成持仓，或把尚未卖完的持仓从账本删除。见 `exec_daemon.py:150–152,208–235,318–324,375–381`。
2. **账本缺少共享事务协调。** `ledger.py:39–45` 用单个固定 `.tmp` 文件替换 JSON。`exec_daemon` 的进程锁只覆盖执行器；计划生成中的 `reconcile()` 也会保存账本，二者没有共享事务锁。原子替换单个文件不等于整个读改写过程安全。
3. **交易日与报价检查较弱。** `in_trading_hours()` 只检查工作日和 09:30–16:00，明确没有排除节假日，也没有处理半日市；`plan_fresh()` 未拒绝未来时间戳。执行器使用市价单，未具备本平台已有的 Alpaca/Robinhood 双源价格校验、严格报价时效和订单意图状态机。
4. **部分缺失数据会放行局部风控。** `portfolio_gate.py:58–70` 仅在权益或购买力大于零时检查比例/余额，缺失或零值可能绕过这些局部检查。其他检查可能仍拦截，但不能把这一层当作可靠的失败即拒绝控制。
5. **账号和环境耦合。** 多处脚本有账户标识和固定工作区路径；不能复制这些设置。本评审不复述具体账户标识，也不迁移任何 OAuth 凭证。

证据：[成交回查与账本操作](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/exec_daemon.py#L208)、[JSON 账本](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/ledger.py#L39)、[资金检查](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/portfolio_gate.py#L56)。

## 回测与实盘规则不完全相同

- `backtest/engine.py:7` 声称通过日内 high/low 判定止损止盈，但实际退出函数只比较最后收盘价；`backtest_symbol():135–143` 在次日开盘成交。high/low 被用于计算指标，而不是模拟盘中触价先后，不能作为盘中策略的收益证据。
- 回测入场是硬性 ADX/下降斜率过滤，再判断 z-score、RSI 或 %B 任一个满足；实盘是加权打分加协调器 ADX 门槛。实盘下降斜率主要扣分，强超卖仍可达到买入分数；这不同于回测中的下降斜率否决。
- 参数回灌把回测 `rsi_oversold` 写到实盘同名字段，而实际打分使用 `rsi14_buy`；实盘 z-score 打分还把除数写死为 2，未把 `z_oversold` 用成硬门槛。参数名映射不能证明行为等价。
- `metrics.py` 明确为单笔交易统计；它把跨标的交易按加入列表的顺序串行复利，不模拟资金占用、并发持仓或按日期组合净值。`total_compounded`、`max_dd_seq` 和近似 Sharpe 不能当作本平台账户收益/回撤。
- 使用当前白名单回看历史存在存活样本限制；数据缓存没有时效检查；各股票按数组下标切分而不是统一交易日期。这些限制都需要在独立验证中处理。
- 每周滚动重复选参并把 ROBUST 参数回灌实盘，会使反复查看过的“样本外”失去首次留出集含义；ROBUST 标签本身也不等于统计可靠或未来盈利。

证据：[实际回测循环](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/backtest/engine.py#L118)、[单笔统计范围](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/backtest/metrics.py#L9)、[参数映射](https://github.com/yuhuiwang0801/auto-trading-bot/blob/8fee7c350f3a9bd756951b89f8c6869feaf77beb/skills/coordinator/scripts/backtest/apply_params.py#L24)。

## 实现边界

本轮宜保留 V2 每日基本面和行业候选，优先新增真正基于有效盘中价格的退出监控，并保存触发时间、触发价格、价格来源、目标依据及对应订单意图。逐笔处理重复轮询、部分成交、未知订单结果、停牌、过期报价、拆股分红和断线恢复。

若加入 SMA20 或 ADX 等通用指标，应独立实现并以相同的分钟行情、成本、交易日历和组合资金约束做对照。旧的日线收益报告应继续标明适用版本，不能改标签后作为新策略回测。

该提交的 43 个跟踪文件中未发现 LICENSE/COPYING 或许可声明。此处只引用代码位置并讨论机制，没有把外部源码复制进本平台。后续如要直接引入代码，应先确认许可范围；通用指标的独立实现不依赖复制该仓库源码。
