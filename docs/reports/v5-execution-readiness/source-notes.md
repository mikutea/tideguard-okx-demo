# V5 execution readiness source notes

## Local evidence

- Status: retired historical evidence; not canonical and not eligible for promotion
- Retirement reason: the observation timestamp already represented the confirmed-close / next-open boundary, then V5 applied one additional bar of latency
- `.research-data/replays/historical-replay-v5-20260821T193659Z.json` (retained unchanged for audit)
- Report-body `reportSha256`: `8e499051e01d610c41f5b700092362c23b8c450fd150615f273ebbd4ee91b7c2`
- File-byte SHA-256: `e90eee00be1d6226351929093cc1f6ecdf564af2df70834c656729a721418345`
- Verifier: `research/verify_historical_replay.py`
- Frozen cohort: `.research-data/cohorts/cohort_6d7c319f462afdace7400053/manifest.json`

## Metric definitions

- Net return: final cash divided by starting cash minus one, after per-side fee and slippage.
- Stress return: the same ledger with 48 bps total round-trip cost.
- BTC execution slice: the original expected-return matrix restricted to the current `BTC-USDT` execution allowlist before order selection; it is not a second fitted model.
- Shadow duration: elapsed wall-clock time from the first eligible prospective signal to the latest eligible settlement for the same model, protocol and policy hash.
- V5 timing: retired `latency=1` after the already aligned boundary; all V5 metrics use this unintended extra five-minute delay.
- V6 timing contract: confirmed close and next open share one timestamp boundary, `latency=0`, exit at the open 12 bars later; a 12-bar label horizon plus 1-bar embargo produces a 13-bar purge-and-embargo gap.
- V6 results: completed twice with identical result/core digests and standalone structural/ledger verification; source replay remains unverified; see `../v6-execution-semantics/report.md`.
- Scope: public research only; `promotable=false`, zero Shadow credit, no order capability, and the execution allowlist remains `BTC-USDT`.

## External protocol references

- [OKX API v5](https://www.okx.com/docs-v5/en/): completed-candle flag, public market endpoints, and Demo request header.
- [scikit-learn TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html): time-ordered evaluation and gap semantics.

## Chart plan

| Decision | Visual | Dimensions | Measures | Required caveat |
|---|---|---|---|---|
| Preserve retired V5 metrics without presenting them as V6 | Grouped bar | evidence layer, cost scenario | net return | retired timing semantics; non-canonical |
| Show the shortest gated route to Live | Readiness table/rail | gate | current, required, status | Live AI remains disabled |
