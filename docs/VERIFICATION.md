# Tideguard v0.3 验证记录

验证日期：2026-08-20。本文只记录已实际运行的检查；历史回放、公共行情、安装验收和模拟盘私有订单验证严格区分。

## 离线与故障注入

- `scripts/check.ps1`：后端 `123 passed`，前端 TypeScript 检查与 Vite 生产构建通过。
- Python 3.11.15 发行环境再次运行后端 `123 passed`。
- 桌面宿主 `14 passed`；Windows 发行契约 `5 passed`。
- PyInstaller 使用 Python 3.11.15 生成 Windows x64 GUI `onedir`，冻结 EXE 的构建期 `--self-test` 返回 0。
- 测试覆盖模拟标头和端点白名单、签名、cash/tag、账户身份、CAA、限频、分页、超时歧义、clOrdId 回查、并发提交、急停/复位竞态、取消和重启恢复。
- v0.3 新覆盖 scheduled training 恢复、walk-forward v3、bracket 标签、未来 shadow、相对 champion 改善门、Codex applied 决策、稳定证据哈希、短时 scoped arm、最终 HTTP 前 master/generation/lease 门、IOC 部分成交、手续费净库存、自动退出、残余人工核对和持仓哈希篡改锁定。

唯一依赖告警是 Starlette 对旧 `httpx` TestClient 兼容层的弃用提示，不影响测试结果或运行路径。

## 真实 OKX 公共数据验证

本轮只调用无需凭证的 OKX 公共接口：

- history-candles 以每页 100 根、游标严格前进和保守限频取回 10,000 根 `BTC-USDT 5m` K 线；
- 10,000/10,000 根通过 9 字段、`confirm=1`、时间顺序和严格 5 分钟连续性检查；
- 三组 challenger 在真实数据上均完成 9 个外层 OOS 折和 4,500 个 OOS 行；
- 合成 10,000 根训练基准约 33.3 秒；安装版首次真实公共数据训练从 18:29:00 至 18:30:31 完成，期间 UI/daemon 健康。

安装版首次候选结果：

| 模型 | OOS 交易 | OOS 净结果 | 主要失败门 |
|---|---:|---:|---|
| `mdl_77cc2c714b1ffc389a2ea963` | 1 | 0.2456% | 交易数不足、净结果低于 0.5% |
| `mdl_3cb7e5eec38ac1b26ace5c1d` | 1 | 0.2456% | 交易数不足、净结果低于 0.5% |
| `mdl_2807267583cdf50654b3f3a8` | 0 | 0.0000% | 交易数不足、净结果低于 0.5% |

三者先进入 `validated` 以保留报告，随后本轮 Codex Supervisor 使用每次状态更新后的新 evidence hash，逐个写入 `reject` 决策；最终三者均为 `rejected`。没有 champion、没有 execution lease、没有交易。约 88% 的 long/flat 准确率主要来自 HOLD，未被当作收益证据。

## 浏览器验收

使用真实本机 FastAPI + Vite 生产资源、空凭证和临时数据目录验证：

- 桌面视口 `1280`：`clientWidth=scrollWidth=1265`，无页面级横向溢出；
- 移动视口 `390×844`：`clientWidth=scrollWidth=375`，6 个移动导航入口可见；
- “长期 AI 自动量化”页只存在 1 个主页面、1 个 master 卡、1 个持仓卡和 1 个 footer；
- 未配置凭证时，即使填入 `ENABLE LONG-RUN OKX DEMO`，启用按钮仍为 disabled；
- 刷新交互正常，控制台 `0 error / 0 warning`；
- 修复了公共请求耗时导致 ticker 被错误判断为“来自未来”的问题，随后多个周期 `autonomy.cycle_failed=0`。

## Windows 发行与安装

GitHub `v0.3.0` 公开 Release 文件（远端标签构建的最终值）：

| 文件 | 大小 | SHA-256 |
|---|---:|---|
| `Tideguard-Setup-0.3.0.exe` | 21,625,478 | `7f8caaf6b5c31ac7ddf4d0270249cb018c37b940ae9bc8338c78eca22b035c20` |
| `Tideguard-0.3.0-windows-x64.zip` | 23,062,970 | `413e27c721743d0aa8ce8a6d53f69755962abede8465cf323ce7d072e9668edb` |
| `Tideguard-0.3.0-manifest.json` | 43,671 | `862905c78816cc1f460c7f5abf5efafd5d975bf9665587a3b952053f6c2cf2e3` |

- 从公开 Release 重新下载 ZIP、manifest 与 SHA256SUMS 后，232/232 个文件的大小和 SHA-256 均匹配，且 `credentialsBundled=false`。
- 本机在推送标签前独立构建并安装的同源产物包含 228 个发行文件；安装目录 `%LOCALAPPDATA%\Programs\Tideguard` 的 228/228 个文件逐项匹配其本机构建 manifest。两次构建都来自同一代码提交，公开分发以 Release 的 SHA256SUMS 为准。
- 安装器注册版本 0.3.0，创建主界面、凭证管理、停止后台三个开始菜单入口，以及当前用户登录自启动 daemon。
- 已安装 daemon 进程参数为 `Tideguard.exe --daemon`，`/healthz` 返回 `Tideguard / demo / 0.3.0`。
- 当前运行状态：`desiredMode=disabled`、`credentialConfigured=false`、`safety=observe`、`killActive=false`、`auditChainValid=true`。
- GitHub `main` 两次 Windows Offline checks 均成功；`v0.3.0` 的 Windows release `build` 与 `publish` job 均成功，Release 为非草稿、非预发布。

本机 Codex 已创建每 6 小时一次的 `Tideguard Codex Supervisor` 周期任务。它只运行脱敏 supervisor CLI；不得访问凭证、私有 API、订单、风控代码或用户 master。本轮已用实际失败候选验证 `review → reject → 重新 review` 的证据更新链，最终 evidence 为 `554292ffd75a8d796e16fdcd7389e923b04bf65198550b4ff04f55c2030f5afb`。

## 尚未验证或有意未开放

- 没有配置用户 OKX Demo 凭证，没有调用私有账户、下单、成交、撤单或复位接口。
- Demo master 尚未启用；必须等待未来候选同时通过 OOS、至少 7 天/20 BUY shadow、相对改善和 Codex 审查后，再开始小额模拟盘测试。
- 没有正式盘路径、杠杆、转账或提现能力。
- Freqtrade/FreqAI 不随安装器捆绑；当前使用本项目原生可审计模型链。
- Tideguard 自身二进制尚未代码签名，SmartScreen 可能提示未知发布者；应从本仓库 Release 下载并核对 `SHA256SUMS.txt`。
