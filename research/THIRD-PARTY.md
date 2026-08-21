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
| VADER Sentiment | Explainable English sentiment baseline | Isolated headline scoring after source/time/license gates | MIT; accepted as a baseline, never a direct signal |
| skfolio / PyPortfolioOpt | Portfolio parity challengers | Research-only covariance, HRP and allocation comparison | BSD-3-Clause / MIT; review before pinning |
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
[VADER Sentiment](https://github.com/cjhutto/vaderSentiment),
[skfolio](https://github.com/skfolio/skfolio),
[PyPortfolioOpt](https://github.com/PyPortfolio/PyPortfolioOpt),
[Qlib](https://github.com/microsoft/qlib),
[Freqtrade/FreqAI](https://github.com/freqtrade/freqtrade),
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader), and
[vectorbt](https://github.com/polakowo/vectorbt). Exact installed versions are
recorded in the hash-locked requirements file and every benchmark report.

Cryptofeed 2.4.1 is accepted only for public trades. Its OKX L2 adapter does not
validate the post-2026 `seqId/prevSeqId` continuity protocol, so it is rejected
for trusted order-book research until patched and replay-tested.

## Admission protocol

1. Source code and algorithm descriptions may be reviewed. Untrusted `.pkl`,
   `.joblib`, `.pt`, `.onnx`, binary strategy plug-ins, and copied API
   credentials are rejected.
2. Single-asset challengers are retrained locally on the same immutable
   BTC-USDT 5m snapshot. Multi-asset challengers use one content-addressed,
   strictly intersected 5m cohort with no forward-fill. The initial cohort is
   a fixed-current survivor cohort and therefore can never be promoted.
   Every fold trains on its past 365 days and evaluates the next 90 days after
   a 12-bar label purge and 1-bar embargo.
3. V2 reserves the final 30 days inside each training fold for disjoint Platt
   probability and monotonic expected-return calibration, with a separate
   label-horizon purge before calibration. The policy stays in cash unless the
   calibrated expected gross return strictly clears ordinary round-trip cost
   plus a predeclared safety buffer.
4. Safety buffers of 12/24/48 bps and minimum entry spacings of 48/96 five-minute
   bars are declared before the run. Ordinary evaluation charges 24 bps per
   round trip and stress evaluation charges 48 bps. Gross return, net return,
   cash exposure, trades/day and instrument concentration are all reported.
5. The four V1 sealed folds have already been observed and are permanently
   retired for V2 tuning. V2 evaluates only the first five retrospective
   development folds; no historical result can be called fresh sealed OOS.
   Every report remains non-promotable until at least 90 days of new prospective
   public Shadow evidence and manual review. It never authorizes autonomous or
   Live execution.
