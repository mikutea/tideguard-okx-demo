# V5 execution readiness source notes

## Local evidence

- `.research-data/replays/historical-replay-v5-20260821T193659Z.json`
- Report SHA-256: `8e499051e01d610c41f5b700092362c23b8c450fd150615f273ebbd4ee91b7c2`
- Verifier: `research/verify_historical_replay.py`
- Frozen cohort: `.research-data/cohorts/cohort_6d7c319f462afdace7400053/manifest.json`

## Metric definitions

- Net return: final cash divided by starting cash minus one, after per-side fee and slippage.
- Stress return: the same ledger with 48 bps total round-trip cost.
- BTC execution slice: the original expected-return matrix restricted to the current `BTC-USDT` execution allowlist before order selection; it is not a second fitted model.
- Shadow duration: elapsed wall-clock time from the first eligible prospective signal to the latest eligible settlement for the same model, protocol and policy hash.

## External protocol references

- [OKX API v5](https://www.okx.com/docs-v5/en/): completed-candle flag, public market endpoints, and Demo request header.
- [scikit-learn TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html): time-ordered evaluation and gap semantics.

## Chart plan

| Decision | Visual | Dimensions | Measures | Required caveat |
|---|---|---|---|---|
| Separate research profit from executable evidence | Grouped bar | evidence layer, cost scenario | net return | historical development only |
| Show the shortest gated route to Live | Readiness table/rail | gate | current, required, status | Live AI remains disabled |
