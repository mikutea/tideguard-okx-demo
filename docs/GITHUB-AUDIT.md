# GitHub 现成 AI 量化项目审计

核对日期：2026-08-14；v0.3 执行边界更新于 2026-08-20。这里只记录可从项目源码、许可证和官方文档复核的事实。

## 结论

现成项目很多，但“自动 AI 量化”通常指三种不同产品：

1. 机器学习训练与预测：Freqtrade + FreqAI、Qlib、FinRL。
2. 自动策略执行：Freqtrade、Hummingbot、OctoBot。
3. LLM 对行情给意见再交易：OctoBot、OKX Agent Trade Kit 周边 agent。

没有一个候选同时满足：

- OKX 官方 Demo API，而不是框架自己的纸面撮合；
- 机器学习训练/预测；
- 现代可改 Web UI；
- Windows 本机保险库；
- 只允许现货 cash，且代码中不存在实盘切换；
- 两阶段确认、确定性风控、超时对账和哈希审计。

因此潮汐台不整体复制这些机器人。当前实现一个可审计的原生线性基线，用真实 OKX 公共 K 线训练、时间隔离验证并产出冻结建议意图；唯一执行入口仍是原有 OKX Demo 风控链。FreqAI 只保留“独立 dry-run 进程 → localhost 冻结信号”的可选边界，不随安装器捆绑。

## 候选

| 项目 | 真正擅长的部分 | 与当前目标的关键差异 | 决策 |
|---|---|---|---|
| [Freqtrade + FreqAI](https://github.com/freqtrade/freqtrade) / [FreqUI](https://github.com/freqtrade/frequi) | 机器学习特征、训练、预测、回测、dry-run；UI 成熟 | 官方 FAQ 明确不支持交易所 sandbox；dry-run 订单不会发到 OKX Demo。GPL-3.0 | 后续作为离线/Shadow 信号引擎，不作为 OKX Demo 执行器 |
| [OctoBot](https://github.com/Drakkar-Software/OctoBot) | 自动策略、LLM/Ollama 评估器、Web UI、内部模拟 | 当前源码 `Okx.is_supporting_sandbox()` 返回 `False`；不能把内部 simulator 当 OKX 官方 Demo。GPL-3.0 | 可单独体验 AI 模式，不接这把 OKX Demo Key |
| [OKX Agent Trade Kit](https://github.com/okx/agent-trade-kit) | 官方 Demo profile、MCP/CLI、约束良好的 API 工具 | 不是模型训练/回测平台，也没有现代交易 UI；工具面包含大量不需要的写操作 | 参考 Demo 行为与安全建议，不整体引入 |
| [CCXT](https://github.com/ccxt/ccxt) | OKX 适配、统一行情/订单 API、Demo 标头 | 没有策略、AI 或 UI；依赖表面覆盖 100+ 交易所 | 参考其 OKX Demo 适配；潮汐台用更窄的白名单客户端 |
| [Hummingbot](https://github.com/hummingbot/hummingbot) / [Dashboard](https://github.com/hummingbot/dashboard) | 做市与多实例编排、回测/运维 UI | `okx_paper_trade` 是 Hummingbot 内部纸面交易；系统较重，AI 训练不是核心 | 不整体部署 |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | 专业事件引擎、OKX Spot Data/Exec、Demo | 无完整 UI，AI 训练不是核心；Rust/Python 栈对首版过重 | 后续高级研究候选 |
| [Microsoft Qlib](https://github.com/microsoft/qlib) | 因子、监督学习、RL、研究流水线 | 主要是研究平台，不是即装即用的 OKX 自动机器人 | 后续离线研究候选 |
| [FinRL / FinRL-X](https://github.com/AI4Finance-Foundation/FinRL-Trading) | DRL/ML 研究与组合权重 | 当前生产示例面向股票/Alpaca，不是 OKX Crypto Demo | 不用于当前执行层 |

## v0.3 已实现的模型与执行边界

原生冻结模型只会在内部生成与以下语义等价的建议意图：

```json
{
  "modelVersion": "frozen-model-sha256",
  "observedAt": "UTC timestamp",
  "instrument": "BTC-USDT",
  "side": "buy",
  "limitPrice": "decimal string",
  "size": "decimal string",
  "evidenceHash": "sha256"
}
```

建议意图不能自行下单。v0.3 在用户长期 Demo master 和 Codex 24 小时 lease 同时有效时，允许固定 10 USDT 的 IOC BUY；只有按 OKX 实际成交和费用持久记录的模型净库存才可产生 IOC SELL。退出由 1.5% 止损、2.5% 止盈、12 根目标持有窗和 24 根硬上限驱动。每次 entry/exit 都重新经过账户身份、行情、精度、价格偏离、余额、挂单、CAA、审计和最终 HTTP 前 generation/lease 门。未知提交状态进入人工核对且禁止自动重试。

## 明确排除

- 不提供正式盘/模拟盘切换。
- 不接受在线模型直接调用交易所。
- 不允许模型更改风险阈值、交易品种、资金规模或自身代码。
- 不把回测、dry-run 或 Demo 收益表述为未来盈利能力。
