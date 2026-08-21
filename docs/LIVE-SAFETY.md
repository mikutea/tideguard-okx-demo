# 墨衡 v0.4 Demo / Live 安全边界

墨衡默认运行于 **OKX 模拟盘**。实盘是独立、默认锁死的运行环境，不是一个即时布尔开关。环境切换只改变下一次进程启动应加载的 profile；确认切换本身不会解除急停、启用下单或启用 AI 自动执行。

## 两个隔离环境

| 边界 | Demo | Live |
|---|---|---|
| Windows Credential Manager | `Tideguard.OKX.Demo` | `Tideguard.OKX.Live` |
| 私有请求头 | 强制 `x-simulated-trading: 1` | 明确不发送模拟头 |
| 订单 tag | `tideguarddemo` | `tideguardlive` |
| 交易状态库 | 兼容原 `%LOCALAPPDATA%\Tideguard` | `%LOCALAPPDATA%\Tideguard\live` |
| 单笔金额硬上限 | 25 USDT（自动策略仍更低） | 10 USDT |
| 最大挂单 | 3 | 1 |
| 人工限时授权 | 输入 `DEMO` | 输入 `我确认使用真实资金` |
| Codex / AI 自动执行 | 通过 Demo master + 独立 lease | v0.4 禁用 |

公共行情仓库不含账户数据，两个环境共享同一个版本化行情快照。模型不能读取 API Secret，也不能选择或改变环境。

## 切换协议

切换请求依次执行：

1. 服务端关闭当前自动化、撤销尚未派发的预检并停止本地授权。
2. 核对当前与目标环境的审计链、未决/未知意图、模型持仓和全部分页挂单。
3. 用目标环境的独立凭证读取 OKX `account/config`，绑定 API Key 指纹、`uid/mainUid` 账户指纹、权限和 IP 状态。
4. Live 必须包含 `read_only` 与 `trade`，不得包含 `withdraw`，必须绑定 IP，且账户模式必须为 Spot。
5. 服务端签发一次性 challenge；challenge 绑定源/目标环境、账户指纹和预检证据，只在 10 秒冷静期后、5 分钟内有效。
6. 用户完成四项风险确认并逐字输入服务端短语；确认时服务端重新执行完整预检。状态变化或 challenge 重放一律拒绝。
7. 目标环境先落盘 `automation disabled + kill active`，再原子写入下一次启动 profile。
8. 重启后仍处于观察/急停；只有再次核对订单并使用该环境专属短语，才能获得短时人工下单授权。

任一步结果未知都保持 fail closed。前端颜色、倒计时和复选框只是说明层，不能替代服务端检查。

## OKX 权限事实

OKX 官方将 API 权限分为 Read、Trade、Withdraw。Trade 不只代表下单/撤单，还可包含资金划转和配置类写操作，因此“禁 Withdraw”不能替代独立子账户、IP 白名单和资金上限。官方同时说明 Demo 与 Production 使用相同 REST 主机；Demo 请求必须增加 `x-simulated-trading: 1`。

- [OKX API guide：Production / Demo 服务与模拟头](https://www.okx.com/docs-v5/en/)
- [OKX API guide：API 权限与 IP 安全](https://www.okx.com/docs-v5/en/)

## 本版本明确不做

- 自动测试、CI、构建或安装过程绝不访问真实私有 API，也不会发送实盘订单。
- Demo 的 Codex lease 不能在 Live 复用。
- Live 不接受后台 AI 自动入场；先用全历史研究、shadow 和 Demo 成交证据验证。未来若开放，需要新的实盘专属授权、资金预算、前瞻 canary 门槛与单独审查。
- 当前只读 Live readiness 明确要求同一 champion 的 90 天/100 笔前瞻 Shadow、30 笔 Demo 闭环、两侧正净结果及不高于 3% 的回撤；通过这些条件仍不会启用 Live AI，只会把“证据不足”和“部署能力禁用”两个问题分开显示。
- 环境切换不接受条款、开通产品、修改 KYC、安全设置或 API 权限；这些始终由用户在 OKX 完成。
