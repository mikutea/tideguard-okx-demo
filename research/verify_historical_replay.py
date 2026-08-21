from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okx_demo_lab.ml.strategy import canonical_json, sha256_hex


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPLAY_ROOT = PROJECT_ROOT / ".research-data" / "replays"
FIVE_MINUTES_MS = 300_000
EXPECTED_HOLDING_BARS = 12
EXPECTED_REPLAY_BARS = 30 * 288


class ReplayVerificationError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReplayVerificationError(message)


def _timestamp_ms(value: object, name: str) -> int:
    _require(isinstance(value, str), f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayVerificationError(f"{name} must be an ISO timestamp") from exc
    _require(parsed.tzinfo is not None, f"{name} must include a timezone")
    return round(parsed.astimezone(timezone.utc).timestamp() * 1_000)


def _number(value: object, name: str) -> float:
    _require(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value)),
        f"{name} must be finite",
    )
    return float(value)


def verify_report(report: dict[str, Any]) -> dict[str, Any]:
    stored_hash = report.get("reportSha256")
    _require(
        isinstance(stored_hash, str)
        and len(stored_hash) == 64
        and stored_hash
        == sha256_hex(
            canonical_json(
                {key: value for key, value in report.items() if key != "reportSha256"}
            )
        ),
        "canonical report hash does not match",
    )
    execution = report.get("execution")
    _require(isinstance(execution, dict), "execution contract is missing")
    schema_version = report.get("schemaVersion")
    _require(
        schema_version
        in {
            "moheng.historical-replay-report.v1",
            "moheng.historical-replay-report.v2",
            "moheng.historical-replay-report.v3",
        }
        and report.get("decision") == "research_only"
        and report.get("promotable") is False
        and report.get("shadowDaysCredited") == 0
        and execution.get("historicalReplayOnly") is True
        and execution.get("publicDataOnly") is True
        and execution.get("privateApi") is False
        and execution.get("orderCapability") is False
        and execution.get("executionAllowlistChanged") is False,
        "research-only safety contract failed",
    )

    dataset = report.get("dataset")
    protocol = report.get("protocol")
    episodes = report.get("episodes")
    _require(
        isinstance(dataset, dict)
        and isinstance(protocol, dict)
        and isinstance(episodes, list),
        "dataset, protocol, or episodes are missing",
    )
    instruments = dataset.get("instruments")
    asset_rows = dataset.get("assetRows")
    _require(
        isinstance(asset_rows, int)
        and not isinstance(asset_rows, bool)
        and 3 <= asset_rows <= 8
        and isinstance(instruments, list)
        and len(instruments) == asset_rows
        and len(set(instruments)) == asset_rows
        and all(isinstance(item, str) and item.endswith("-USDT") for item in instruments),
        "replay instrument universe is invalid",
    )
    episode_count = protocol.get("episodeCount")
    _require(
        isinstance(episode_count, int)
        and episode_count == len(episodes)
        and episode_count > 0
        and protocol.get("trainBars") == 365 * 288
        and protocol.get("retrainEveryBars") == EXPECTED_REPLAY_BARS
        and protocol.get("holdingBars") == EXPECTED_HOLDING_BARS
        and protocol.get("scope") == "retrospective-development-only",
        "rolling replay protocol is invalid",
    )
    if schema_version in {
        "moheng.historical-replay-report.v2",
        "moheng.historical-replay-report.v3",
    }:
        model = report.get("model")
        _require(isinstance(model, dict), "model contract is invalid")
        target = model.get("targetContract")
        leakage = report.get("leakageAudit")
        _require(
            isinstance(target, dict)
            and target.get("decisionAt") == "confirmed_bar_close"
            and target.get("entryAt") == "next_bar_open"
            and target.get("exitAt") == "entry_plus_12_bars_open"
            and target.get("labelHorizonBars") == 13
            and target.get("predictionUnit") == "gross_return"
            and isinstance(leakage, dict)
            and leakage.get("targetExecutionAligned") is True
            and protocol.get("executionLabelHorizonBars") == 13
            and protocol.get("developmentHistoryAlreadyObserved") is True,
            "V4 execution target or selection-bias contract failed",
        )

    previous_stop: int | None = None
    episode_ids: set[str] = set()
    for expected_index, episode in enumerate(episodes):
        _require(isinstance(episode, dict), "episode row is invalid")
        episode_id = episode.get("episodeId")
        _require(
            episode.get("episode") == expected_index
            and isinstance(episode_id, str)
            and episode_id.startswith("replay_episode_")
            and episode_id not in episode_ids,
            "episode identity is invalid",
        )
        episode_ids.add(episode_id)
        timeline = [
            _timestamp_ms(episode.get("fitStartAt"), "fitStartAt"),
            _timestamp_ms(episode.get("fitStopAt"), "fitStopAt"),
            _timestamp_ms(episode.get("calibrationStartAt"), "calibrationStartAt"),
            _timestamp_ms(episode.get("calibrationStopAt"), "calibrationStopAt"),
            _timestamp_ms(episode.get("labelCompleteAt"), "labelCompleteAt"),
            _timestamp_ms(episode.get("availableAt"), "availableAt"),
            _timestamp_ms(episode.get("replayStartAt"), "replayStartAt"),
            _timestamp_ms(episode.get("replayStopAt"), "replayStopAt"),
        ]
        _require(
            all(left < right for left, right in zip(timeline, timeline[1:])),
            "episode timeline is not strictly causal",
        )
        _require(
            timeline[6] - timeline[5] == FIVE_MINUTES_MS,
            "model availability must precede replay by one bar",
        )
        if previous_stop is not None:
            _require(
                timeline[6] - previous_stop == FIVE_MINUTES_MS,
                "episode replay windows are not contiguous",
            )
        previous_stop = timeline[7]
        _require(
            episode.get("assetRows") == asset_rows
            and episode.get("replayRows") == EXPECTED_REPLAY_BARS * asset_rows
            and isinstance(episode.get("fitRows"), int)
            and episode.get("fitRows") > 0
            and isinstance(episode.get("calibrationRows"), int)
            and episode.get("calibrationRows") > 0,
            "episode row counts are invalid",
        )

    result = report.get("result")
    _require(isinstance(result, dict), "replay result is missing")
    ordinary = result.get("ordinary")
    stress = result.get("stress48Bps")
    _require(
        isinstance(ordinary, dict) and isinstance(stress, dict),
        "ordinary or stress ledger is missing",
    )
    ordinary_broker = ordinary.get("broker")
    stress_broker = stress.get("broker")
    leakage = ordinary.get("leakageGuard")
    _require(
        isinstance(ordinary_broker, dict)
        and ordinary_broker.get("roundTripCostBps") == 24.0
        and isinstance(stress_broker, dict)
        and stress_broker.get("roundTripCostBps") == 48.0
        and isinstance(leakage, dict)
        and leakage.get("causalEpisodeBinding") is True
        and leakage.get("nextBarExecution") is True
        and leakage.get("sameBarFillAllowed") is False,
        "broker cost or leakage contract failed",
    )
    if schema_version in {
        "moheng.historical-replay-report.v2",
        "moheng.historical-replay-report.v3",
    }:
        _require(
            ordinary_broker.get("capacityHandling") == "clip"
            and ordinary_broker.get("executionLabelHorizonBars") == 13
            and stress_broker.get("capacityHandling") == "clip"
            and execution.get("engineSchemaVersion")
            == "moheng.historical-replay.v2",
            "V4 capacity or engine contract failed",
        )
    if schema_version == "moheng.historical-replay-report.v3":
        execution_slice = result.get("executionSlice")
        _require(isinstance(execution_slice, dict), "BTC execution slice is missing")
        slice_ordinary = execution_slice.get("ordinary")
        slice_stress = execution_slice.get("stress48Bps")
        slice_failures = execution_slice.get("failures")
        _require(
            execution_slice.get("instrument") == "BTC-USDT"
            and execution_slice.get("decision") == "research_only"
            and isinstance(slice_ordinary, dict)
            and isinstance(slice_stress, dict)
            and isinstance(slice_failures, list)
            and all(isinstance(item, str) for item in slice_failures)
            and execution_slice.get("developmentGatePassed")
            is (len(slice_failures) == 0),
            "BTC execution slice contract failed",
        )
        slice_ordinary_broker = slice_ordinary.get("broker")
        slice_stress_broker = slice_stress.get("broker")
        _require(
            isinstance(slice_ordinary_broker, dict)
            and slice_ordinary_broker.get("roundTripCostBps") == 24.0
            and isinstance(slice_stress_broker, dict)
            and slice_stress_broker.get("roundTripCostBps") == 48.0
            and isinstance(slice_ordinary.get("trades"), int)
            and not isinstance(slice_ordinary.get("trades"), bool)
            and isinstance(slice_stress.get("trades"), int)
            and not isinstance(slice_stress.get("trades"), bool),
            "BTC execution slice ledger is invalid",
        )
    starting_cash = _number(ordinary_broker.get("startingCash"), "startingCash")
    final_cash = _number(ordinary.get("finalCash"), "finalCash")
    max_drawdown = _number(ordinary.get("maxDrawdown"), "maxDrawdown")
    _require(starting_cash > 0 and final_cash >= 0, "cash ledger is invalid")
    _require(0 <= max_drawdown <= 1, "max drawdown is outside [0, 1]")
    _require(
        _number(ordinary.get("totalFees"), "totalFees") >= 0
        and _number(
            ordinary.get("totalEstimatedSlippageCost"),
            "totalEstimatedSlippageCost",
        )
        >= 0,
        "cost ledger is invalid",
    )

    checkpoints = ordinary.get("checkpoints")
    trades = ordinary.get("trades")
    _require(
        isinstance(checkpoints, list)
        and len(checkpoints) >= 2
        and isinstance(trades, list),
        "replay checkpoints or trades are missing",
    )
    checkpoint_times: list[int] = []
    checkpoint_drawdowns: list[float] = []
    for checkpoint in checkpoints:
        _require(isinstance(checkpoint, dict), "checkpoint row is invalid")
        checkpoint_times.append(_timestamp_ms(checkpoint.get("at"), "checkpoint.at"))
        cash = _number(checkpoint.get("cash"), "checkpoint.cash")
        equity = _number(checkpoint.get("equity"), "checkpoint.equity")
        drawdown = _number(checkpoint.get("drawdown"), "checkpoint.drawdown")
        _require(cash >= 0 and equity >= 0 and 0 <= drawdown <= 1, "checkpoint ledger is invalid")
        checkpoint_drawdowns.append(drawdown)
    _require(
        all(left < right for left, right in zip(checkpoint_times, checkpoint_times[1:])),
        "checkpoint clock is not strictly increasing",
    )
    _require(
        max(checkpoint_drawdowns) <= max_drawdown + 1e-12,
        "checkpoint drawdown exceeds reported maximum",
    )

    trade_ids: set[str] = set()
    net_pnl = 0.0
    for trade in trades:
        _require(isinstance(trade, dict), "trade row is invalid")
        trade_id = trade.get("tradeId")
        _require(
            isinstance(trade_id, str)
            and trade_id not in trade_ids
            and trade.get("episodeId") in episode_ids
            and trade.get("instrument") in instruments,
            "trade identity is invalid",
        )
        trade_ids.add(trade_id)
        signal_at = _timestamp_ms(trade.get("signalAt"), "trade.signalAt")
        entered_at = _timestamp_ms(trade.get("enteredAt"), "trade.enteredAt")
        exited_at = _timestamp_ms(trade.get("exitedAt"), "trade.exitedAt")
        _require(
            entered_at - signal_at == FIVE_MINUTES_MS
            and exited_at - entered_at == EXPECTED_HOLDING_BARS * FIVE_MINUTES_MS,
            "trade timing violates latency or holding period",
        )
        net_pnl += _number(trade.get("netPnl"), "trade.netPnl")
    _require(
        math.isclose(starting_cash + net_pnl, final_cash, rel_tol=0, abs_tol=1e-7),
        "cash ledger does not reconcile to trade net PnL",
    )

    return {
        "assetRows": asset_rows,
        "checkpoints": len(checkpoints),
        "episodes": episode_count,
        "finalCash": final_cash,
        "maxDrawdown": max_drawdown,
        "netReturn": _number(ordinary.get("netReturn"), "netReturn"),
        "reportSha256": stored_hash,
        "shadowDaysCredited": 0,
        "trades": len(trades),
        "verified": True,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify one V3/V4 historical replay report.")
    parser.add_argument("report", type=Path)
    return parser.parse_args()


def main() -> int:
    path = _parse_args().report.expanduser().resolve()
    try:
        path.relative_to(REPLAY_ROOT.resolve())
    except ValueError as exc:
        raise ReplayVerificationError(
            "replay report must stay under project .research-data/replays"
        ) from exc
    _require(path.is_file() and 2 <= path.stat().st_size <= 5_000_000, "report file is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayVerificationError("report is not valid UTF-8 JSON") from exc
    _require(isinstance(value, dict), "report root must be an object")
    print(json.dumps(verify_report(value), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReplayVerificationError as exc:
        print(json.dumps({"error": str(exc), "verified": False}, ensure_ascii=False))
        raise SystemExit(1) from exc
