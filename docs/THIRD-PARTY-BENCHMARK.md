# Third-party model benchmark — 2026-08-21

## Decision

None of the six locally retrained model families or their probability ensemble
qualified for a MOHENG native adapter. They remain isolated research
challengers and cannot be registered, promoted, authorized, or used for an
order.

This is a useful negative result: common boosted-tree and neural-network model
families do not turn the current 16-feature BTC-USDT 5m problem into a
cost-adjusted trading edge. Adding them to the execution package would increase
complexity and attack surface without evidence of benefit.

## Evidence boundary

- Immutable public snapshot: `dset_08123b986d947c9af478b8f5`
- Snapshot SHA-256:
  `08123b986d947c9af478b8f5b135d8e92b271315cca295d74fafeff7113c1319`
- Coverage: 905,294 confirmed BTC-USDT 5m candles, 2018-01-11 through
  2026-08-20
- Prepared observations: 905,234
- Protocol: rolling 365-day training, 90-day non-overlapping OOS, 12-bar label
  purge plus 1-bar embargo, 30 folds
- Execution semantics: cash-SPOT long/flat, fixed 12-bar holding period,
  non-overlapping capital, no synthetic short profit
- Thresholds declared before execution: 0.52, 0.56, 0.60; selected using
  development folds only
- Sealed holdout: last four folds
- Ordinary round-trip cost: 24 bps; stress cost: 48 bps
- Benchmark ID: `bench_ab4c1fd18db395334bbf85e9`
- Canonical report SHA-256:
  `84bcc73034c368fdaf8d67f79d6638b9e48d0c8f1d00153b35c800b2aca67d0e`
- Wall time: 793.68 seconds on the local workstation

The full canonical report is intentionally kept under ignored
`research/results/`; it contains every fold and every predeclared threshold.
The hash above permits local verification without publishing bulky generated
evidence.

## Results

All selected development thresholds were 0.60. Accuracy is shown only to make
the base-rate trap visible: high directional accuracy did **not** produce
positive cost-adjusted returns.

| Family | OOS trades | OOS accuracy | OOS net | Max drawdown | Worst fold | Sealed net | 48 bps stress net | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| HistGradientBoosting | 55,413 | 68.30% | -100.00% | 100.00% | -99.75% | -100.00% | -100.00% | Rejected |
| ExtraTrees | 10,526 | 76.88% | -100.00% | 100.00% | -84.70% | -97.64% | -100.00% | Rejected |
| MLP | 24,087 | 78.12% | -100.00% | 100.00% | -98.74% | -99.88% | -100.00% | Rejected |
| LightGBM | 21,664 | 78.04% | -100.00% | 100.00% | -97.38% | -99.88% | -100.00% | Rejected |
| XGBoost | 21,346 | 78.10% | -100.00% | 100.00% | -97.57% | -99.84% | -100.00% | Rejected |
| CatBoost | 20,363 | 78.00% | -100.00% | 100.00% | -97.27% | -99.81% | -100.00% | Rejected |
| Probability mean ensemble | 25,855 | 78.36% | -100.00% | 100.00% | -98.65% | -99.98% | -100.00% | Rejected |

`-100%` is the conservative diagnostic-capital floor after sequential
compounding, not an assertion that a particular user account would have been
liquidated. No real or Demo order was submitted by this benchmark.

## What was integrated

- A prediction-only adapter applies the exact native v4 evaluator to external
  probability scores. It never accepts executable model objects.
- Model specifications and reports are canonical JSON and content-addressed.
- A Python 3.11 research runtime has a hash-locked Windows dependency graph.
- Fixed adapters cover scikit-learn HistGradientBoosting, ExtraTrees and MLP,
  LightGBM, XGBoost and CatBoost, plus an unweighted probability ensemble.
- A single command reproduces full history, sealed holdout and doubled-cost
  stress evaluation. Results cannot automatically cross into the registry.

## Next research hypothesis

The failure is shared across materially different learners, so the next useful
change is not another classifier over the same 16 candle-only features. Future
challengers should first add independently collected, public-only
microstructure/regime evidence, then repeat the same sealed protocol. Any
change to features, labels, costs, folds or thresholds creates a new protocol
hash and must not overwrite this result.
