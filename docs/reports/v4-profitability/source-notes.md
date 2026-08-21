# V4 profitability report source notes

Audience: technical. Status: RETIRED semantic-mismatch audit material. Delivery mode: self-contained HTML generated from the historical Data Analytics artifact contract.

V4 is not canonical. Its feature timestamp already represented the confirmed-close / next-open boundary, but the replay applied an additional bar of latency. All values below are retained only to audit that superseded contract; they must not be used for V6 comparison, promotion, Shadow credit, Demo, or Live decisions.

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

- Retired V4 report-body hash: `afc1732045a4a88a3ebfce61d340855791892c58f68242b70f104ff90b61f597`.
- The historical standalone ledger check reconciled all 172 trade net PnL values to final cash `11200.562705251243` and confirmed 841 strictly increasing equity checkpoints; it did not detect the later timing-semantic mismatch and was not a source-data replay.
- Reported policy sensitivity is descriptive development evidence from already observed history, not a sealed model-selection surface.
- V3 stress drawdown and V4 stress trade count/drawdown are retained only as fields from superseded contracts; no current decision may hinge on them.
- Historical portable packaging and payload validation passed. Enhanced Chromium QA was not accepted because the shared reader top bar produced an 8 px desktop overflow when a vertical scrollbar was present (`clientWidth=1425`, `scrollWidth=1433`); this rendering note does not rehabilitate the retired trading semantics.
