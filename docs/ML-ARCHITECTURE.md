# 冻结模型、时间隔离验证与 OKX 模拟盘自动执行边界

## 结论

潮汐台可以增加自动研究与模拟盘执行，但它不是收益承诺，也不允许模型在线改代码、参数、风险阈值、品种或资金规模。推荐链路是：

```text
已完成 OKX K 线快照
  → 本地离线特征/标签数据集（内容哈希）
  → 严格 JSON 线性逻辑候选训练（当前可执行基线）
  → label_horizon + embargo 外层 walk-forward
  → 固定周期 long-only、非重叠的 OOS 研究诊断（理论退出 + 双边成本）
  → 成本、回撤、分折稳定性报告（内容哈希）
  → 人工审阅并以 generation CAS 晋级 champion
  → 短时、限额、绑定 champion 的单次 OKX Demo 入场许可
  → 冻结模型 BUY 建议意图（SELL 硬拒绝）
  → 现有 TradingService.preview
  → 现有 TradingService.commit / dispatch_guard / CAA / 急停
```

任何模型指标都只描述给定数据、时间区间和成本假设下的样本外研究诊断，不能外推为未来盈利能力。尤其要区分两种不同语义：OOS 报告假设 BUY 后按固定 horizon 理论退出并扣双边成本；v0.2 运行时只允许一次最多 10 USDT 的 Demo BUY 入场，不执行自动 SELL，退出需人工处理。因此 OOS round-trip 净值不是部署后已实现收益。

## 已落地模块

模型代码与交易客户端分层；`main.py` 只暴露受限 API，前端只操作候选、人工晋级和短时 permit：

| 文件 | 当前接口 | 安全职责 |
|---|---|---|
| `backend/src/okx_demo_lab/ml/strategy.py` | `FrozenModelBundle`、`build_order_proposal` | 只接受规范 JSON 数据模型；内容寻址；完整特征模式；已确认 K 线；固定 BTC-USDT、Demo、限价；v0.2 运行时只生成固定 10 USDT BUY，SELL 拒绝 |
| `backend/src/okx_demo_lab/ml/walk_forward.py` | `plan_walk_forward`、`run_walk_forward`、`fit_linear_model` | 外层测试窗互不重叠；训练标签跨度和 embargo 与测试隔离；只在训练窗拟合标准化；只在 flat 时 BUY，固定 horizon 后理论退出，持仓期忽略信号，SELL 不计空头收益，验证强制计入双边正成本 |
| `backend/src/okx_demo_lab/ml/registry.py` | `register_candidate`、`record_validation`、`promote`、`load_champion` | 独立 SQLite；模型、数据、配置、验证报告四重哈希绑定；人工短语；审阅说明；事务和 generation CAS；晋级前重验 artifact |
| `backend/src/okx_demo_lab/ml/execution.py` | `authorize_demo_session`、`DemoAutoExecutor.execute`、`AutomationLedger` | 10 分钟内仅 1 笔、总额最多 10 USDT、BUY-only；绑定 champion 代次/哈希；持久单次 signal claim；只调用 `preview/commit`；未知 commit 禁止自动重试 |
| `backend/src/okx_demo_lab/ml/pipeline.py` | OKX K 线解析、特征、训练与候选注册 | 只接受已确认连续 5m K 线；固定特征/标签窗/成本；训练完成绝不自动晋级 |
| `backend/src/okx_demo_lab/ml/runtime.py` | `MLCoordinator` | 默认网络空闲；显式短时授权后才读取当前行情并调用既有 `TradingService` |
| `backend/tests/test_ml_pipeline.py` | 定向离线测试 | 覆盖泄漏间隔、确定性、成本、long-only 非重叠资本、AlwaysSell 无空头收益、篡改、人工晋级、CAS、BUY-only 单次提交与未知结果停机 |

当前 `FrozenLinearModel` 是不依赖第三方反序列化器的可执行安全基线，用于把真实公共数据、冻结、验证、晋级和执行契约跑通；它不是“高收益模型”，也不应依据测试夹具或单一时间段表现晋级实际 champion。

## 后续可选的非线性研究实现

若后续新增 `backend/src/okx_demo_lab/ml/hgb_train.py`，应仅由本机离线训练任务调用，不提供“每根 K 线在线自改模型”API：

```python
def train_candidate(
    dataset_manifest: DatasetManifest,
    feature_frame: DataFrame,
    config: HgbTrainingConfig,
) -> CandidatePackage: ...

def validate_candidate(
    feature_frame: DataFrame,
    config: HgbTrainingConfig,
    split: WalkForwardConfig,
) -> ValidationReport: ...
```

训练器建议使用 `sklearn.ensemble.HistGradientBoostingRegressor`，预测下一固定 horizon 的净收益或超额收益。外层使用 `TimeSeriesSplit(gap=label_horizon)`；每个外层测试窗只能评估一次。超参数必须在外层测试之前冻结，若需搜索，只能在每个外层训练窗内部再做时间序列拆分。标准化、缺失值处理、特征选择和阈值拟合都必须位于训练窗内。

建议以后单独增加可选依赖组，而不是让基本交易终端强制安装研究栈：

```toml
[project.optional-dependencies]
ml = ["numpy", "pandas", "scikit-learn", "skops"]
```

禁止 `pickle` / `joblib` 直接加载外部文件。HGB 产物应选一种并固定：

1. `skops`，加载前检查未受信任类型并与代码中的精确类型白名单比对；或
2. 经预测一致性测试的非可执行格式。

产物必须限制大小、存入内容寻址存储、记录依赖锁哈希，并在注册、晋级和每次进程加载时重新计算 SHA-256。没有通过安全格式和预测一致性测试前，HGB 只能生成离线验证报告，不能进入自动执行进程。

## 数据与验证 schema

### `dataset_manifest.json`

```json
{
  "datasetId": "ds_<sha256-prefix>",
  "contentSha256": "sha256",
  "schemaSha256": "sha256",
  "source": "OKX public completed candles",
  "instrument": "BTC-USDT",
  "bar": "5m",
  "startsAt": "UTC",
  "endsAt": "UTC",
  "rowCount": 10000,
  "labelHorizonBars": 3,
  "featureNames": ["..."],
  "missingValuePolicy": "frozen-id",
  "createdAt": "UTC"
}
```

要求：只用已确认 K 线；时间戳严格递增且去重；保留原始快照哈希；标签窗口不能跨入外层测试；若数据源、bar、特征或缺失值规则变化，则生成新 dataset ID。

### `validation_report.json`

现有 `ValidationReport` v2 固定保存：dataset、feature schema、training config、walk-forward spec、`long-only-fixed-horizon-non-overlapping` 评估模式、每折起止时间、窗口行数、非重叠 long 入场数、gross/net return、双边成本假设、最大回撤、最差折和整体哈希。只在 flat 时接受 BUY；固定持有 `label_horizon` 根已完成 K 线后，使用与标签相同的窗口收益结算并回到 flat；持仓期间忽略新信号；SELL 只表示 flat，不计任何空头收益；不能在同一外层测试折内完成理论退出的尾部信号不参与诊断。建议再增加：

- buy-and-hold / always-hold 基线；
- 延迟、maker/taker 费率和压力滑点三组情景；
- 各折收益贡献集中度；
- 特征漂移与缺失率；
- 最终保留的、从未参与调参的验收区间；
- 软件依赖锁哈希和训练命令参数哈希。

晋级阈值只是拒绝明显不稳候选的门，不是盈利保证。旧 v1 long/short 重叠报告仍可读取以保留审计链，但会以 `unsupported_evaluation_semantics` 拒绝晋级；只有上述 v2 语义可通过该门。阈值版本本身也要哈希并进入 promotion 记录。

## 人工 champion 晋级

当前注册表是独立的 `%LOCALAPPDATA%\Tideguard\ml-registry.sqlite3`，建议保持以下状态机：

```text
candidate → validated → champion → retired
           ↘ rejected
```

只有 `promote` 能更新 champion。它要求：artifact、manifest、validation report 哈希一致；dataset/config/schema 与 manifest 绑定；所有门通过；人工 reviewer、说明、确认短语和预期 generation 均存在。训练完成或新报告写入绝不能自动调用 promote。

晋级新 champion 后，旧入场许可因 champion generation 不匹配立即失效。晋级本身不启用交易；操作员还需单独创建短时 Demo 单次入场许可。

## 当前集成 API

训练使用公共数据并在本机离线计算；HTTP API 不接受任意数据路径、代码、模型类或交易所 URL：

| 方法与路径 | 请求/响应要点 | 额外控制 |
|---|---|---|
| `GET /api/v1/ml/status` | 候选、验证摘要、champion、permit、执行摘要 | 不返回 artifact BLOB、路径、特征全集或敏感账户数据 |
| `POST /api/v1/ml/train` | 固定 BTC-USDT/5m 公共数据训练候选 | 1600–5000 根；不会自动 promote 或交易 |
| `POST /api/v1/ml/promote` | `modelId`、`confirmation`、`reviewer`、`rationale`、`expectedGeneration` | 现有 CSRF；BEGIN IMMEDIATE；审计链追加 |
| `POST /api/v1/ml/automation/authorize` | TTL、最大订单数、最大总名义额、确认短语 | Demo-only；TTL ≤ 600 秒；固定订单上限 1；总额 ≤ 10 USDT；BUY-only；无自动退出 |
| `POST /api/v1/ml/automation/stop` | 无敏感参数 | 先清除进程内 permit，立即调用 `emergency_stop`；本地撤销/审计失败不能阻断急停 |

不要增加以下 API：任意 artifact 文件路径、任意 Python 类名、任意交易所 URL、任意 signal 直接提交、在线代码/参数修改、正式盘切换、自动 promote。

### 与现有服务的最小适配

`DemoAutoExecutor` 的 `TradingPort` 与现有方法形状一致：

```python
preview = await trading_service.preview(OrderDraft(...))
result = await trading_service.commit(intent_id, digest, deterministic_idempotency_key)
```

模型执行层不调用 `OkxClient`。现有服务仍会重新读取行情、余额和挂单，执行精度、偏离、额度、账户身份、arming、CAA 租约、审计完整性和真正 HTTP 前 dispatch guard。自动层不得复制或弱化这些检查。当前运行时只允许一次 BUY 入场；它不核验或维护模型库存，也不自动提交 SELL。人工退出后的实际结果应单独核对，不能拿 OOS 理论 round-trip 指标代替。

## FreqAI 可选适配边界

不把 Freqtrade/FreqAI 连接到 OKX sandbox，也不让它持有 Tideguard 的 API 凭证。若保留兼容性，仅新增独立 `ml/freqai_adapter.py`，从另一个本地进程读取 analyzed dataframe 的窄 JSON 投影：

```json
{
  "schemaVersion": "tideguard.freqai-signal.v1",
  "observedAt": "UTC",
  "candleClosedAt": "UTC",
  "candleConfirmed": true,
  "instrument": "BTC-USDT",
  "modelId": "external frozen id",
  "modelSha256": "sha256",
  "featuresSha256": "sha256",
  "score": 0.0
}
```

适配器只允许 `http://127.0.0.1:<固定端口>` 或受控本机 IPC；禁止重定向、代理和用户输入 URL；限制响应体、超时、字段和数值范围；校验已完成 K 线与新鲜度；只输出建议意图。它不复制 GPL 源码、不随 Tideguard 打包 Freqtrade，也不向 FreqAI 暴露 `preview/commit` 或 OKX 凭证。若未来分发两个组件，许可证边界仍需单独审查，不能仅凭进程隔离作法律结论。

## 上线前测试清单

### 数据与训练

- 时间戳乱序、重复、未来 K 线和未确认 K 线均拒绝。
- feature/label 每个变换器只在训练窗拟合。
- `gap >= label_horizon`，外层测试窗互不重叠；测试行从不进入训练或调参。
- 固定 seed、依赖锁、数据哈希和配置哈希时报告可复现。
- 空特征、NaN/Inf、极值、缺失率漂移和 schema 漂移 fail closed。
- HGB 安全序列化允许类型清单、artifact 大小与预测一致性测试。

### 晋级与注册表

- artifact、manifest、dataset、config 或 report 任一字节变化都拒绝。
- 未验证、门未通过、确认短语错误、陈旧 generation、空 reviewer/说明均拒绝。
- 两个进程并发 promote 只能有一个成功。
- 失败晋级不改变旧 champion；新 champion 使旧 permit 失效。
- 注册表损坏或不可读时不加载模型、不创建 permit、不发建议单。

### 模拟盘执行

- 模型不能改变 environment、instrument、order type、risk policy 或 fixed notional；许可只能派发 1 笔、总额最多 10 USDT 的 BUY。
- SELL 在 proposal 和 executor 两层均 fail closed；v0.2 不宣称自动退出或长期闭环执行。
- 陈旧行情、未完成 K 线、非 champion、artifact hash 不一致、permit 过期/撤销/预算耗尽均拒绝。
- 同一 signal 的并发与重启重放只 claim 一次；idempotency key 保持确定。
- preview 拒绝时 commit 次数为零。
- commit 超时、取消、畸形响应或未知状态时不自动重试，进入 manual review/现有急停路径。
- 自动 stop 先清除进程内 permit 并无条件触发现有急停，再在 `finally` 中尽力持久撤销 permit 和追加审计；本地持久化失败不能让进程内许可继续派发。
- 用真实 OKX 模拟盘做受控 smoke test 前，保持默认 observe，并由人工确认每一步；测试结果不能被称为实盘能力或盈利能力。
