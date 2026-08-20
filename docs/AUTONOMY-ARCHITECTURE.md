# 墨衡 v0.4 长期自动量化契约

目标是长期运行、可证据化升级的 **OKX Demo、BTC-USDT、cash SPOT** 自动量化系统；优化的是扣成本、受限风险下的前瞻证据，不承诺未来收益。OKX Live 在同一应用中有独立连接和人工限时交易能力，但 v0.4 不允许 AI/Codex 自动下 Live 订单。

## 四个隔离平面

```text
公共数据仓库
  -> candidate + snapshot/split/config hashes
  -> Codex Supervisor 脱敏审查
  -> champion registry / rollback
  -> deterministic execution service
  -> OKX Demo or guarded manual OKX Live
```

1. **数据/研究平面**只访问 OKX 公共行情，构建不可变 snapshot、滚动 walk-forward 和未来 Shadow；不读取任何账户凭证。
2. **监督平面**只读取模型/报告/策略哈希和脱敏指标。Codex 可审查、晋级、拒绝、暂停或回滚 Demo champion，但不能选择环境、解除急停或扩大风险。
3. **执行平面**只接受冻结 champion、有效 Demo lease、用户 Demo master 和确定性风控；模型只能产生意图。
4. **环境治理平面**管理 Demo/Live 独立凭证、账户指纹、状态目录、请求头和切换 challenge；它拥有比模型更高的否决权。

## 自我更迭不是在线自改

```text
scheduled history sync
  -> immutable snapshot
  -> three validated candidates
  -> prospective shadow
  -> paired champion-recipe comparison
  -> Codex decision
  -> flat-position generation CAS
  -> Demo canary
  -> retain / suspend / rollback
```

禁止模型在运行时修改源码、特征、标签、验证门、风险阈值、品种、账户权限、交易环境或资金规模。新模型只以不可执行严格 JSON artifact 出现；训练故障保留旧 champion，不会覆盖。

## Demo master 与 Codex lease

- 新安装默认 `disabled`。用户显式启用 Demo master 后，状态绑定 Demo API Key 指纹和 OKX uid/mainUid 指纹。
- Codex lease 最长 24 小时；每笔 entry/exit 仍产生新的短时 supervisor arm，并绑定 decision ID 和用途。
- lease 过期、模型/策略/账户身份变化、数据或审计异常、Shadow/Demo 风险门失败时，停止新开仓。
- 关闭 master 立即禁止新 entry；已确认属于模型的库存仍保留风险降低型 exit 管理。
- Live profile 的 supervisor CLI、arm_supervised 和 long-run enable 均硬拒绝，不能复用 Demo 决策。

## 订单与模型库存

```text
flat
  -> entry_submitted
  -> entry_unfilled | long | manual_review
long
  -> exit_submitted
  -> long(partial) | closed | manual_review
```

- 自动订单为限价 IOC；提交响应不是成交证明，系统逐笔 GET order。
- `accFillSz` 是库存数量来源。BTC 手续费减少模型净库存；USDT 费用进入净结果。
- SELL 先按 lot size 向下对齐，且不能超过该 position 的 remaining size；账户原有 BTC 不属于模型。
- 持仓默认 12 根 5m，另有固定止损、止盈和最大持有时间；模型换代不改写已有 position 的退出计划。
- 小于最小卖出量的残余保留数量、急停并人工核对，不记作已平仓。
- 模糊下单、无法核对终态、身份错配或持久化异常均 kill + manual review；未知提交绝不自动重试。

## 环境切换线性化

最终确认阶段在首个异步网络复核前，先设置进程单调的 `transitionPending` dispatch gate，并立即 signal/persist 当前 kill。该 gate 会被：

- `arm`
- `preview`
- `commit`
- 最终 `_dispatch_guard`

共同检查。已经进入真实 HTTP 的不可逆请求会先完成；切换流程等待 trade lock，再重新检查本地意图、模型持仓和当前/目标账户的全部 SPOT pending orders（不限定交易对）。只有零 blocker、零 pending、两侧 kill 已持久化时才写 selector。最终核对失败时 gate 和 kill 保留到重启，不能自动重新开放交易。

## Live 人工交易

- Live 使用独立 Key、状态 DB、tag 和风险策略；私有请求明确不发送模拟头。
- API Key 必须 Read+Trade、禁 Withdraw、绑定 IP，账户为 Spot mode。
- 每次启动 Live 都先持久急停；解除急停需逐字输入 `解除实盘急停` 并完成订单终态核对。
- 人工 arm 需输入 `我确认使用真实资金`，只生效 60 秒；单笔最多 10 USDT、最多一个挂单、且不超过权益 0.05%。
- arm 初始化必须确认 CAA；凭证轮换、审计异常、deadman 失效或请求歧义均停止执行。

## 暂停与回滚

以下任一条件停止新开仓：

- rolling Demo 回撤越过硬上限；
- 订单未知、模型库存异常或账户身份变化；
- snapshot 缺口/冲突、报告口径不匹配、审计链损坏；
- Shadow 或 paired comparison 不满足门；
- Codex lease 过期/拒绝；
- 环境切换开始或 selector 状态无效。

只有空仓、没有未决订单且候选通过全部确定性与前瞻门时，Codex 才能晋级或回滚。它只改变未来 entry 模型，不改变已有持仓。

## 长期 Windows 进程

单实例 `--daemon` 固定独占 `127.0.0.1:8791`，运行训练调度、持仓恢复、CAA 和本地 API。UI 可连接 daemon；关闭 UI 不终止后台。安装器始终提供“启动/停止墨衡后台服务”入口，并提供默认不勾选的当前用户登录自启动选项（受 Windows 安全策略约束）；它不会替用户启用 Demo master、解除 kill、切换 Live 或配置凭证。

## 推荐上线顺序

1. 离线故障注入、历史回放和构建自检。
2. 全历史 OOS 与公共 Shadow，零私有请求。
3. Demo 账户只读核验。
4. 用户启用 Demo master，10 USDT canary 与闭环退出。
5. 积累至少一个完整前瞻评估窗口，持续由 Codex 审查、暂停或回滚。
6. Live 只做用户显式的人工限时交易；未来若开放自动化，必须另建实盘资金预算、canary、授权和审计协议。
