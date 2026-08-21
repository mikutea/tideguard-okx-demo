# Third-party systematic-trading intake

This inventory starts from `paperswithbacktest/awesome-systematic-trading` and
records what MOHENG may use. A repository listing, paper, backtest screenshot,
or pretrained file is never profitability evidence.

| Component | Role in MOHENG | Integration boundary | License decision |
| --- | --- | --- | --- |
| scikit-learn | HistGradientBoosting, ExtraTrees and MLP baselines | Isolated local research runtime; locally trained predictions only | BSD-3-Clause; accepted |
| LightGBM | Gradient-boosted tree challenger | Isolated local research runtime; locally trained predictions only | MIT; accepted |
| XGBoost | Gradient-boosted tree challenger | Isolated local research runtime; locally trained predictions only | Apache-2.0; accepted |
| CatBoost | Ordered-boosting challenger | Isolated local research runtime; locally trained predictions only | Apache-2.0; accepted |
| QuantStats | Human-readable research diagnostics | Offline report only; its period-based ratios never replace MOHENG trade gates | Apache-2.0; accepted as optional research tooling |
| Cryptofeed | Future public microstructure capture | Public OKX market-data sidecar only; no credential/private/order imports | MIT; accepted as optional collector |
| Qlib | Research-workflow reference | Separate process/reference only; no copied model weights | MIT; defer framework embedding |
| FreqAI | Retraining/producer-consumer reference | Separate GPL process, Demo shadow signals only | GPL-3.0; do not link or bundle into MOHENG |
| NautilusTrader | Event-driven simulation reference | Separate sidecar only; not an order path | LGPL-3.0; defer heavy integration |
| vectorbt | Internal analyst experimentation only | Never bundled or redistributed with MOHENG | Apache-2.0 plus Commons Clause; excluded from product |

Source repositories: [awesome-systematic-trading](https://github.com/paperswithbacktest/awesome-systematic-trading),
[scikit-learn](https://github.com/scikit-learn/scikit-learn),
[LightGBM](https://github.com/lightgbm-org/LightGBM),
[XGBoost](https://github.com/dmlc/xgboost),
[CatBoost](https://github.com/catboost/catboost),
[QuantStats](https://github.com/ranaroussi/quantstats),
[Cryptofeed](https://github.com/bmoscon/cryptofeed),
[Qlib](https://github.com/microsoft/qlib),
[Freqtrade/FreqAI](https://github.com/freqtrade/freqtrade),
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader), and
[vectorbt](https://github.com/polakowo/vectorbt). Exact installed versions are
recorded in the hash-locked requirements file and every benchmark report.

## Admission protocol

1. Source code and algorithm descriptions may be reviewed. Untrusted `.pkl`,
   `.joblib`, `.pt`, `.onnx`, binary strategy plug-ins, and copied API
   credentials are rejected.
2. Every challenger is retrained locally on the same immutable BTC-USDT 5m
   snapshot. Every fold trains on its past 365 days and evaluates the next 90
   days after a 12-bar label purge and 1-bar embargo.
3. Capital is cash-SPOT long/flat and non-overlapping. A SELL signal never
   earns synthetic short profit. Ordinary evaluation charges 24 bps per round
   trip and stress evaluation charges 48 bps.
4. Thresholds 0.52, 0.56, and 0.60 are declared before the run. The threshold
   is selected only on development folds; the final four folds remain sealed.
5. Every family and every failure is reported. A model must pass development,
   full OOS, sealed OOS, and doubled-cost stress checks before native-adapter
   review. Passing still authorizes only a data-only artifact and Demo shadow,
   not autonomous trading or Live execution.
