# NautilusTrader 采用决策与隔离边界

## 决策

墨衡将 NautilusTrader 作为候选的事件仿真与交易执行内核，而不是现成的
盈利策略、模型训练器或桌面应用。采用顺序固定为：

1. 离线协议与供应链自检；
2. 修正后的 V6 合同与隔离 sidecar 的只读契约核对；
3. 公共行情与故障恢复验证；
4. 经单独授权的 OKX Demo 验收；
5. 正式 v2、长期前瞻证据和新的人工授权全部满足后，才重新评估 Live。

当前阶段不得读取 Windows Credential Manager，不得访问 OKX 私有 API，不得
创建、修改或撤销订单。NautilusTrader 不进入现有 Python 3.11 后端或桌面 EXE；
它运行在项目内 `.research-data/nautilus-poc/` 的独立 Python 3.12 环境中。

## 固定上游

- 项目：`nautechsystems/nautilus_trader`
- 版本：`2.0.0rc3`
- Python/平台：CPython 3.12.13 / Windows x86-64
- Wheel：`nautilus_trader-2.0.0rc3-cp312-cp312-win_amd64.whl`
- SHA-256：`8a90b01ccf66d78946c565bca08b7758bc7f312caf1ded1c2c2c710013a7c092`
- 许可证：LGPL-3.0-only

RC 只允许用于隔离研究和 Demo 候选验证。它不得控制真实资金；生产候选至少
等待正式 v2，并对新的精确版本重新执行供应链、Windows、回放、对账和故障测试。

## V6 执行合同与 V5 退役

审计发现，V5 的特征观察时间戳已经位于确认线收盘与下一根 K 线开盘的共同
时间边界，旧回放器却又施加 `latency=1`，使入场额外晚了一根 5 分钟 K 线。
因此 V5 报告和其中的收益数值只保留为历史审计材料，不再是 canonical 证据，
也不得用于晋级或作为 Nautilus 对照真值。

墨衡 `moheng.shadow.next-open-bracket.v2` 的执行合同是：

```text
确认的 5 分钟 K 线收盘产生决策
    == 下一根 K 线开盘的同一时间边界
    -> 对齐执行时钟 latency=0 入场
    -> 12 根后开盘退出
```

训练隔离使用 12-bar label horizon 和 1-bar embargo，共 13 bars purge + embargo。
这里的 `latency=0` 是墨衡已经对齐后的研究时钟语义，不能机械映射为
NautilusTrader 的某个回调或成交设置。当前 pinned sidecar 只验证供应链、协议、
导入边界和确定性；它尚未被声明为与 V6 逐笔等价。

在逐笔订单、入场索引、退出索引、费用、PnL 和 canonical digest 达到完全一致
之前：

- V6 合同是唯一允许实现的时间语义；当前报告已两次确定性复跑并通过结构/逐笔账本完整性 verifier，但该 verifier 不从冻结源数据重放，更不证明 Nautilus 执行等价；
- Nautilus 输出只能标记为 `research_only / promotable=false`；
- Nautilus 不得重写、覆盖 V5/V6 报告或为它们补记 Shadow 天数；
- 收益差异必须被解释，不能被表述为“框架提高了收益”。

## 进程边界

```text
React / PyWebView
        |
FastAPI 治理层
模型 artifact / 策略哈希 / 风险 / 用户确认 / 审计
        |
版本化 canonical JSON
        |
Nautilus Python 3.12 Sidecar PoC
当前仅离线协议与模型类型自检；后续才可能验证公共行情
```

Sidecar 只能接受已经冻结的研究输入。当前协议不包含凭证、账户、环境切换、
下单、改单、撤单、转账或提币字段。即使将来加入 Demo 执行端口，模型、品种、
仓位、环境和风险限额仍由墨衡治理层决定；缺少任何显式字段必须 fail closed。
当前 PoC 只使用公开研究数据，不访问 OKX 私有 API；它不会扩大现有仅
`BTC-USDT` 的执行白名单。

## 收益证据边界

GitHub stars、下载量、成交量、社媒截图和上游吞吐 benchmark 只能证明关注度、
采用率或软件速度。它们不能证明 Alpha、净收益、回撤或未来盈利。上游示例策略
一律视为教学候选，必须在墨衡相同的不可变数据、时间切分、成本、滑点和容量
口径上重新训练和评估。

任何第三方策略只有同时具备可复现源码和参数、point-in-time 数据、严格 OOS、
完整成本、逐笔结果以及足够长的前瞻验证，才可以进入 challenger 清单。它仍不能
自动注册为 champion 或触发订单。

## 后续准入门

进入公共行情阶段前必须满足：

- 官方 wheel 的文件名、大小和 SHA-256 验证通过；上游 Sigstore/intoto
  attestation 当前尚未由本项目验证，验证完成前不得进入公共行情阶段；
- Sidecar 在清空凭证环境变量、未授权网络使用且不导入网络适配器时可以完成确定性自检；操作系统层并未实施断网隔离；
- 相同输入重复运行的 canonical digest 一致；
- Sidecar 缺失、损坏、版本不匹配或输出非法时，墨衡保持原有研究能力且拒绝晋级；
- 所有输出留在项目内 `.research-data/nautilus-poc/`。

当前 PoC 已在固定 wheel 和精确 CPython `3.12.13` 上重复通过：协议自检只读取
distribution metadata，不导入 `nautilus_trader`；只有显式本地 Bar 物化测试才导入
`nautilus_trader.model` 并构造 1 根 Bar。三份证据中 `sidecar` 对象的 canonical
SHA-256 均为
`7c7dd4dec9ce505e88047c440348b71ff91509ead1e202864f0936ef0007480d`；
这不是协议响应字段 `summarySha256`，后者在相同输入下均为
`9ffaeaf644bbe4ddb1b62d45524ebf61b5944b28466e06e4bb768439539961a7`。
各次均为 `simulation=false / trades=0 / private API=0 / orders=0`，网络使用未授权、
网络适配器未导入、OS 断网隔离未实施，并固定输出
`NATIVE_BAR_FILL_PARITY_NOT_VALIDATED`。Nautilus wheel 已按文件名、大小和 SHA-256
锁定；每次执行前还逐文件核对安装目录与 wheel `RECORD`、安装后的 `RECORD`、
`direct_url.json` 和 setup-v3 状态，当前 101 个安装文件全部匹配。uv 管理的 Python
下载归档尚未由本项目独立锁定哈希，因此仍只允许研究。

进入 OKX Demo 前还必须满足：

- 仅 `BTC-USDT.OKX / SPOT / cash / ordinary MARKET or LIMIT`；
- Data 与 Execution 两侧均显式设置 `DEMO`，不得依赖上游默认的 `LIVE`；
- 禁止 risk bypass，保留墨衡的日亏损、回撤、租约和硬停止链；
- 断网、认证失败、行情 stale、未知 submit、重复 fill、部分成交、强杀重启、
  外部手工订单和持续 reconciliation 全部通过故障注入；
- 真正发送任何 Demo 订单前再次取得用户明确授权。

Live 门槛不因采用 NautilusTrader 而降低：同一 champion 仍需至少 90 天/100 笔
前瞻 Shadow、30 笔 Demo 闭环、两侧正净结果、回撤不高于 3%，再加正式 v2、
Windows 打包、许可证和极小额 canary 验收。实际资金仍需新的明确授权。
