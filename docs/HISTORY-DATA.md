# 墨衡官方历史行情仓库

训练数据来自 OKX 公共 `GET /api/v5/market/history-candles`，不需要也不会读取 Demo/Live 私有凭证。首版固定 `BTC-USDT / SPOT / 5m`。

## 当前可见覆盖

2026-08-20 对官方接口的只读探测显示，当前最早可见的已确认 5 分钟 K 线为：

- UTC：2018-01-11 11:10
- 北京时间：2018-01-11 19:10
- `ts=1515669000000`

这只是当时的接口观察值。OKX 文档只承诺可取近年数据，并不保证一个永久固定的起点，所以程序不会硬编码该日期；首次回填持续分页到官方返回空页。

## 持久仓库与可恢复回填

数据写入 `%LOCALAPPDATA%\Tideguard\market-data.sqlite3`，不会进入 Git、安装包或 Release。

- 公共行情请求不携带 Demo/Live 私有请求头；`x-simulated-trading: 1` 只用于 Demo 私有账户与交易请求。这样 Demo 与 Live 共用同一份真实公共研究数据，也避免 OKX Demo 路由截断旧历史。
- 请求 `limit=300`，同时兼容服务端只返回 100 条；短页不能被当成历史终点。
- 每页以真实最小时间戳作为下一个 `after`，游标必须严格递减；重复、反向或振荡即失败关闭。
- 空页不能单独证明历史终点。首次空页簇只记录 `HistoryOriginUnconfirmed` 并保持 partial；至少 60 秒后的下一次同步，必须在同一最老边界以不同游标和 `limit=100` 再次得到空页，才允许标记 backfill complete。任一探测恢复数据就继续回填。
- 每页 candle 与可恢复进度原子落盘；进程中断后从本地最老连续记录继续。
- 采用保守限频；HTTP 429、5xx 与 OKX `50011` 指数退避。
- 只保存 `confirm=1`；同时间戳出现不同已确认内容时写入冲突表，不静默覆盖。
- 每次增量同步覆盖近期窗口，以吸收缓存延迟和官方历史修订。

## 训练前硬门

快照只有在以下条件全部满足时才可训练：

- 时间戳严格按 300,000 ms 对齐且无缺口；
- 无未解决内容冲突；
- OHLC 有限、为正且关系合法，成交量非负；
- 全部 K 线已完结；
- 流式内容 SHA-256、特征契约 SHA-256、起止时间和行数已落盘。

全历史主要用于覆盖不同市场制度的 rolling walk-forward OOS。部署模型只拟合预先声明的近期窗口，避免把 2018 年与当前市场机械等权。更多数据可以降低抽样盲点，但不能自动提高收益。

## v4 时间协议

- 365 天滚动训练窗；
- 90 天非重叠 OOS 测试窗和 90 天 step；
- 标签 12 根 K 线；
- purge + embargo 共 13 根；
- 三个预先冻结的候选配置共享同一数据快照和 split protocol；
- 同 cohort 时直接比较 challenger 与旧 champion；跨 cohort 时，使用新 cohort 内、与旧 champion 具有相同 `trainingConfigSha256` 的同批模型作为 paired champion-recipe baseline。找不到配对配方时以 `champion_comparison_missing` 失败关闭，绝不把不同时间口径直接排名。

候选仍需扣成本、long/flat、不重叠资本、future shadow 和 Codex 监督，不能把高 accuracy 或单次回测排行等同于可执行盈利。

参考：

- [OKX Candlesticks history](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks-history)
- [OKX API rate limits](https://www.okx.com/docs-v5/en/#overview-rate-limits)
- [OKX Historical Market Data](https://www.okx.com/historical-data)
- [OKX Historical Data Terms](https://www.okx.com/en-us/help/historicaldata-terms-and-conditions)
