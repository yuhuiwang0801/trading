# 短期行业相对反转研究

后续精进版见 [V2：已披露风险筛选与修复退出](REVERSAL_REFINEMENT_V2.md)。本页保留 V1 的定义和原始限制；`latest` 在完成 V2 后展示新报告，原运行存档不覆盖。

本模块实现用户《美股短期价格偏离与反转策略研究方案》的首轮 E00/E01。它是独立研究模块，不替换平台原有突破策略，不接入交易 worker，不发送券商订单。真实历史结果和数据质量限制一起展示。

## 策略定义

- E00：过去5个收盘间隔的总收益代理取负。
- E01：个股过去5日总收益代理，减去相同行业其他有效股票逐日等权收益的5日复合，再取负。每个交易日至少5个其他有效同行；当前使用SEC两位SIC的静态代理。
- 两实验使用相同可评分全集，排序后取前 `ceil(0.1*N)` 且分数大于零，限制后不扩大候选名单。E01允许股票自身上涨但落后于行业；没有另加绝对下跌门槛。
- 月末使用过去60个交易日原始收盘价×原始成交量的中位数排序，下月生效。原始价格≥5美元，至少252个已观察交易日。
- 下一交易日开盘价代理买入，入场交易日加5个交易日退出。该代理尚不能说明09:35后仍能获得同样收益。
- 每天最多4只、同时最多20只；单证券与发行人5%，行业25%，当日新增买入20%。只做多、整股、不借款、不重复加仓、不延长已有持仓、不在同日卖出后重买。
- 所有持仓共用现金账本，卖出所得按历史T+3/T+2/T+1结算；现金收益为零。未成功退出的持仓继续占风险额度，测试末尾不虚构清仓。
- 成本每边0、2、5、10、20bp，其中10bp用于默认展示；这是全包成本情景，不等同于实际券商收费。

配置见 `config/reversal_research_v1.json`。配置在首次计算收益前固定；E01始终为主实验，不根据结果选择最好的成本或策略。

## 数据来源与限制

Alpaca提供SIP历史日线，分别保存raw、split及all三种价格口径。历史SIP权限通过实际请求确认，不由实时IEX订阅推断。原始请求、原始分页响应、数据哈希和问题记录保留在独立快照。

Robinhood仅用于独立历史OHLCV核验。本轮交叉验证对象为AAPL和SPY的2016年及2026年10月样本，不代表全部研究证券完成交叉验收。不同来源不拼接成交量、不平均价格；插值、零量占位或无效OHLC不能作为可成交数据。Robinhood日线00:00Z字段作为交易日期标签使用，不先转换成纽约日期。

现有数据不能支持2005年开始的原方案，故在计算收益前固定为2016年预热、2017–2020年开发、2021–2023年验证、2024–2026-10-26冻结评估。各阶段从现金开始；阶段内年度曲线使用同一连续账本。

研究样本来自当前仍存续普通股中的2016年末流动性预选名单，行业是当前SEC分类回填。后续退市证券、当时行业、历史市值和证券身份衔接尚不完整，因此所有结果都是探索性结果，不能通过原方案G0或G3，也不能认定已获得Alpha。存在重大身份衔接疑点的证券在收益计算前按质量证据隔离，原始数据与原因仍保留。

公司行动优先使用通过校验的显式事件；其余由split/raw和all/split因子变化重建。合成分配不是已验收的现金股息，未知支付日按识别后30个自然日的下一交易日处理。历史供应商修订和拆分分配处理会影响结果。末尾未平仓部分尚无退出成本。

SPY全仓买入持有和按前日收盘股票仓位配置SPY加现金均仅作描述性比较；后者未模拟盘中仓位和调仓成本。原方案行业、规模、Beta、波动匹配组合因点时数据不足而不输出虚构结果。区块bootstrap区间只覆盖抽样不确定性，不包含数据偏差。

## 复现

在仓库根目录执行：

```bash
uv sync --group research
uv run python scripts/download_reversal_data.py --help
uv run --group research python scripts/run_reversal_research.py --snapshot data/reversal/alpaca_sip_20261027_proxy200_v2
```

下载脚本仅通过现有服务端凭据读取官方行情与SEC公开资料；不打印密钥，不调用交易API。已封存快照不覆盖。运行脚本先验证数据哈希、保存配置/代码/数据封存，再计算固定协议；打开2024+区间后保存解封记录。重复运行相同区间只是复现，不能称作新的未查看测试集。

输出：

- `data/reversal/latest/report.html`：中文报告及可导出图表。
- `data/reversal/latest/report.json`：报告、指标、全部试验、质量和限制。
- `data/reversal/runs/<run_id>/`：逐笔成交、拒绝原因、公司行动、现金/净值、期末持仓、评分、覆盖、数据和代码封存。
- `data/reversal/dual_source_validation.json`：Alpaca/Robinhood独立历史核验。

平台“策略与回测”页增加独立只读研究卡片。API：`GET /api/research/reversal/latest` 与 `GET /api/research/reversal/report`；它们不会排队执行回测、变更设置或下单。静态报告允许现有服务在不重启交易进程的情况下查看已完成结果。

## 验证

```bash
uv run python -m pytest backend/tests/test_reversal_signals.py backend/tests/test_reversal_backtest.py backend/tests/test_reversal_api.py -q
npm --prefix frontend run build
```

测试覆盖月末名单延迟生效、同行剔除与复合、未来数据不改变过去信号、缺失同行不填补、交易日退出、公司行动、历史结算、仓位与现金约束、退出受阻与期末持仓、只读接口无交易副作用。

官方口径：[Alpaca bars](https://docs.alpaca.markets/us/reference/stockbars)、[Robinhood工具](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)、[SEC公开数据](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)、[T+1说明](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/new-t1-settlement-cycle-what-investors-need-know-investor-bulletin)、[2017 T+2转换](https://www.nasdaqtrader.com/TraderNews.aspx?id=ETA2017-174)。
