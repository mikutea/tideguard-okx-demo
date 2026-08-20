# 潮汐台 Tideguard

[![Offline checks](https://github.com/mikutea/tideguard-okx-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/mikutea/tideguard-okx-demo/actions/workflows/ci.yml)

Tideguard 是一个仅连接 **OKX 模拟盘** 的 Windows 本地现货量化终端。v0.3 提供常驻后台、定时训练、未来影子验证、Codex 监督晋级、IOC 自动入场、实际成交库存跟踪，以及止损、止盈和定时自动退出。

它的目标是持续寻找并验证更稳健的候选模型，而不是承诺收益。模型不能在线改代码、风险阈值、交易品种或资金规模；每次更迭都产生新的冻结 JSON artifact，只有通过样本外、未来 shadow、相对 champion 改善和 Codex 内容哈希审查后，才可影响未来订单。

## 固定安全边界

- 所有交易请求固定发送到 `https://openapi.okx.com`，并强制携带 `x-simulated-trading: 1`；没有正式盘切换路径。
- 只允许 `BTC-USDT / SPOT / cash`，零杠杆、无转账和提现端点。
- 自动订单固定为限价 IOC；提交响应不能代替逐笔终态查询。
- 每次自动入场名义额固定 10 USDT，同时最多一个模型持仓，每个 UTC 日最多三次入场。
- SELL 只能使用 Tideguard 按 OKX 实际累计成交和费用计算出的模型净库存，不能卖出账户原有 BTC。
- 自动止损 1.5%、止盈 2.5%、目标持有 12 根 5 分钟 K 线；验证和未来 shadow 使用相同的保守 bracket 语义及 24 bps 双边压力成本。
- 未知下单结果、账户身份变化、订单回显不一致、审计损坏或不可交易残余会触发持久急停和人工核对，未知提交绝不自动重试。
- 用户关闭长期 Demo master 后立即禁止新开仓；已确认属于模型的持仓仍进入退出管理。
- Codex 的监督授权只绑定一笔订单、一个用途和一个决策 ID，不能被浏览器手工单或旧预检借用。
- 原始 API Key、Secret 和 Passphrase 仅存入 Windows Credential Manager；SQLite 只保存不可逆的 API Key/OKX UID 身份指纹。
- 本地服务只监听 `127.0.0.1:8791`，并强制单后端实例、单 Uvicorn worker。

> 这是模拟盘研究和工程验证软件，不是盈利承诺、投资建议或实盘代炒工具。

## Windows 桌面版

普通用户可从 [GitHub Releases](https://github.com/mikutea/tideguard-okx-demo/releases/latest) 下载 `Tideguard-Setup-*.exe`。安装器默认创建当前用户登录自启动的后台 daemon；关闭桌面窗口不会停止训练、shadow 或已有持仓退出。安装器不会自动启用 Demo master，也不会打包任何凭证或本地数据库。

开始菜单提供三个入口：

- **Tideguard**：打开桌面界面；
- **Tideguard 凭证管理**：把 OKX Demo 凭证写入当前用户的 Windows Credential Manager；
- **停止 Tideguard 后台服务**：停止常驻 daemon。

源码构建发行包：

```powershell
.\packaging\build-release.ps1
```

产物采用 PyInstaller `onedir` 和 Inno Setup，详见 [打包说明](packaging/README.md)。

## 从源码运行

前置条件：Python 3.11、Node.js 22 和 Corepack。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\.venv\Scripts\python.exe -m okx_demo_lab.cli credentials set
.\scripts\run.ps1
```

源码入口使用项目根目录下已忽略的 `.local-data\`；安装版使用 `%LOCALAPPDATA%\Tideguard\`。原始秘密不得粘贴到聊天、截图或项目文件。

## 长期模型流水线

后台每天从 OKX 公共接口获取 10,000 根已完成且连续的 `BTC-USDT 5m` K 线，训练三组确定性的线性逻辑 challenger。当前基线故意使用严格 JSON 和纯数据权重，而不加载来自网络的 `pickle`、`joblib` 或“高收益模型”。

晋级链为：

```text
定时训练
  → label horizon + embargo 的外层 walk-forward
  → long/flat、bracket、非重叠资本、24 bps 成本诊断
  → 至少 7 天且 20 个结算 BUY 的未来 shadow
  → 相对当前 champion 的 OOS 改善门
  → Codex 脱敏证据审查与 generation CAS
  → 最长 24 小时执行 lease
  → 10 USDT Demo canary
  → 净成本结果监测、暂停或回滚
```

Codex Supervisor 命令只输出/接收模型哈希、验证指标和状态，不读取 OKX Secret：

```powershell
.\.venv\Scripts\python.exe -m okx_demo_lab.cli supervisor review
```

旧版浏览器人工晋级和一次性 BUY permit 已返回 `410 Gone`。Freqtrade/FreqAI 不随安装器捆绑；如以后接入，只能作为独立 localhost 公共信号源，不能持有 Tideguard 凭证或直接下单。详细契约见 [长期自动量化架构](docs/AUTONOMY-ARCHITECTURE.md) 与 [模型架构](docs/ML-ARCHITECTURE.md)。

## 验证

```powershell
.\scripts\check.ps1
```

该命令执行后端测试、前端类型检查、生产构建和秘密扫描，不调用 OKX 私有 API，也不发订单。模拟盘端到端测试只有在用户显式启用 master 后才开始。

## 目录

```text
backend/   FastAPI、OKX Demo 客户端、风控、模型、监督与 SQLite 状态
frontend/  React + Vite 响应式桌面界面
desktop/   pywebview 桌面宿主、后台 daemon 与凭证管理窗
packaging/ PyInstaller、Inno Setup 和 GitHub Release 构建
scripts/   Windows 安装、启动与离线检查脚本
docs/      架构、验证和设计记录
```

公开仓库：<https://github.com/mikutea/tideguard-okx-demo>
