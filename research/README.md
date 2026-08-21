# 墨衡第三方研究层

这里不是第二套交易机器人。常规模型库只在隔离的 Python 3.11 研究环境中读取
`market-data.sqlite3` 的不可变公共快照；NautilusTrader PoC 另用项目内隔离的
Python 3.12 sidecar。两者都只输出 canonical JSON 研究证据：

- 不读取 Windows Credential Manager；
- 不导入 `TradingService` 或订单端点，不实例化 OKX 私有客户端；
- 不加载 `.pkl`、`.joblib`、`.pt` 等不可信可执行序列化文件；
- 不自动注册、晋级、授权或下单；
- 每个模型使用同一 365 天 rolling train、90 天非重叠 OOS、13 bars
  purge/embargo、long/flat、12 bars 持有期和 24 bps 成本；
- 最后四个 OOS 折单列为 sealed holdout，另做 48 bps 双成本压力测试。

首批预注册模型族来自 Qlib/FreqAI 常用的可审计本地训练器：LightGBM、
XGBoost、CatBoost、scikit-learn HistGradientBoosting、ExtraTrees 和 MLP。
所有模型都从本机公共行情重新训练，不下载所谓“高收益权重”。

QuantStats 只做周期收益指标交叉验证；Cryptofeed 只用于未来 OKX 公共
WebSocket 数据采集。NautilusTrader `2.0.0rc3` 以固定 wheel SHA-256 的独立
Python 3.12 sidecar PoC 对接；Qlib 与 FreqAI 仍只保留后续 canonical JSON
sidecar 边界。它们都不进入墨衡凭证、订单进程或桌面 EXE。

结果默认写入项目内 `.research-data/`，该目录不进入 Git。只有脱敏、可复核的汇总
才允许进入 `docs/` 和模型监督证据链。

## Windows quick start

```powershell
.\scripts\setup-research.ps1
.\scripts\run-research-benchmark.ps1
```

第一条命令会在 `Y:\Projects\okx\.research-data\runtime` 创建隔离的
Python 3.11 环境，不会把机器学习库打进桌面 EXE。第二条只读取项目内的公共
`btc-market-data.sqlite3` 快照，并将证据写入已忽略的 `.research-data/benchmarks/`；它不能
读取凭证或导入订单服务。

源码、许可和准入清单见 `THIRD-PARTY.md`。

可选的人类可读诊断可由同一隔离环境生成：

```powershell
& ".\.research-data\runtime\venv\Scripts\python.exe" `
  .\research\render_quantstats.py `
  .\.research-data\benchmarks\benchmark-full-v1.json `
  .\.research-data\benchmarks\quantstats
```

QuantStats 图表以一个 90 天折为一期，只用于浏览折间稳定性；其比率不会替代
墨衡的逐交易 OOS、回撤、封存和双成本门槛。

## Public multi-asset discovery

```powershell
& ".\.research-data\runtime\venv\Scripts\python.exe" `
  .\research\discover_universe.py
```

该命令只访问 OKX 公开 `SPOT instruments + tickers`，按 USDT、上线时间、
成交额、点差、新鲜度、稳定币和杠杆代币排除规则生成内容寻址候选快照。
它不会更改生产订单白名单；候选仍需逐资产完整历史、对齐 cohort、相关性和
组合级 OOS 后才能进入 Demo shadow。

新闻/社媒的首个本地基线只使用 MIT 的 VADER，并冻结包版本与完整词典哈希：

```powershell
& ".\.research-data\runtime\venv\Scripts\python.exe" `
  .\research\vader_adapter.py --self-test
```

该适配器只接受已经通过来源许可和 point-in-time 检查的本地事件；它不下载
文章、模型权重或社媒数据，输出也只能作为后续消融研究的数值弱信号。

## Phase 2: multi-asset public research

以下命令只使用公开数据，所有数据库、锁、进度和数组都留在项目内
`.research-data/`。它们没有凭证提供器、私有 API 或订单能力：

```powershell
.\scripts\run-multiasset-history.ps1 -StatusOnly
.\scripts\run-public-signals.ps1 -Command status
.\scripts\build-multiasset-cohort.ps1
.\scripts\run-multiasset-benchmark.ps1 -MaxFolds 1 -Families hist_gradient_boosting
```

完整历史同步需数小时，必须单写者串行运行。当前固定成员是今天按流动性规则
发现的幸存者 cohort，因此历史结果只用于模型消融，`promotable=false`；完成
至少 90 天前瞻公共 Shadow 前，不能据此扩展 Demo 或 Live 下单白名单。

多资产 benchmark 只接受经过二次验证的内容寻址 cohort：运行前会重验清单、
数组哈希、严格 5 分钟时间网格、OHLCV 领域规则和相关矩阵。训练/测试按时间
滚动切分，训练标签与测试窗之间保留 label horizon + embargo。V2 在每个训练折
中再保留最后 30 天作为隔离校准窗，基础模型、校准窗和开发测试窗之间均有标签
清除区间；Platt 概率校准只负责恢复真实置信度，单调 expected-return 映射再把分数
转换为预期毛收益。

V2 只有在预期毛收益严格超过 24 bps 往返成本和预声明安全余量时才允许一个现金
SPOT long 仓位，否则持有现金；最短再次入场间隔候选为 48/96 根 5 分钟 K 线，
另做 48 bps 压力测试。V1 的最后四个 sealed 折已经查看，因此 V2 只运行前五个
回顾性开发折，并将旧 sealed 折标记为 retired，绝不把重复查看的历史伪装成全新
OOS。任何结果仍固定为 `research_only` 和 `promotable=false`；只有新的 90 天
前瞻公共 Shadow 才能提供新证据，且不会自动注册 champion、扩大白名单或下单。

## Phase 3: 历史高速回放训练场 V6

V6 把内容寻址的多资产 cohort 变成一个因果历史时钟：每个模型只看当时已经完成
标签的数据，使用 365 天滚动训练协议，其中末 30 天作为隔离校准窗、此前数据用于
基础模型拟合；随后回放 30 天，再训练下一代模型。确认线收盘与下一根 K 线开盘
位于同一时间戳边界，故在已对齐的执行时钟上使用 `latency=0` 入场，并在 12 根后
的开盘退出。标签 horizon 为 12 bars，另加 1-bar embargo，共保留 13 bars gap。
虚拟 SPOT 经纪商维护单一现金账本，并显式扣除双边手续费、滑点和历史成交量容量
约束；超出容量的目标仓位只缩小到可成交规模并留下证据：

```powershell
# 单周期冒烟测试
.\scripts\run-historical-replay.ps1 -MaxEpisodes 1

# 对最新冻结 cohort 运行所有可用的 30 天周期
.\scripts\run-historical-replay.ps1

# standalone 复核哈希、因果时间线、逐笔延迟与现金账本
& ".\.research-data\runtime\venv\Scripts\python.exe" `
  .\research\verify_historical_replay.py `
  .\.research-data\replays\historical-replay-v6-<timestamp>.json
```

证据写入 `.research-data/replays/historical-replay-v6-*.json`，训练中心会以逐日权益
曲线、可播放时钟和模型更迭轨展示最新一份通过哈希校验的报告。播放器只重放报告，
不会重新训练，也不会访问私有 API。

最终 V6 合同要求组合普通/压力和 BTC 普通/压力四套账本都声明
`checkpointValuationBasis=current_bar_open_at_checkpoint_boundary`，保留峰谷见证和
精确检查点，并满足 `cash + positionMarketValue = equity`。报告采用独占锁、唯一
临时文件和 fail-if-exists 原子重命名；同名并发写入只能有一个成功，旧证据不可覆盖。

V5 在已经代表“确认收盘/下一根开盘”的时间边界之后又施加一根 K 线延迟，实际
入场比预定合同晚 5 分钟，因此其报告已退役。旧文件和数值保留用于审计，但不得再
称为 canonical、用于模型晋级或与 V6 结果直接拼接。V6 已完整运行两次，结果与
验证器明确定义的稳定核心 digest 均一致，并通过结构/逐笔账本完整性 verifier；
该 verifier 不从冻结源数据重新训练或重放，输出固定声明
`sourceReplayVerified=false`。完整数值、哈希和仍然阻断上线的条件见
`docs/reports/v6-execution-semantics/report.md`。

这是只使用公开行情的回顾性开发工具，不是“刷模拟盘天数”：所有报告固定
`research_only / promotable=false / shadowDaysCredited=0`，并保留固定幸存者偏差、
静态 OHLCV 成交模型、缺少历史订单簿和仍需 90 天未来公共 Shadow 等阻断项。
历史回放能更快淘汰差策略，不能证明未来盈利，也不会扩大当前仅 `BTC-USDT` 的
执行白名单。
