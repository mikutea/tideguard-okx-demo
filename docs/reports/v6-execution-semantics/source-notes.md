# V6 execution semantics source notes

## Local immutable replay evidence

- Latest valid report: `.research-data/replays/historical-replay-v6-20260821T230559Z.json`
- First valid repeat: `.research-data/replays/historical-replay-v6-20260821T230117Z.json`
- Report-body SHA-256 values: `083d5d2eeb12ef022efb4564133de1b868a7f1012be6450f748b3e80439d5de9` and `7888cd1ecc0f2ad06a7143f52febd897056f0733c25565c02262475993c1d3f7`
- File-byte SHA-256 values: `4dc5f20e433e99aca824ff06889644d535abe2a6e2d584f0604502cb78c650e8` and `924e04daf0fb3ebc88e9c02829d8b79fb69b28b29293e1dc50f6ab05a9fa2287`
- Schema: `moheng.historical-replay-report.v4`
- Engine schema: `moheng.historical-replay.v3`
- Frozen cohort: `.research-data/cohorts/cohort_6d7c319f462afdace7400053/manifest.json`
- Cohort content SHA-256: `6d7c319f462afdace7400053e91110b863f14941616bc4899be05e2d84f0f98c`
- Standalone structural/ledger verifier: `research/verify_historical_replay.py`
- Result canonical digest, repeated twice: `d4c17435e470b606dfdf5cc278cb90e2a13356e31ed8d86484db99662b62f075`
- Core projection: `moheng.historical-replay-core.v1`
- Core digest, repeated twice: `16ed5ee04954c0119cab5c9355e5888fd77df114192e5f7f8d20f361a528eece`
- Verifier scope: `structuralLedgerVerified=true`, `sourceReplayVerified=false`
- Superseded evidence: `213029Z` was pre-canonical; `213912Z` and `214516Z`
  used close-at-open checkpoint valuation; `222347Z` and `222758Z` used the corrected
  open valuation but predated the mandatory machine marker and peak/trough witnesses.
  All five are rejected by the final verifier contract and excluded from monitoring.

`reportSha256` hashes canonical JSON after removing only the hash field itself. It is not
the SHA-256 of the pretty-printed JSON file bytes; both values are recorded above.

## Final ledger metrics

| Ledger | Net return | Trades | Max drawdown |
|---|---:|---:|---:|
| 7-asset ordinary 24 bps | +26.8441904659% | 187 | 4.7543250256% |
| 7-asset stress 48 bps | +12.0876522037% | 151 | 8.2527939867% |
| BTC-USDT ordinary 24 bps | +1.7172185331% | 14 | 0.8523326565% |
| BTC-USDT stress 48 bps | +0.2789801620% | 11 | 0.9721964722% |

The reported drawdown is sampled at each five-minute open boundary using estimated
liquidation fees and slippage. It is not an intrabar-low or order-book drawdown measure.

## Contracts

- The feature timestamp is the confirmed source-bar close and the next-candle open boundary.
- Decision-row latency is zero; exit is the open exactly 12 bars later.
- The 12-bar label horizon plus one-bar embargo creates a 13-bar train/test gap.
- Capacity uses quote volume from the confirmed feature-source bar, not full entry-bar volume.
- Every ledger declares `checkpointValuationBasis=current_bar_open_at_checkpoint_boundary`;
  embedded peak/trough witnesses are bound to exact checkpoint equity rows.
- Zero-latency next-open fill remains an instantaneous inference/order assumption.
- Public research only; `promotable=false`, zero Shadow credit, no private API and no order capability.
- Execution allowlist remains `BTC-USDT` only.

## Nautilus PoC evidence

- Setup state SHA-256: `1a887648edf379d9bf32a9cde7d89c8795d99e6e3f396af48fd2c3c446388031`
- Wheel SHA-256: `8a90b01ccf66d78946c565bca08b7758bc7f312caf1ded1c2c2c710013a7c092`
- Repeated evidence files and byte SHA-256 values:
  - `offline-self-test-20260821T225527721Z-7bb767943bac.json`: `0b23870f4b9ff5f41129cabf039b386188fb146407dd20e8053ba1b173b033c9`
  - `offline-self-test-20260821T230029782Z-4bc8db83466b.json`: `7b8698a53350307557681b1b0824d087eb143728e65ef4bec7742062e61b513f`
  - `offline-self-test-20260821T230417586Z-a20e351ba7fd.json`: `1b958f652d4b3cac2e3fe2bc30e96fa798648d410426b4272eb05f58bc1a9d3b`
- Repeated evidence `sidecar` object canonical SHA-256: `7c7dd4dec9ce505e88047c440348b71ff91509ead1e202864f0936ef0007480d`
- Repeated protocol `summarySha256`: `9ffaeaf644bbe4ddb1b62d45524ebf61b5944b28466e06e4bb768439539961a7`
- Runtime: CPython `3.12.13`, 64-bit, observed architecture `AMD64`; `uv 0.11.7`
- Materialized local bars: 1
- Installed distribution integrity: 101 files matched the pinned wheel RECORD;
  setup-v3 state, sidecar hash record, installed RECORD, and `direct_url.json` verified
- Network use authorized: false; network adapters imported: false
- OS network isolation enforced: false
- Simulations, trades, private API calls, order calls: 0
- Execution parity: not validated (`NATIVE_BAR_FILL_PARITY_NOT_VALIDATED`)

The Nautilus wheel is pinned by filename, size, and SHA-256. The exact uv-managed Python
version is verified at runtime, but its downloaded distribution archive is not independently
hash-locked by this project; this residual supply-chain gap remains research-only.

## Interpretation boundary

Historical net return is a development diagnostic, not a forecast. The V6 result must not be
combined with retired V5 metrics, credited as forward Shadow, or used to authorize Demo or
Live execution. The standalone verifier checks report structure and ledger arithmetic; it does
not independently regenerate the report from frozen source arrays.
