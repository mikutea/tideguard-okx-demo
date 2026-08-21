# 墨衡第三方研究层

这里不是第二套交易机器人。第三方库只在隔离的 Python 3.11 研究环境中读取
`market-data.sqlite3` 的不可变公共快照，并输出 canonical JSON 评估证据：

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
WebSocket 数据采集。NautilusTrader、Qlib 与 FreqAI 通过后续 canonical JSON
sidecar 协议对接，不进入墨衡凭证和执行进程。

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
```

完整历史同步需数小时，必须单写者串行运行。当前固定成员是今天按流动性规则
发现的幸存者 cohort，因此历史结果只用于模型消融，`promotable=false`；完成
至少 90 天前瞻公共 Shadow 前，不能据此扩展 Demo 或 Live 下单白名单。
