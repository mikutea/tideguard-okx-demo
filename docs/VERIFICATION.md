# 验证记录

验证日期：2026-08-14。

## 自动检查

最终工作树执行结果：

- `scripts/check.ps1`：后端 89 项测试通过。
- 桌面宿主：9 项测试通过。
- Windows 打包契约：5 项测试通过。
- TypeScript 类型检查通过。
- Vite 生产构建通过，1580 个模块完成转换。
- 硬编码凭证模式扫描通过。

后端覆盖签名、模拟盘标头、私有端点白名单、cash/tag 固定、公共历史 K 线分页、基础风控、审计脱敏与审计链篡改锁定、持久急停严格解析、订单歧义与回查、并发派发、崩溃恢复、急停/复位竞态、账户身份绑定、挂单分页、CAA 租约与限频，以及取消时 fail-closed 行为。

模型测试另外覆盖时间隔离、label horizon 与 embargo、现货 long/flat 固定周期评估、持仓期信号忽略、非重叠资本、SELL 不产生空头收益、旧 v1 报告禁止晋级、冻结模型篡改检测、人工晋级 CAS、单次许可、SELL 双层拒绝、提交结果未知时禁止重试，以及授权/停机故障注入。

这些仍是离线单元与故障编排测试，不是 OKX 私有模拟盘端到端订单验证。

## 公共行情与本地状态检查

- `environment = OKX 模拟盘`
- `mode = observe`
- `credentialConfigured = false`
- SQLite `integrity_check = ok`
- `auditChainValid = true`
- OKX 公共接口返回 `BTC-USDT / SPOT`、正数 ticker，以及 96 根、每根 9 字段的 5 分钟 K 线。
- 本轮没有调用私有 OKX API，没有下单或撤单。

使用 2000 根真实 OKX 公共、已完成 K 线重新训练了 v2 候选 `mdl_a08bb2a016a39966544f883e`：

- 评估模式：`long-only-fixed-horizon-non-overlapping`
- 5 个 OOS 折、1000 个 OOS 行、2 次非重叠 long 入场
- long/flat 准确率约 69.93%
- 扣双边成本后的固定周期诊断净值约 -0.51%
- 最大回撤约 0.51%
- 门槛失败：交易数不足、整体净值未达标

因此该候选未晋级，当前没有 champion，也没有自动执行许可。该结果说明方向准确率不能代替扣成本后的可执行绩效判断。

## 浏览器验收

- 桌面 1280 px 视口无页面级横向溢出，策略实验室双列布局正常。
- 移动端显式设置 375×812，文档 `clientWidth = scrollWidth = 360`，无页面级横向溢出。
- 移动端 6 个导航入口全部存在，DOM 中只有一个主区、顶栏和移动导航。
- 控制台没有 error 或 warn。
- 未配置凭证/未晋级 champion 时，自动许可按钮保持禁用。
- 最终界面明确区分固定周期 long-only OOS 研究诊断与 v0.2 单次 Demo BUY 入场；不显示盈利承诺。

## Windows 发行验证

- PyInstaller `onedir` 冻结 EXE 的 `--self-test` 返回 0。
- Inno Setup 静默安装返回 0。
- 安装后的 EXE `--self-test` 返回 0。
- 静默卸载返回 0；卸载后 `Tideguard.exe` 与 `_internal` 均不存在。
- 安装包逐文件 manifest、秘密/本地状态排除检查和 SHA-256 清单均通过。
- 产物不包含 API 凭证、SQLite、`.env` 或私钥文件。

当前 Windows 安装包没有代码签名，SmartScreen 可能显示信誉警告；这不是哈希失败，用户应从本仓库 Release 下载并对照 `SHA256SUMS.txt`。

## 尚未验证或有意未开放

- 本程序尚未配置用户的 OKX Demo API 凭证。
- 未运行私有账户读取、模拟盘下单、成交、查询或撤单 smoke test。
- v0.2 自动路径只允许一次最多 10 USDT 的 Demo BUY，不会自动 SELL；退出需要人工完成，因此不是长期闭环策略执行器。
- Freqtrade/FreqAI 不随安装器捆绑；当前采用本项目原生、可审计的数据模型与兼容边界。
- 没有正式盘代码路径，也未做正式盘验证。
