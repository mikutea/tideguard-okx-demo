# Tideguard v0.3 长期自动量化契约

本文件冻结 v0.3 的实现边界。目标是长期运行的 **OKX Demo-only、BTC-USDT、cash SPOT** 自动量化系统；它优化可验证的风险调整后结果，但不承诺未来收益。

## 三个隔离平面

```text
公共行情/训练平面
  -> candidate + 内容哈希 + walk-forward/压力报告
  -> Codex Supervisor 脱敏审查决策
  -> champion registry（可回滚）
  -> 确定性执行平面
  -> OKX Demo
```

1. **研究平面**只能访问公共行情。它按计划训练新 candidate、生成固定数据快照哈希、执行无前视的 walk-forward、24 bps 成本压力诊断和 shadow 记录。训练失败绝不能影响当前 champion。
2. **监督平面**只读取脱敏模型元数据、验证报告、shadow/live 统计、代码版本和风控哈希。Codex 可以批准或拒绝 candidate、续发执行 lease、暂停系统和回滚 champion；它不能读取原始 API Secret，不能改交易环境、品种、资金上限或 kill switch。
3. **执行平面**只接受已冻结 champion、有效 supervisor lease 和用户预先启用的 Demo master switch。最终订单仍必须经过 TradingService 的身份绑定、预检、幂等提交、CAA 和急停门。

## 自我更迭不是在线自改

允许的模型更迭流程：

```text
scheduled train
  -> validated candidate
  -> shadow observations
  -> deterministic gates
  -> Codex decision
  -> flat-position promotion
  -> canary Demo allocation
  -> monitor / rollback or retain
```

禁止模型在运行时修改源代码、特征定义、标签、风控阈值、交易品种、账户权限或资金规模。新模型只以不可执行的规范 JSON artifact 出现；晋级采用 generation CAS，并保留上一 champion 用于回滚。

## Demo master switch 与监督 lease

- 新安装默认 `disabled`。用户显式启用后，该选择与账户指纹会跨 UI/daemon 重启持久保存，直至用户关闭；每笔订单仍需重新获得短时 supervisor arm。
- 用户在开始模拟盘测试时只需一次显式启用；此动作绑定当时的 API Key 指纹和 OKX UID 指纹，但不保存秘密。
- Codex Supervisor lease 最长 24 小时。后台每次准备新开仓前都重新检查 lease、champion、账户身份、审计链和硬风控。
- lease 过期、Codex 拒绝、模型过期、数据漂移、审计异常或账户身份变化时，系统停止新开仓。
- 已确认属于 Tideguard 的持仓仍允许执行风险降低型退出；退出不能卖出账户原有 BTC。

## 订单与持仓状态机

每个持仓都绑定 entry model、policy、账户身份和实际累计成交量：

```text
flat
  -> entry_submitted
  -> entry_unfilled | long | manual_review
long
  -> exit_submitted
  -> long (partial exit) | closed | manual_review
```

- 自动订单使用 `IOC limit`，剩余未成交部分由交易所立即取消；系统仍逐笔 GET order 确认终态，不能把提交响应当成交。
- `accFillSz` 是唯一持仓数量来源。零成交不创建持仓；部分成交只记录实际成交量。
- SELL 数量不得超过该 position 的 `remaining_size`，并在交易前再次核对同一账户可用 BTC。
- 默认按 12 根已完成 5 分钟 K 线退出；同时有固定止损、止盈和最大持仓时间。历史 OOS 与未来 shadow 使用相同 bracket，并在提前退出后保留原 12 根资本冷却窗。模型更换不能改写已有持仓的退出计划。
- 不确定下单、无法查询终态、数量不一致或持久化失败都会触发 kill + `manual_review`，禁止自动重试未知提交。

## 暂停与回滚

以下任一条件停止新开仓并保持退出管理：

- rolling Demo 回撤超过硬上限；
- 订单身份不一致、未知提交或持久状态异常；
- shadow 表现不再满足下一次 lease 的门槛；
- 训练/验证数据不连续、时间戳异常、审计链损坏；
- Codex lease 过期或明确拒绝。

只有在空仓、没有未决订单且候选通过全部确定性门槛时，Codex 才能晋级或回滚。回滚由周期 Codex 监督任务基于脱敏证据决定，不由模型自行触发；它只改变未来入场模型，不会改变已有持仓。

## 长期进程

Windows 包包含一个单实例 `--daemon` 模式：固定独占 `127.0.0.1:8791`，运行训练调度、持仓恢复、CAA 和 API。UI 可连接已有 daemon；关闭 UI 不终止 daemon。安装器默认创建当前用户登录自启动任务，但不替用户开启 Demo master switch。Codex 决策由独立的本机周期监督任务写入内容寻址决策表。

## 上线顺序

1. 离线故障注入和历史回放。
2. 公共数据 shadow，零私有请求。
3. 配置 Demo 凭证并人工启动首个测试窗口。
4. 至少一个完整评估周期后才允许 Codex 续发长期 lease。
5. 本版本不包含正式盘路径。
