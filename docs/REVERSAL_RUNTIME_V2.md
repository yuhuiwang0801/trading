# V2 日常信号服务

2026-10-30 运行包升级为 v2，新增前收盘基准、有效同行集合及版本字段，供 [盘中退出层](INTRADAY_EXITS.md) 使用。旧包会触发一次自动更新，旧封存文件保留；升级不改变冻结日线选股参数。

`backend/trading/reversal_runtime.py` 将冻结 V2 研究规则应用于每天新获取的行情及 SEC 数据。它只调用 GET，不含交易、撤单或账户修改。

## 调用

```python
from trading.reversal_runtime import (
    expected_signal_session, build_runtime_packet, load_runtime_packet,
    runtime_packet_status,
)
```

- `build_runtime_packet(now=None, data_dir=None)`：慢任务，必须由独立研究 worker 或专用线程调用；抓取行情/SEC、计算信号并写不可变数据包。`data_dir` 是包存储根目录，默认 `data/reversal/runtime_v2`。
- `load_runtime_packet(now=None, data_dir=None)`：只读本地文件，不联网；校验内容哈希、配置、证券元数据、信号日期及执行日期。返回 `execution_eligible`。
- `expected_signal_session(now=None)`：美东时间 18:00 起使用当天已结束的 XNYS 会话，此前使用上一会话；节假日按 XNYS 日历跳过。
- `runtime_packet_status(...)`：去掉大量持仓路径数据的状态摘要。

命令行预热：

```bash
.venv/bin/python scripts/build_reversal_runtime.py
.venv/bin/python scripts/build_reversal_runtime.py --status-only
```

## 数据与时效

使用同一冻结 190 只证券名单及排除清单，核验最新 Alpaca asset UUID，避免将复用代码当成原证券。重新抓取过去 750 个日历日的 SIP 日线 raw/split/all 三种口径，要求信号日全部证券都有有效完整行情。独立于平台 IEX 盘中报价，不混用两种 feed 的成交量；不加载旧回测候选。

SEC 每日重新抓取各发行人的 submissions、覆盖近期 550 日的必要归档和 companyfacts。沿用研究版“申报日之后第一个交易日收盘才可使用”的规则。缺失/陈旧/不可比财务数值保持 unknown，禁止新开仓。请求失败导致整日包 blocked，后续可重新构建；已知财务风险仍保留退出标记。

每天成功的数据包首次生成后封存，不在同一信号日覆盖。后续日期生成新包；历史原件在 `packets/<内容哈希>.json`，`latest.json` 仅指向最新原件。原始请求、响应和输入哈希保存在 `attempts/`，进度保存在 `build_status.json`。

包仅供紧接信号日的交易会话美东 **09:35（含）至 09:40（不含）** 新买入，过期不重标日期。窗口结束后，已校验日线仍用于已有持仓的退出；卖出可在 09:35 至收盘继续处理。交易执行仍必须另行验证券商状态、报价和真实成交。

## 数据包字段

- `strategy_id=reversal_v2`、`strategy_version`、`signal_session`、`data_session`、`execution_session`、`created_at`、`config_hash`、`metadata_hash`、`universe_hash`、`packet_id`。
- `status/quality`：ready、blocked，读取时可变为 expired；`blocked_reasons`、`execution_eligible`。
- `candidates`：按冻结研究顺序排列，包含 symbol/security_id/issuer_id/industry/rank/score/entry_gap/signal_price。基本面筛除后不会递补原排名之外的股票。
- `observations[symbol]`：最近 32 会话 raw open/close、total-return index、adjusted open、split-adjusted prices、残差及同行收益、已知基本面退出标记。信号日之后逐日累计残差，与研究定义一致；不因滚动五日窗口消失而误判修复。
- `security_master` 按 symbol、`securities` 按 security_id 提供元数据及风险指标。

公司行动数据标记为 `adjusted_price_proxy_not_entitlement_ledger`；`cash_dividend` 为 null，不虚构真实现金到账。实际持仓绝对止损若使用复权价格，应以实际成交价乘成交日 `adjusted_open/raw_open` 归一，并明确这种近似不等于券商现金股息权益账。

局限仍是固定当前存续样本、冻结 SIC 和 SEC 披露延迟；日常服务不会把这些研究限制变成已验证的基本面分类能力。
