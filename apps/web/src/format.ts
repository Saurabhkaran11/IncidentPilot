/** Presentation-only helpers. No `Date` library: `Intl` already does this. */

import type { IncidentState, RequestOutcome } from './types';

const TIME = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

const DATETIME = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'medium',
});

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : TIME.format(date);
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : DATETIME.format(date);
}

/** "4m 12s" / "expired". Used for the plan expiry countdown. */
export function formatCountdown(targetIso: string, now: number): string {
  const remaining = new Date(targetIso).getTime() - now;
  if (Number.isNaN(remaining)) return '—';
  if (remaining <= 0) return 'expired';
  const totalSeconds = Math.floor(remaining / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${String(seconds).padStart(2, '0')}s` : `${seconds}s`;
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms / 60_000)} min`;
}

/**
 * Operator-facing wording for each incident state. `tone` drives colour, and
 * every place that uses it also renders this text -- colour is never the only
 * signal.
 */
export const STATE_META: Record<
  IncidentState,
  { label: string; tone: 'neutral' | 'busy' | 'pending' | 'good' | 'warn' | 'bad'; blurb: string }
> = {
  detected: { label: 'Detected', tone: 'neutral', blurb: 'Failure recorded. Investigation queued.' },
  investigating: { label: 'Investigating', tone: 'busy', blurb: 'The agent is collecting evidence.' },
  needs_information: {
    label: 'Needs information',
    tone: 'warn',
    blurb: 'Evidence was insufficient to support an action. No plan was prepared.',
  },
  awaiting_approval: {
    label: 'Awaiting approval',
    tone: 'pending',
    blurb: 'A bounded recovery plan is ready for a human decision.',
  },
  rejected: { label: 'Rejected', tone: 'neutral', blurb: 'An operator rejected the proposed plan. Nothing was changed.' },
  applying: { label: 'Applying', tone: 'busy', blurb: 'The executor is applying the approved alias rollback.' },
  verifying: { label: 'Verifying', tone: 'busy', blurb: 'Running independent recovery checks.' },
  replaying: { label: 'Replaying', tone: 'busy', blurb: 'Replaying only the approved request IDs.' },
  recovered: { label: 'Recovered', tone: 'good', blurb: 'Checks passed and every affected request is accounted for.' },
  service_restored_pending_requests: {
    label: 'Service restored, requests pending',
    tone: 'warn',
    blurb: 'The service verified healthy, but some requests are still unresolved.',
  },
  needs_attention: {
    label: 'Needs attention',
    tone: 'bad',
    blurb: 'A check failed or the outcome is unknown. This incident is not recovered.',
  },
};

export const REQUEST_OUTCOME_META: Record<
  RequestOutcome,
  { label: string; tone: 'good' | 'warn' | 'bad' }
> = {
  completed: { label: 'Completed', tone: 'good' },
  already_completed: { label: 'Already completed', tone: 'good' },
  still_failed: { label: 'Still failed', tone: 'bad' },
  not_replayed: { label: 'Not replayed', tone: 'warn' },
  conflict: { label: 'Conflict', tone: 'bad' },
};

const EVENT_LABELS: Record<string, string> = {
  'incident.detected': 'Incident detected',
  'investigation.started': 'Investigation started',
  'investigation.tool_started': 'Evidence tool started',
  'investigation.tool_completed': 'Evidence tool completed',
  'investigation.completed': 'Investigation completed',
  'plan.created': 'Recovery plan prepared',
  'plan.invalidated': 'Plan invalidated',
  'decision.recorded': 'Decision recorded',
  'execution.started': 'Execution started',
  'execution.reconciled': 'Alias change reconciled',
  'verification.completed': 'Verification completed',
  'replay.started': 'Replay started',
  'request.reconciled': 'Request reconciled',
  'receipt.created': 'Recovery receipt created',
  'incident.needs_attention': 'Escalated: needs attention',
};

export function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type;
}

const SOURCE_LABELS: Record<string, string> = {
  cloudwatch_log: 'Error excerpt',
  lambda_configuration: 'Deployment change',
  deployment_manifest: 'Configuration difference',
  request_inbox: 'Affected requests',
  runbook: 'Runbook guidance',
};

export function sourceLabel(sourceType: string): string {
  return SOURCE_LABELS[sourceType] ?? sourceType;
}

export function shortHash(hash: string): string {
  return hash.length > 16 ? `${hash.slice(0, 12)}…` : hash;
}
