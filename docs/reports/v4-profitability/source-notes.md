# V4 profitability report source notes

Audience: technical. Delivery mode: self-contained HTML generated from the canonical Data Analytics artifact contract.

Question: did the execution-target correction move the historical development result above zero after explicit costs, and what prevents that result from being treated as future-profit evidence?

Sources:

- `.research-data/replays/historical-replay-v3-20260821T171933Z.json`
- `.research-data/replays/historical-replay-v4-20260821T181600Z.json`
- `research/historical_replay.py`
- `backend/src/okx_demo_lab/ml/historical_replay.py`

Chart map:

| Section | Analytical question | Family / type | Fields | Supported claim | Palette policy |
|---|---|---|---|---|---|
| V3 to V4 | Did the corrected contract remain positive under both costs? | Comparison / grouped bar | version, costScenario, netReturn | V4 ordinary is positive; stress margin is thin | hard two-root cap |
| Asset contribution | Which assets explain portfolio PnL? | Comparison / bar | instrument, netPnl, trades | XRP and DOGE dominate positive contribution; PEPE is the largest detractor | single-root preferred with signed labels |

Validation notes:

- Canonical V4 hash: `afc1732045a4a88a3ebfce61d340855791892c58f68242b70f104ff90b61f597`.
- Independent verifier reconciled all 172 trade net PnL values to final cash `11200.562705251243` and confirmed 841 strictly increasing equity checkpoints.
- Reported policy sensitivity is descriptive development evidence from already observed history, not a sealed model-selection surface.
- V3 stress drawdown and V4 stress trade count/drawdown are context fields only; the decision hinges on canonical net return and the V4 ordinary ledger.
- Portable packaging and canonical payload validation passed. Enhanced Chromium QA was not accepted because the shared reader top bar produced an 8 px desktop overflow when a vertical scrollbar was present (`clientWidth=1425`, `scrollWidth=1433`); the delivered HTML therefore carries the validated semantic chart/table fallback and a `structural_only` receipt rather than a false browser-pass claim.
