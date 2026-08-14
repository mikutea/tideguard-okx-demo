# 潮汐台 Tideguard

[![Offline checks](https://github.com/mikutea/tideguard-okx-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/mikutea/tideguard-okx-demo/actions/workflows/ci.yml)

一个只连接 **OKX 模拟盘** 的本地现货研究终端。程序默认处于观察模式；正常重启不保留下单授权，持久急停或未决订单则继续保持锁定。原始 API Key、Secret 与 Passphrase 只由 Windows Credential Manager 保存，不进入前端、项目文件、SQLite 或日志；SQLite 仅保存 API Key 与 OKX UID 的不可逆 SHA-256 身份指纹，用于阻止换账户核对订单。

当前版本同时提供手工下单底座与受控模型链：公共 K 线离线训练、带标签隔离和 embargo 的 walk-forward、内容寻址冻结模型、人工 champion 晋级，以及一次性的 OKX Demo 模型 BUY 入场试运行。训练结果不等于未来收益，模型不能绕过确定性风控。

## 安全边界

- REST 主机固定为 `https://openapi.okx.com`，所有请求强制携带 `x-simulated-trading: 1`。
- 交易品种固定为 `BTC-USDT`，交易模式固定为 `cash`，首版只接受限价现货单。
- 前端不能传入 API 路径、交易模式、客户端订单号或环境标志。
- 下单采用 `预检 → 明确确认 → 提交`；服务端重启后回到观察模式。
- 每次限时授权绑定同一组 API Key 与 OKX 账户 UID；预检、提交、回查、急停与复位必须保持身份一致。
- SQLite 事务保证同一运行状态只能派发一个账户身份的潜在订单；多账户或未知身份状态会保持急停锁定。
- 当前部署模型限定为单个后端实例、单个 Uvicorn worker；不要并行启动多个 Tideguard 服务进程共享同一账户。
- 私有请求超时不会盲目重试下单，而是按 `clOrdId` 查询并锁定等待核对。
- 未决订单只有在同一账户逐笔查询且确认进入终态后，才允许解除急停。
- 急停只阻止新单并尝试撤销本程序挂单，不会逆转已成交结果。
- 本地服务仅监听 `127.0.0.1`。
- 模型自动执行默认关闭；只有通过样本外门槛、人工逐字确认晋级、已启用演练且再次逐字授权短时 permit 后才会运行。
- v0.2 自动许可最长 10 分钟、仅允许 1 笔、总名义额最多 10 USDT，并硬拒绝 SELL；它只做一次 Demo BUY 入场，不会自动退出，成交后的平仓需人工处理。未知提交结果不自动重试。

> 这是模拟盘测试程序，不是盈利承诺、投资建议或实盘代炒工具。

## 快速开始（Windows PowerShell）

前置条件：Python 3.11 或更高版本、Node.js 22 或更高版本，以及已可用的 Corepack。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\.venv\Scripts\python.exe -m okx_demo_lab.cli credentials set
.\scripts\run.ps1
```

然后打开 <http://127.0.0.1:8791>。`5173` 只用于运行 `.\scripts\run-dev.ps1` 时的前端开发服务器。

凭证设置命令会在本机终端中逐项隐藏输入。不要把 API Key、Secret 或 Passphrase 粘贴到聊天、截图或项目文件中。

在其他地方“已保存”的凭证不会被本程序自动导入；请只在本机项目根目录运行上面的隐藏输入命令。

## Windows 桌面版

普通用户可从 [GitHub Releases](https://github.com/mikutea/tideguard-okx-demo/releases/latest) 下载 `Tideguard-Setup-*.exe` 一键安装，或使用便携 ZIP。桌面版使用 WebView2 打开并独占固定的 `127.0.0.1:8791`，从而与官方源码启动入口互斥；原始凭证仍只保存到 Windows Credential Manager。

安装完成后可从开始菜单打开 **Tideguard 凭证管理**，无需 Python 或控制台即可设置/删除当前 Windows 用户的 OKX Demo 凭证。

从源码构建安装器：

```powershell
.\packaging\build-release.ps1
```

构建产物是 PyInstaller `onedir` + Inno Setup 安装器，不承诺真正的“单文件免解压”运行；这样更容易验证原生依赖并减少启动时临时释放风险。详见 [打包说明](packaging/README.md)。

## 模型实验室

在“策略实验室”中点击“训练新候选”只会下载 OKX 公共、已完成的 `BTC-USDT 5m` K 线并在本机训练，不读取私有凭证。候选验证采用固定周期、long-only、持仓期不重叠的样本外研究诊断：只在 flat 时计一次 BUY，按固定 horizon 的理论退出收益和双边成本结算；SELL 不产生空头收益。该诊断是研究用的理论 round-trip，不是部署后收益，也与 v0.2 运行时“只入场、不自动退出”的单次 Demo BUY 语义不同。候选满足门槛后，仍要人工填写审阅说明并输入确认短语，才可成为 champion。

当前内置模型是可审计、严格 JSON 的线性逻辑基线，用于跑通可复现训练和安全执行契约；它不是“高收益模型”。可选 FreqAI 边界只允许独立 dry-run 进程通过 localhost 提供冻结信号；Freqtrade/FreqAI 不随安装器捆绑，也不持有 OKX 凭证。详见 [模型架构](docs/ML-ARCHITECTURE.md)。

## 验证

```powershell
.\scripts\check.ps1
```

该命令运行后端单元测试、前端类型检查与生产构建，并扫描项目中不应出现的凭证字段赋值。它不会调用私有 OKX API，也不会发出订单。

## 目录

```text
backend/   FastAPI、OKX Demo 客户端、确定性风控、SQLite 审计
frontend/  React + Vite 响应式界面
desktop/   pywebview 桌面宿主与固定回环端口启动器
packaging/ PyInstaller、Inno Setup 与 Release 构建
scripts/   Windows 安装、启动与检查脚本
docs/      视觉概念与设计说明
```

安装版运行时数据位于 `%LOCALAPPDATA%\Tideguard\`。源码 `run.ps1` / `run-dev.ps1` 为便于项目整体迁移，使用项目根目录下已忽略的 `.local-data\`；两者都不会进入 Git。

公开仓库：<https://github.com/mikutea/tideguard-okx-demo>

## 审计与验收材料

- [GitHub AI 量化项目审计](docs/GITHUB-AUDIT.md)
- [视觉保真记录](docs/FIDELITY-LEDGER.md)
- [验证记录](docs/VERIFICATION.md)
- [桌面端截图](docs/tideguard-desktop.png)
- [移动端截图](docs/tideguard-mobile.png)
