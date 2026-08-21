# Multi-asset and alternative-data research boundary

## Decision

MOHENG should research more than BTC-USDT, and it should test news, macro,
on-chain and attention features. Neither change is assumed to improve trading
accuracy or returns. Both remain public-data research until they demonstrate
incremental, cost-adjusted OOS value over the candle-only baseline.

The production execution allowlist remains `BTC-USDT`. Research discovery does
not grant Demo or Live order permission.

## Provisional public universe

On 2026-08-21 the content-addressed discovery policy queried OKX public SPOT
instruments and tickers and selected six provisional members:

| Rank | Instrument | 24h quote volume at discovery | Spread at discovery |
| ---: | --- | ---: | ---: |
| 1 | BTC-USDT | 995.1M USDT | 0.013 bps |
| 2 | ETH-USDT | 471.6M USDT | 0.043 bps |
| 3 | XRP-USDT | 166.8M USDT | 0.783 bps |
| 4 | SOL-USDT | 101.8M USDT | 1.132 bps |
| 5 | DOGE-USDT | 85.3M USDT | 1.227 bps |
| 6 | PEPE-USDT | 28.9M USDT | 3.123 bps |

The frozen discovery-policy SHA-256 is
`d45eead71795deae02c3c425b851dbde0705d6d10b05648de95cee84f4206ff2`.
Each run's market snapshot and report hash change with public prices and time;
the generated JSON remains under ignored `research/results/`.

This is not a portfolio recommendation. A single 24-hour volume snapshot is
volatile and can be manipulated. Each candidate still needs:

- complete confirmed 5m history and an independently hashed snapshot;
- zero unresolved gaps/conflicts and no pre-listing synthetic fill;
- 30-day median quote volume, spread and outage coverage;
- per-asset development/sealed/48-bps-stress OOS;
- an aligned cohort and one-shared-cash portfolio OOS;
- correlation, contribution concentration, delisting and venue-token stress;
- 60–90 days prospective shadow after model and threshold selection.

PEPE is therefore a provisional research row, not an endorsement. A
correlation/concentration gate may remove it or any other member.

## Shared-capital portfolio protocol

Adding independent single-asset equity curves is invalid because each curve
reuses the same capital. `ml/portfolio.py` introduces a conservative research
evaluator with:

- one aligned decision clock and one cash account;
- deterministic score ranking for simultaneous signals;
- inverse-past-volatility sizing, gross-exposure and per-asset caps;
- a fixed holding window that ignores all signals while capital is occupied;
- per-asset cost, contribution, worst-rebalance and portfolio drawdown evidence.

The first baseline will use simple volatility scaling. Portfolio libraries such
as [skfolio](https://github.com/skfolio/skfolio),
[Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib) and
[PyPortfolioOpt](https://github.com/PyPortfolio/PyPortfolioOpt) are reference
challengers only; estimated-return optimization and solver complexity do not
enter the product before they beat the deterministic baseline on sealed data.

## News, social, macro and on-chain quality findings

The intended grain is one revision of one public event for one mapped asset.
Every row must preserve `published_at`, `first_seen_at`, `fetched_at`, content
hash, source/license snapshot, language and revision. Feature availability is:

```text
max(published_at, first_seen_at) <= candle_closed_at
and fetched_at <= candle_closed_at
and inference_completed_at <= candle_closed_at
```

Historical news without a provable first-seen timestamp is retrospective only.
It cannot enter promotion evidence or prospective shadow.

| Source/model | Status | Data-quality and rights decision |
| --- | --- | --- |
| Official RSS/Atom | Recommended | Capture locally and preserve first seen; each feed still needs a terms snapshot. Use [feedparser](https://github.com/kurtmckee/feedparser) rather than custom XML parsing. |
| [GDELT 2.0](https://www.gdeltproject.org/data.html) | Recommended metadata source | Store URL/title/domain/event metadata, attribution and GDELT file time; do not republish article bodies. Historical rows remain retrospective unless first-seen is proven. |
| [Alternative.me Fear & Greed](https://alternative.me/crypto/fear-and-greed-index/) | Recommended daily control | Requires attribution and is partly derived from BTC price/volume, so it is a regime control, not independent sentiment alpha. |
| [FRED/ALFRED](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html) | Recommended macro source | Use ALFRED vintages, not today's revised history; whitelist series without third-party copyright restrictions. |
| [Coin Metrics](https://github.com/coinmetrics/data) community data | Local non-commercial research only | CC BY-NC 4.0 and revision states prevent bundling in a public commercial-capable desktop release. |
| NewsAPI / LunarCrush / StockTwits / Google Trends Alpha | Conditional | Require an approved account/tier and explicit training/commercial rights; persist revisions and vendor outages. |
| [Reddit](https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data) | Rejected for model training | Current developer terms require express approval for model training and deletion synchronization. |
| [Telegram](https://telegram.org/tos/content-licensing) | Rejected for model training | Current content terms prohibit scraping/aggregation for training, validation or benchmarking ML systems. |
| [X/Twitter](https://docs.x.com/developer-terms/agreement) | Rejected without written authorization | Current developer terms restrict ML training and require edit/deletion compliance; third-party archives are also rejected. |
| [VADER](https://github.com/cjhutto/vaderSentiment) | Accepted baseline | MIT, deterministic and explainable; low crypto-language accuracy means it is only a baseline. |
| [Prosus FinBERT](https://github.com/ProsusAI/finBERT) | Source ideas only | Model repository supplies executable `.bin` weights and has incomplete weight/corpus licensing provenance for this product. Do not load. |
| [CryptoBERT](https://huggingface.co/ElKulako/cryptobert) | Legal-source review only | Safetensors/MIT model card, but training data includes platforms whose current terms conflict with model training. Do not bundle. |
| [LedgerBERT sentiment](https://huggingface.co/ExponentialScience/LedgerBERT-Market-Sentiment) | Excluded from product | Safetensors but CC BY-NC 4.0. |
| [FinBERT2 Chinese encoder](https://github.com/valuesimplex/FinBERT) | Future isolated candidate | MIT source and Safetensors exist, but it is not a ready crypto sentiment model; it needs an authorized local labeled set and separate OOS evidence. |

Source-code, data and model-weight licenses are three separate gates. A MIT API
wrapper does not grant rights to the content it downloads.

The machine-readable source decision registry is
`research/alternative-data-sources.json`; its current canonical SHA-256 is
`39dd69486cd3b7349eb2e42d49d476e4de7bde235f48d2a7a88b40872c87178f`.

## Weak-signal architecture

```text
approved public source
  -> append-only source snapshot + license hash
  -> dedup/revision/language/entity mapping
  -> isolated offline scorer (no tools, credentials or private API)
  -> numeric point-in-time feature snapshot
  -> baseline vs +source ablation on aligned OOS
  -> prospective shadow
  -> optional risk-state input (normal / reduce / pause)
```

News and social features do not mechanically repeat across every 5m candle.
Experiments must test 15m, 1h, 4h and 1d contexts, include age/missing/stale
features, and regress out contemporaneous return/volume/volatility before
claiming sentiment leads price.

Required ablations are baseline, official events, news sentiment, authorized
social aggregation, on-chain, macro/attention, and the combined gate. All
candidates and failures are reported. The final four folds stay sealed, then a
new 60–90 day forward shadow is collected; classifier accuracy is not an
admission metric.

## Cryptofeed audit

[Cryptofeed](https://github.com/bmoscon/cryptofeed) remains useful for public
trade callbacks. Its Python 3.11-compatible 2.4.1 OKX L2 implementation only
checks the legacy checksum and does not enforce `seqId/prevSeqId`. OKX
deprecated checksum integrity on 2026-06-23 and requires sequence continuity.
Therefore MOHENG must not use that version for trusted L2 order-book features.
Trade capture may be tested separately; L2 needs a patched adapter with
sequence-gap recovery and replay tests first.

## Safety invariants

- `ALLOWED_INSTRUMENTS` remains the execution allowlist and remains BTC-only.
- Research artifacts must bind instrument, bar, universe hash, market snapshot,
  auxiliary-signal snapshot, point-in-time join contract and cost model.
- News/LLM processes have no credential provider, `TradingService`, private API
  or order capability.
- No alternative-data model can enter Live automatic execution. Live remains
  the existing independently authorized, short-lived manual path.
