# NautilusTrader offline sidecar PoC

This package is a deliberately narrow protocol and data-adapter proof of
concept. It is **not** a trading engine integration and cannot connect to OKX.

## Hard boundary

- canonical JSON input and output are versioned and content-addressed;
- only frozen public `BTC-USDT` 5-minute bars are accepted;
- credentials, private APIs, execution, and orders are absent;
- the audited sidecar source imports no network or exchange adapter and grants
  no authorization for network use;
- the operating system does not enforce packet-level network isolation for
  this process, so outputs state `osNetworkIsolationEnforced=false` instead of
  claiming that the process has no network capability;
- outputs are always `research_only`, `promotable=false`, and credit zero
  Shadow days;
- NautilusTrader is optional for protocol validation, and dependency status is
  read from distribution metadata without importing the package;
- when the exact audited `2.0.0rc3` package is installed, the PoC may only
  construct local `Bar`, `BarType`, `Price`, and `Quantity` value objects;
- it never imports an exchange adapter, live node, execution engine, account,
  order factory, or credential provider.

The wheel's annotated `v2.0.0rc3` tag resolves to source commit
`648970ce64a304d93da0a29320cb6e19b905fa39`; the architecture and bar-semantics
review also covered `develop@2114cf6f761429e0adb5ca9596fcd7b895b16011`.
A later wheel is a new dependency and remains blocked until separately audited
and pinned.

The isolated Windows runtime requests exactly
`cpython-3.12.13-windows-x86_64` and records the observed Python identity and
`uv` version in immutable setup state. The Python distribution is managed by
`uv`; its download archive is not independently hash-locked by this project.
That residual supply-chain gap is explicit and keeps this integration
research-only. The Nautilus wheel itself remains pinned by filename, size, and
SHA-256.

## V6 execution parity is not yet validated

MOHENG V6 makes its decision at the boundary where a confirmed bar close and
the next bar open share the same timestamp. It enters at that next-bar-open
boundary and exits at the open 12 bars after entry. No Nautilus native bar-fill
configuration has yet been validated as execution-equivalent to this corrected
contract. Until a parity test exists and passes, a Nautilus result must not be
presented as execution-equivalent to the canonical V6 ledger.

Every response carries the machine-readable invariant:

```json
{"canonicalExecutionVersion":"V6","comparisonPolicy":"do_not_compare_pnl_as_execution_equivalent","nativeNautilusExecutionParityValidated":false,"reasonCode":"NATIVE_BAR_FILL_PARITY_NOT_VALIDATED"}
```

Equivalence requires a separately validated implementation of the V6 corrected
next-open-boundary contract. That may use a custom bridge, or an appropriately
validated higher-resolution tick/quote path. This PoC validates neither path
and executes no simulation.

## Dependency-free self-check

From the repository root:

```powershell
& .\.venv\Scripts\python.exe -m research.nautilus_sidecar --self-test
```

The command prints one canonical JSON line. No timestamp, host path, random ID,
or machine-specific field enters `summarySha256`; identical requests have an
identical deterministic summary. Runtime dependency availability is reported
separately and does not affect that summary.
The dependency-free self-check also reports `packageImported=false`; the
package is imported only if the caller explicitly requests local `Bar`
materialization and the exact audited distribution is present.

For a sealed request file under the project workspace:

```powershell
& .\.venv\Scripts\python.exe -m research.nautilus_sidecar --input `
  .\.research-data\nautilus-poc\request.json
```

The input must already be canonical JSON and contain its valid
`requestSha256`. Non-canonical whitespace, duplicate keys, non-finite numbers,
unknown fields, credentials-shaped fields, unconfirmed bars, gaps, and invalid
OHLC rows fail closed. Errors never echo the rejected payload.
