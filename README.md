# 潮汐台 Tideguard

[![Offline checks](https://github.com/mikutea/tideguard-okx-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/mikutea/tideguard-okx-demo/actions/workflows/ci.yml)

一个只连接 **OKX 模拟盘** 的本地现货研究终端。程序默认处于观察模式；正常重启不保留下单授权，持久急停或未决订单则继续保持锁定。原始 API Key、Secret 与 Passphrase 只由 Windows Credential Manager 保存，不进入前端、项目文件、SQLite 或日志；SQLite 仅保存 API Key 与 OKX UID 的不可逆 SHA-256 身份指纹，用于阻止换账户核对订单。

当前版本是手工下单、确定性风控与审计底座，**尚未集成模型训练、自动策略执行或 FreqAI 适配器**。

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

## 验证

```powershell
.\scripts\check.ps1
```

该命令运行后端单元测试、前端类型检查与生产构建，并扫描项目中不应出现的凭证字段赋值。它不会调用私有 OKX API，也不会发出订单。

## 目录

```text
backend/   FastAPI、OKX Demo 客户端、确定性风控、SQLite 审计
frontend/  React + Vite 响应式界面
scripts/   Windows 安装、启动与检查脚本
docs/      视觉概念与设计说明
```

运行时数据位于 `%LOCALAPPDATA%\Tideguard\`，不位于项目目录。

公开仓库：<https://github.com/mikutea/tideguard-okx-demo>

## 审计与验收材料

- [GitHub AI 量化项目审计](docs/GITHUB-AUDIT.md)
- [视觉保真记录](docs/FIDELITY-LEDGER.md)
- [验证记录](docs/VERIFICATION.md)
- [桌面端截图](docs/tideguard-desktop.png)
- [移动端截图](docs/tideguard-mobile.png)
