# 墨衡 v0.4 模型、验证与监督架构

## 可执行结论

v0.4 的内置模型是本机 NumPy 向量化、内容寻址、严格 JSON 的线性逻辑基线。它是真实机器学习模型，但不是“高收益模型”。训练、验证、未来 Shadow、监督和执行分别落盘；模型不能在线修改代码、风险阈值、交易环境或资金规模。

```text
OKX public completed candles
  -> recoverable SQLite history + immutable snapshot
  -> one shared feature/label matrix
  -> three frozen training configurations
  -> rolling purged walk-forward OOS
  -> prospective shadow
  -> paired champion-recipe comparison
  -> Codex content-addressed decision
  -> champion generation + scoped Demo lease
  -> deterministic risk + OKX Demo IOC
```

## 数据与特征

- 固定 `BTC-USDT / SPOT / 5m`；首次回填持续到 OKX 官方空页，2026-08-20 实测当前约 90.5 万根、最早 2018-01-11。
- 只训练 `confirm=1`、严格 5 分钟连续、无冲突、OHLC 合法的快照。
- 16 个特征只使用当时及过去数据：1/3/12/24/48 根收益、12/48 根波动率、振幅、相对成交量、EMA12/48 距离、RSI 和 UTC 小时/星期周期。
- 标签观察未来 12 根。路径先按 1.5% 止损、2.5% 止盈；同一 OHLC 同时触碰两侧时保守计止损，否则第 12 根收盘退出。
- 标签与验证扣除 24 bps 双边压力成本；数据、特征、标签、配置、split 和报告全部进入 SHA-256 证据链。

矩阵采用 `int64 timestamps + float64 features/returns + uint8 labels`。特征与标签只构建一次，三组配置共享同一矩阵；最终部署 fit 只使用最近 365 天，完整历史用于跨市场阶段 OOS，而不是机械等权拟合。

## Walk-forward v4

当前可晋级报告 schema 为 `tideguard.walk-forward.v4`，评估语义仍为 cash-SPOT long/flat、bracket、固定周期和非重叠资本：

- rolling 365 天训练；
- label horizon 12 + embargo 1，共 13 bars gap；
- 90 天 OOS 测试和 90 天 step，测试窗不重叠；
- 标准化只在每个训练折拟合；
- flat 时才接受 BUY，持仓窗内信号被忽略；SELL 不产生空头收益；
- 报告逐折起止、行数、交易、准确率、扣成本净收益、最弱折和最大回撤。

v1–v3 artifact/report 仍可读取和审计，但必以 `unsupported_evaluation_semantics` 拒绝新的晋级。

当前确定性淘汰门至少要求 5 折、1,000 OOS 行、20 笔非重叠交易、52% long/flat 准确率、0.5% 聚合 OOS 净结果、最差折不低于 -3%、最大回撤不高于 10%，且往返成本不少于 24 bps。这些只是淘汰门，不是未来收益保证。

## 长期更迭与同口径基线

同一批三个 candidate 绑定相同：

- `benchmarkCohortId`
- `evaluationDatasetSha256`
- `marketSnapshotSha256`
- `splitProtocolSha256`
- OOS 起止日期

已有 champion 时：

1. 若旧 champion 与 candidate 同 cohort，直接按同一报告口径比较。
2. 若新一轮使用新 snapshot，系统在新 cohort 的三组候选中找到与旧 champion 相同 `trainingConfigSha256` 的模型，将其作为 **paired champion-recipe baseline**。
3. challenger 必须在该 paired baseline 之上满足净收益改善和回撤不退化门。
4. 同配方 candidate 与自身 baseline 比较时不会产生所需改善，因此不能靠重复训练自胜。
5. 旧配方不在冻结候选族中或配对缺失时，返回 `champion_comparison_missing` 并停止自动换代。

这里重评的是 champion 的训练配方，不是把一个已拟合的静态 artifact 回测到它诞生前的历史；因此不会制造前视比较。

## Artifact v2

冻结 artifact 继续使用严格 JSON，不使用 pickle/joblib。manifest v2 额外绑定：

- evaluation dataset 与 final-fit dataset SHA-256；
- final-fit 行数和训练起止；
- training config、feature schema、benchmark cohort、market snapshot 和 split protocol；
- trainer、代码 revision、seed 和 validation run。

registry 在写入 validation 前逐项核对 manifest/report；不一致的候选不会进入 validated。

## Future Shadow

每个 validated/champion 在新的已完成 K 线上生成不可执行 shadow BUY。结算使用与执行一致的 bracket、12 根持有窗口和 24 bps 成本。晋级至少需要 20 个已结算 BUY、7 天跨度、正净结果和不高于硬上限的 shadow 回撤。

Shadow 是前瞻证据，但仍不是成交证明；实际 Demo 的滑点、成交率、费用、残余和 CAA 状态继续独立监测。

## Codex Supervisor

`CodexSupervisor.review_pack()` 只包含脱敏状态、artifact/report/snapshot/policy 哈希、同口径 OOS、Shadow、Demo 净成本结果和审计链状态。它不读取 API Secret，也不能选择 Demo/Live、修改品种、资金或 kill。

允许的决策：

- `approve`：空仓时对通过全部门槛的 candidate 做 generation CAS 晋级；
- `lease`：用户已启用 Demo master 时，为当前 champion 签发最长 24 小时的 Demo 执行许可；
- `reject` / `suspend`：拒绝候选或停止新入场；
- `rollback`：空仓时恢复一个仍满足当前证据门的 retired champion。

Live profile 在 CLI、SafetyController 和服务端状态中均拒绝 Demo Codex lease。

## 执行语义

- 自动 entry/exit 只在 Demo 走 `preview -> atomic claim -> dispatch_guard -> commit`。
- 最终 HTTP 前复核 user master、champion 代次/artifact、lease、账户身份、审计链、deadman lease 和硬风控。
- IOC 提交后逐笔查询；只有 `accFillSz` 进入模型库存。
- BUY 的 BTC 手续费减少净库存，USDT 费用进入净成本结果。
- SELL 不超过模型自有剩余库存；小于交易所最小量的残余进入 manual review，不能伪装已平仓。
- 模糊提交、取消、账户错绑或本地持久化异常触发 kill；未知订单不自动重试。

## FreqAI 边界

v0.4 不把 Freqtrade/FreqAI 打进 EXE。未来适配时只能作为独立 localhost 公共信号/研究进程，不能持有墨衡凭证或直接调用 commit。Freqtrade/FreqAI 为 GPLv3，若随安装器分发还需要完整许可、对应源码与构建义务。不得下载并反序列化未知 `.pkl/.joblib/.pt` “高收益模型”。

## 已知边界

- 当前只覆盖一个交易对和一个线性模型族；跨资产和非线性泛化尚未证明。
- OHLC 无法确定同一根内止损/止盈先后，验证按更不利结果。
- 通过历史 OOS 与 Shadow 仍可能在未来失效；没有模型能保证盈利。

## Historical replay V3

`research/historical_replay.py` 在隔离研究环境中对最新冻结多资产 cohort 运行高速
因果回放。它采用 365 天滚动训练协议，末 30 天作为隔离校准窗、此前数据拟合基础
模型，并每 30 天更迭一次；标签清除区间和模型可用时间戳保持独立。
`ml/historical_replay.py` 只实现无凭证的虚拟 SPOT 现金账本：
下一根 K 线延迟、双边费用、双边滑点、历史报价成交量容量和固定持有期都进入结果。

V3 报告是可重复审计的开发证据，不进入模型注册表，不累计 Shadow 天数，不修改
执行白名单，也不具备订单能力。训练中心的播放、步进和速度按钮只是对报告检查点的
本地可视化，不是后台交易控制器。
- Live 仅支持独立人工限时交易，AI 自动入场在 v0.4 硬禁用。
