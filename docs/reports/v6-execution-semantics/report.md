# 墨衡 V6：修正执行时间语义后的历史回放

生成日期：2026-08-22（北京时间）
结论等级：已通过 standalone 结构与账本完整性校验的历史开发证据；
`research_only / promotable=false / sourceReplayVerified=false`

## 先说结论

V6 修正了旧 V5 多等待一根 5 分钟 K 线的问题。特征只使用已经确认的源 K 线；
源 K 线收盘与下一根 K 线开盘是同一时间边界，研究账本在该边界以
`latency=0` 入场，并在 12 根后开盘退出。容量只使用决策时已经可见的源 K 线
报价成交量，不使用入场 K 线事后完整成交量。

在同一个冻结 7 资产 cohort 下，24 bps 往返成本的历史开发账本为
`+26.8442%`，48 bps 压力账本为 `+12.0877%`；当前唯一执行白名单
`BTC-USDT` 的独立切片分别为 `+1.7172%` 和 `+0.2790%`。

这些正数不等于可上线：BTC 普通成本切片只有 14 笔闭环，低于预声明的 20 笔
开发样本门；历史已被观察、策略阈值沿用旧开发假设、零延迟开盘成交是假设，且
没有盘口队列、前瞻 Shadow 或 Demo 成交。因此 V6 不累计 Shadow 天数、不生成
模型晋级、不触发订单。

| 证据层 | 成本口径 | 净收益 | 闭环 | 最大回撤 | 结论 |
|---|---:|---:|---:|---:|---|
| 7 资产历史开发账本 | 24 bps | +26.8442% | 187 | 4.7543% | 开发门通过，但不可晋级 |
| 7 资产历史压力账本 | 48 bps | +12.0877% | 151 | 8.2528% | 压力门通过，但不可晋级 |
| BTC-USDT 执行白名单切片 | 24 bps | +1.7172% | 14 | 0.8523% | 样本不足，门失败 |
| BTC-USDT 执行白名单切片 | 48 bps | +0.2790% | 11 | 0.9722% | 仅压力诊断，仍不可晋级 |
| 前瞻 Shadow v2 | 实际策略口径 | — | 0 | — | 需要 90 天/100 BUY |
| OKX Demo 闭环 | 实际费用口径 | — | 0 | — | 尚未授权开始 |
| Live AI | — | — | — | — | 当前物理禁用 |

最大回撤按每个 5 分钟开盘边界进行持仓清算估值，并计入估算手续费与滑点；它是
open-to-open 采样回撤，不是 K 线内最低价或历史盘口下的最坏可实现回撤。旧报告曾
把当前 K 线收盘价标到开盘时间戳上，导致 5 分钟前视；修正后组合普通成本回撤由
旧证据的 4.7320% 更新为 4.7543%，收益与成交数不变。

## 时间与数据合同

```text
已确认源 K 线 [t-5m, t] 的 close/features/quote volume
                    |
                    v
决策时间 t == 下一根 K 线 [t, t+5m] 的 open
                    |
                    +-- 研究假设：在 t 瞬时完成推理并按 open + 滑点入场
                    |
                    v
                 t + 12 bars 的 open 退出
```

- label horizon：12 bars；
- embargo：1 bar；
- train/test gap：13 bars；
- rolling train：365 天；
- calibration：每个训练窗末 30 天；
- retrain/replay：每 30 天；
- replay：840 天、28 个模型周期、241,920 个 5 分钟时间行；
- 成本：普通场景每边 8 bps fee + 4 bps slippage；压力场景每边
  8 bps fee + 16 bps slippage；
- 资金：单一 SPOT 现金账本、long/flat、目标 25%、固定 12 bars、非重叠持仓；
- 容量：使用决策时已确认源 K 线 quote volume 的 0.5%，超出时 clip；普通账本
  32 次 clip、0 次容量拒绝，压力账本 25 次 clip、0 次容量拒绝。

## 可复现性与验证范围

两次完整运行的报告体哈希和文件字节哈希不同，因为 `startedAt`、`completedAt`、
每代训练耗时、墙钟耗时与格式化字节属于运行观测值。由验证器明确定义的
`moheng.historical-replay-core.v1` 投影仅移除这些顶层运行字段和每代
`trainingSeconds`，其他数据、时间戳、成交和结果仍全部参与核心摘要：

| 项目 | 第一次 | 第二次 | 一致 |
|---|---|---|---|
| 文件 | `historical-replay-v6-20260821T230117Z.json` | `historical-replay-v6-20260821T230559Z.json` | — |
| `result` canonical SHA-256 | `d4c17435e470b606dfdf5cc278cb90e2a13356e31ed8d86484db99662b62f075` | 同左 | 是 |
| `moheng.historical-replay-core.v1` SHA-256 | `16ed5ee04954c0119cab5c9355e5888fd77df114192e5f7f8d20f361a528eece` | 同左 | 是 |
| 报告体 `reportSha256` | `7888cd1ecc0f2ad06a7143f52febd897056f0733c25565c02262475993c1d3f7` | `083d5d2eeb12ef022efb4564133de1b868a7f1012be6450f748b3e80439d5de9` | 预期不同 |
| 文件字节 SHA-256 | `924e04daf0fb3ebc88e9c02829d8b79fb69b28b29293e1dc50f6ab05a9fa2287` | `4dc5f20e433e99aca824ff06889644d535abe2a6e2d584f0604502cb78c650e8` | 预期不同 |
| standalone 结构/账本验证 | `structuralLedgerVerified=true` | `structuralLedgerVerified=true` | 是 |
| 从冻结源数据重新执行验证 | `sourceReplayVerified=false` | `sourceReplayVerified=false` | 是 |

`reportSha256` 是“排除 `reportSha256` 字段自身后的 canonical JSON 报告体”哈希，
不是缩进 JSON 文件的字节哈希；上表分别保留两者，避免混用。

当前监控读取的最新有效文件是：

- `.research-data/replays/historical-replay-v6-20260821T230559Z.json`
- schema：`moheng.historical-replay-report.v4`
- engine：`moheng.historical-replay.v3`
- frozen cohort：`cohort_6d7c319f462afdace7400053`

第一次有效复跑文件是
`.research-data/replays/historical-replay-v6-20260821T230117Z.json`。此前五份 V6
只保留作失败审计：`213029Z` 尚未完成修正合同；`213912Z`、`214516Z` 把当前
K 线 close 标在 open 时间戳；`222347Z`、`222758Z` 虽已改为 open 估值，但还没有
强制机器标记和峰谷见证。最终验证器会拒绝这些旧文件，监控器也不会回退展示。

最终四套账本都强制声明
`checkpointValuationBasis=current_bar_open_at_checkpoint_boundary`，并保留 843 个
检查点、峰谷见证、`cash + positionMarketValue = equity` 与累计峰值。验证器从这些
行重算检查点回撤并要求最大值等于报告值；这仍是嵌入证据的结构校验，不是读取
冻结行情的独立源重放。

`research/verify_historical_replay.py` 会重新计算报告体和稳定核心哈希，检查研究
安全合同、episode 因果顺序、决策/入场/退出时间、容量与防泄漏字段，并从成交价
逐笔反推原始价格，重算手续费、滑点、周转、毛/净 PnL、现金和分资产账本；对
BTC 普通/压力摘要也强制相同 V6 合同。它不会重新读取冻结 cohort、重建特征、
重新训练模型或重放行情，所以输出明确是 `sourceReplayVerified=false`，不能称为
独立源数据复现。

## 为什么仍不能上线

V6 解决的是“我们到底测了什么”，不是“未来一定赚钱”。仍然存在以下硬阻断：

- 开发历史已经被观察，且策略沿用在复用历史上形成的假设；
- 当前幸存者 cohort 固定，存在资产选择偏差；
- 没有新鲜封存 OOS；
- 只有 OHLCV，没有历史盘口、队列位置和逐笔冲击；
- `latency=0` 假设能在边界瞬时完成推理与下单，真实 Demo 必须测量实际延迟；
- BTC 只有 14 笔，样本门失败；
- 尚无 90 天/100 笔同策略前瞻 Shadow；
- 尚无至少 30 笔 Demo 闭环；
- Live AI 自动执行仍被硬禁用。

下一步应冻结 V6 的 BTC 候选和协议，先积累严格前瞻 Shadow，再在用户重新明确授权后
进入小额 Demo 验收。多资产正收益只能作为 challenger 研究线索，不能扩大订单白名单。
