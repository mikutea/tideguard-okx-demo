# Tideguard v0.3 模型、验证与监督架构

## 可执行结论

v0.3 的可执行模型是本机训练、内容寻址、严格 JSON 的逻辑回归基线。它是真实机器学习模型，但不是“高收益模型”，也不允许边交易边自改。训练、验证、监督和执行分别落盘；模型只有取得当前 Codex 决策和短期 lease 后才能提出一笔固定 10 USDT 的 Demo 入场。

```text
OKX public completed candles
  -> deterministic features and bracket labels
  -> three frozen challengers
  -> purged walk-forward OOS
  -> forward shadow
  -> Codex content-addressed decision
  -> champion generation
  -> scoped dispatch gate
  -> OKX Demo IOC order
```

## 数据与标签

- 固定 `BTC-USDT / 5m`，每轮目标 10,000 根 K 线。
- OKX history-candles 按每页最多 100 根分页；游标必须严格前进，页面间采用保守限频。
- 只接受 9 字段、`confirm=1`、严格 5 分钟连续、时间戳不在未来的 K 线。
- 16 个特征全部只使用当时及过去数据：多周期收益、波动率、振幅、相对成交量、EMA 距离、RSI 和 UTC 周期编码。
- label horizon 为 12 根 K 线。未来路径先按 1.5% 止损、2.5% 止盈判断；同一根 OHLC 同时触碰两侧时保守计止损，否则按第 12 根收盘价退出。
- 标签与验证统一扣除 24 bps 双边压力成本。训练配置、特征 schema、数据内容和验证报告均进入 SHA-256 绑定。

## Walk-forward v3

当前报告 schema 是 `tideguard.walk-forward.v3`，评估模式为 `long-only-bracket-fixed-horizon-non-overlapping`：

- 外层训练和测试按时间顺序，gap 为 label horizon 加 embargo；
- 外层测试窗互不重叠；
- 只在 flat 时接受 BUY，SELL 不产生空头收益；
- 一次入场占用诊断资本 12 根 K 线，期间信号被忽略；
- 各折报告交易数、准确率、净成本收益、最大回撤和最差折；
- v1 的重叠 long/short 和 v2 的无 bracket 固定周期报告仍可读取，但必以 `unsupported_evaluation_semantics` 拒绝晋级。

当前长期门槛至少要求 5 折、1,000 个 OOS 行、20 笔非重叠交易、52% long/flat 准确率、0.5% OOS 净结果、最差折不低于 -3%、最大回撤不高于 10%，且成本假设不少于 24 bps。这些只是淘汰门，不是未来收益保证。

## Future shadow 与 challenger 改善门

每个 validated/champion 模型在新完成 K 线上生成不可执行 shadow BUY。结算使用与 live 相同的 bracket、12 根持有窗和 24 bps 成本。晋级至少需要 20 个已结算 BUY、7 天跨度、正净结果和不高于 3% 的 shadow 回撤。

已有 champion 时，challenger 的历史 OOS 净结果还必须至少高出 0.2 个百分点，且最大回撤不得比 champion 高 1 个百分点以上。不同训练窗口的比较并非严格因果结论，所以 Codex 仍需结合 shadow、样本数、分折稳定性和 Demo 结果审查；程序不会把单一排行等同于收益提升。

## Codex Supervisor

`CodexSupervisor.review_pack()` 只包含脱敏状态、模型/报告哈希、OOS 和 shadow 指标、Demo 净成本结果、审计链状态及硬策略哈希。`generatedAt` 仅用于展示，不进入证据哈希，因此分开的 `review` 与 `approve` 命令可以复现同一静态状态；任何实质状态变化都会改变证据哈希。

允许的决策：

- `approve`：空仓时将 validated challenger 以 generation CAS 晋级；
- `lease`：用户已启用 Demo master 时为当前 champion 签发最长 24 小时许可；
- `reject` / `suspend`：拒绝候选或停止新入场；
- `rollback`：空仓时恢复一个仍通过当前门槛的 retired champion。

registry 变化、审计追加和 supervisor applied 标记不是同一数据库事务，因此执行端额外要求“当前 champion 存在完整 applied Codex 决策”。任何中途崩溃都会留下不可执行 champion，而不是绕过监督。

## 执行语义

- 自动 entry/exit 都走现有 `TradingService.preview -> commit -> dispatch_guard`。
- 最终 HTTP 前再次检查用户 master、champion 代次和 artifact、lease、模型持仓身份、Demo 回撤及硬风控。
- 监督 arm 绑定 decision ID 和 `entry|exit` 用途；浏览器不能在该窗口创建或提交手工订单。
- IOC 提交后逐笔 GET order；只有 `accFillSz` 进入模型库存。
- BUY 的 BTC 手续费减少模型净库存；USDT 入场/退出费用进入实际净成本回报。
- SELL 先按 lotSz 向下对齐且不超过模型净库存。部分成交只扣实际成交，继续管理剩余数量。
- 低于交易所最小卖出量的残余不会被写成“已平仓”；系统保留数量、急停并进入 manual review。
- 任何不确定提交、取消、身份错配或持久化异常都禁止重试同一未知订单。

## FreqAI 边界

当前安装器不捆绑 Freqtrade/FreqAI。FreqAI 若以后接入，只允许独立进程通过固定 localhost 协议提供已完成 K 线的冻结信号；它不能读取 OKX 凭证，也不能调用 commit。不得从 GitHub 下载并反序列化未知 `.pkl/.joblib/.pt` “高收益模型”。

## 已知边界

- 当前只研究一个交易对和一个线性模型族，不能据此声称跨市场泛化。
- 约 35 天滚动历史加至少 7 天未来 shadow 仍不足以覆盖所有市场状态；长期监督应积累更长 Demo 证据。
- OHLC 无法知道同一根 K 线内先触发止损还是止盈，验证按止损优先处理。
- 极端跳空、交易所停机、残余库存和未知订单仍可能需要人工账户核对；Codex 不能越过交易所事实或删除安全锁。
