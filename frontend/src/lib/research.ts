import type { LongRunStatus, MLStatus, ResearchStatus, SupervisorModelReview } from "../types";

export function deriveResearchStatus(ml: MLStatus | null): ResearchStatus {
  const run = ml?.longRun.latestTraining ?? null;
  const warehouse = ml?.longRun.dataWarehouse;
  const stage = run?.status === "failed" ? "failed" : run?.status === "completed" ? "completed" : run?.phase ?? "idle";
  const syncState = warehouse?.syncStatus === "syncing" ? "running" : warehouse?.syncStatus === "error" ? "failed" : warehouse?.backfillComplete ? "complete" : "idle";

  return {
    dataset: warehouse ? {
      source: warehouse.source,
      instrument: warehouse.instrument,
      bar: warehouse.bar,
      confirmedRows: warehouse.storedRows,
      targetRows: warehouse.backfillComplete ? warehouse.expectedRows : null,
      earliestAt: warehouse.firstOpenAt,
      latestAt: warehouse.lastOpenAt,
      gaps: warehouse.missingBars,
      conflicts: warehouse.unresolvedConflicts,
      snapshotSha256: warehouse.latestSnapshot?.contentSha256 ?? null,
      lastErrorType: warehouse.lastErrorType,
      syncState,
      updatedAt: warehouse.lastSyncAt
    } : undefined,
    training: {
      stage,
      progress: run?.progressTotal && run.progressTotal > 0 ? Math.min(1, run.progressCurrent / run.progressTotal) : null,
      cpuPercent: null,
      memoryMb: null,
      elapsedSeconds: run?.completedAt
        ? Math.max(0, (new Date(run.completedAt).getTime() - new Date(run.startedAt).getTime()) / 1000)
        : null,
      nextRunAt: run?.completedAt
        ? new Date(new Date(run.completedAt).getTime() + (ml?.longRun.policy.train_interval_hours ?? 24) * 3600 * 1000).toISOString()
        : null
    }
  };
}

export function modelFailures(model: SupervisorModelReview): string[] {
  return [...model.deterministicFailures, ...model.shadowFailures, ...model.comparisonFailures];
}

const failureLabels: Record<string, string> = {
  insufficient_oos_rows: "样本外覆盖不足",
  insufficient_trades: "有效交易样本不足",
  cost_assumption_too_low: "成本压力假设不足",
  aggregate_accuracy_below_gate: "方向判断稳定性未过门",
  aggregate_net_return_below_gate: "扣成本净收益未过门",
  worst_fold_below_gate: "最弱时间折表现未过门",
  drawdown_above_gate: "最大回撤超过上限",
  unsupported_evaluation_semantics: "旧评估语义已失效",
  market_snapshot_not_current: "模型绑定的历史快照已失效",
  validation_missing: "缺少可复核验证",
  shadow_buys_insufficient: "影子买入样本不足",
  shadow_duration_insufficient: "影子观察天数不足",
  shadow_net_return_not_positive: "影子扣成本收益未转正",
  shadow_drawdown_above_limit: "影子回撤超过上限",
  champion_comparison_missing: "缺少冠军同口径基线",
  challenger_oos_improvement_insufficient: "相对冠军改善不足",
  challenger_drawdown_regression: "回撤相对冠军退化"
};

export function failureLabel(code: string): string {
  return failureLabels[code] ?? code.replaceAll("_", " ");
}

export function dominantBlocker(models: SupervisorModelReview[]): string {
  if (models.length === 0) return "尚未生成候选模型";
  const counts = new Map<string, number>();
  for (const model of models) {
    for (const failure of modelFailures(model)) counts.set(failure, (counts.get(failure) ?? 0) + 1);
  }
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
  return top ? `${failureLabel(top[0])}（${top[1]}/${models.length}）` : "候选已通过确定性门槛，等待监督决策";
}

export function runtimeNarrative(longRun: LongRunStatus | null, credentialConfigured: boolean) {
  if (!credentialConfigured) return {
    now: "公共数据训练可继续，交易连接尚未就绪",
    why: "本机未检测到可用的 OKX 凭证",
    next: "在独立凭证窗口配置后运行只读连接检查",
    tone: "warning" as const
  };
  if (!longRun) return {
    now: "正在读取长期运行状态",
    why: "本地服务尚未返回模型监督快照",
    next: "保留最后有效数据并自动重试",
    tone: "neutral" as const
  };
  if (longRun.state.runtimeStatus === "manual_review") return {
    now: "执行已进入人工核对，禁止新开仓",
    why: longRun.state.suspendedReason ?? longRun.lastError?.errorType ?? "存在无法自动证明终态的订单",
    next: "核对交易所终态与模型自有库存后再恢复",
    tone: "danger" as const
  };
  if (longRun.state.runtimeStatus === "suspended") return {
    now: "自动执行已暂停，风险出口保持可用",
    why: longRun.state.suspendedReason ?? "监督器触发了暂停条件",
    next: "查看问题中心与最近监督决策",
    tone: "danger" as const
  };
  if (longRun.activePosition) return {
    now: `正在管理 ${longRun.activePosition.remainingSize} BTC 模型自有仓位`,
    why: `由第 ${longRun.activePosition.championGeneration} 代 champion 触发，止损/止盈与最迟退出均已绑定`,
    next: `持续核对成交；计划退出 ${longRun.activePosition.exitDueAt}`,
    tone: "healthy" as const
  };
  if (!longRun.champion) return {
    now: "系统在训练和验证，当前不会开仓",
    why: dominantBlocker(longRun.review.models),
    next: "扩大历史覆盖并等待候选通过 OOS、shadow 与 Codex 审查",
    tone: "neutral" as const
  };
  if (!longRun.activeSupervisorLease) return {
    now: "已有冠军模型，但执行仍被锁定",
    why: "当前没有有效的 Codex 执行 lease",
    next: "监督器将在下一轮读取脱敏证据并决定是否签发 lease",
    tone: "warning" as const
  };
  if (longRun.state.desiredMode !== "demo") return {
    now: "冠军与监督 lease 已就绪，用户 master 仍关闭",
    why: "长期自动执行需要一次明确的用户授权",
    next: "确认风险边界后，在执行页启用 Demo master",
    tone: "warning" as const
  };
  return {
    now: "长期 Demo 自动量化正在运行",
    why: "冠军、监督 lease、审计链与用户 master 均有效",
    next: "等待下一根完成 K 线并继续执行确定性风控",
    tone: "healthy" as const
  };
}
