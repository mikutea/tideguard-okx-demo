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

结果默认写入 `research/results/`，该目录不进入 Git。只有脱敏、可复核的汇总
才允许进入 `docs/` 和模型监督证据链。

## Windows quick start

```powershell
.\scripts\setup-research.ps1
.\scripts\run-research-benchmark.ps1
```

第一条命令会在 `%LOCALAPPDATA%\Tideguard\research-runtime` 创建隔离的
Python 3.11 环境，不会把机器学习库打进桌面 EXE。第二条只读取共享的公共
`market-data.sqlite3` 快照，并将证据写入已忽略的 `research/results/`；它不能
读取凭证或导入订单服务。

源码、许可和准入清单见 `THIRD-PARTY.md`。

可选的人类可读诊断可由同一隔离环境生成：

```powershell
& "$env:LOCALAPPDATA\Tideguard\research-runtime\venv\Scripts\python.exe" `
  .\research\render_quantstats.py `
  .\research\results\benchmark-full-v1.json `
  .\research\results\quantstats
```

QuantStats 图表以一个 90 天折为一期，只用于浏览折间稳定性；其比率不会替代
墨衡的逐交易 OOS、回撤、封存和双成本门槛。
