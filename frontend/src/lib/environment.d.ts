import type { EnvironmentMode, EnvironmentStatus, SystemStatus } from "../types";

export function environmentFromSystemStatus(status: SystemStatus | null | undefined): Exclude<EnvironmentMode, "unknown"> | null;
export function resolveEnvironmentMode(input: {
  lastKnown: EnvironmentMode;
  systemStatus: SystemStatus | null | undefined;
  environmentStatus: EnvironmentStatus | null | undefined;
}): EnvironmentMode;
export function environmentUiPolicy(mode: EnvironmentMode): {
  showLiveBanner: boolean;
  executionLocked: boolean;
  manualArmPhrase: string;
  liveAutomationAvailable: false;
};
export function canConfirmEnvironmentChallenge(input: {
  now: number;
  readyAt: string;
  expiresAt: string;
  phrase: string;
  expectedPhrase: string;
  allAcknowledged: boolean;
  transitionLocked: boolean;
}): boolean;
