/**
 * Hand-written mirror of contracts/openapi/incidentpilot.openapi.json.
 *
 * Why not generated: the three payloads the UI works hardest on -- `plan`,
 * `presentation`, and the receipt -- are declared `additionalProperties: true`
 * in the OpenAPI document, so a generator emits `Record<string, unknown>` for
 * exactly the shapes that need types. These were written against real
 * responses from the running fixture API; no field here is invented.
 */

export type Mode = 'fixture' | 'aws_live';

export type IncidentState =
  | 'detected'
  | 'investigating'
  | 'needs_information'
  | 'awaiting_approval'
  | 'rejected'
  | 'applying'
  | 'verifying'
  | 'replaying'
  | 'recovered'
  | 'service_restored_pending_requests'
  | 'needs_attention';

export type OperationStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'needs_attention';
export type OperationPhase = 'validate' | 'apply' | 'verify' | 'replay' | 'receipt';
export type Assessment = 'supported' | 'insufficient_evidence';
export type SourceType =
  | 'cloudwatch_log'
  | 'lambda_configuration'
  | 'deployment_manifest'
  | 'request_inbox'
  | 'runbook';

export interface Capabilities {
  schema_version: string;
  mode: Mode;
  agent_mode: string;
  features: {
    mcp_evidence: boolean;
    alias_rollback: boolean;
    bounded_replay: boolean;
    agentcore_runtime: boolean;
  };
  applications: { app_id: string; label: string }[];
}

export interface Impact {
  failed_requests: number;
  completed_requests: number;
  first_failure_at: string | null;
  service: string;
}

export interface Hypothesis {
  description: string;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
}

export interface Diagnosis {
  assessment: Assessment;
  summary: string;
  evidence_ids: string[];
  unknowns: string[];
  recommended_action: string;
  hypotheses: Hypothesis[];
  agent_mode: string;
  model_id: string;
  tool_call_count: number;
  duration_ms: number;
}

export interface IncidentListItem {
  incident_id: string;
  app_id: string;
  state: IncidentState;
  mode: Mode;
  failed_requests: number;
  updated_at: string;
}

export interface IncidentListResponse {
  items: IncidentListItem[];
  next_cursor: string | null;
}

export interface Incident {
  incident_id: string;
  version: number;
  app_id: string;
  demo_run_id: string;
  state: IncidentState;
  mode: Mode;
  impact: Impact;
  diagnosis: Diagnosis | null;
  active_plan_id: string | null;
  active_operation_id: string | null;
  latest_receipt_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface IncidentEvent {
  sequence: number;
  event_id: string;
  type: string;
  occurred_at: string;
  data: Record<string, unknown>;
}

export interface EventsResponse {
  items: IncidentEvent[];
  last_sequence: number;
  has_more: boolean;
}

export interface Evidence {
  evidence_id: string;
  incident_id: string;
  run_id: string;
  source_type: SourceType;
  source_ref: string;
  observed_at: string;
  retrieved_at: string;
  payload_sha256: string;
  payload: Record<string, unknown>;
  truncated: boolean;
  warnings: string[];
}

export interface ReplayRequestRef {
  request_id: string;
  payload_sha256: string;
}

/** `PlanResponse.plan` -- mirrors packages/domain/plan.py RecoveryPlan. */
export interface RecoveryPlan {
  schema_version: string;
  plan_id: string;
  incident_id: string;
  workspace_id: string;
  app_id: string;
  account_id: string;
  region: string;
  action: string;
  function_name: string;
  alias_name: string;
  from_version: string;
  to_version: string;
  expected_alias_revision_id: string;
  routing: string;
  good_version_fingerprint: string;
  verification_profile: string;
  replay_requests: ReplayRequestRef[];
  evidence_ids: string[];
  policy_version: string;
  expires_at: string;
  created_at: string;
}

export interface PlanResponse {
  plan: RecoveryPlan;
  plan_digest: string;
  /** Server-authored operator copy. Never re-word `confirmation` client-side. */
  presentation: {
    confirmation: string;
    action: string;
    expires_at: string;
    affected_request_ids: string[];
  };
}

export interface VerificationCheck {
  check: string;
  passed: boolean;
  observed_at: string;
  evidence_id: string | null;
  details: string | null;
}

export type RequestOutcome =
  | 'completed'
  | 'already_completed'
  | 'still_failed'
  | 'not_replayed'
  | 'conflict';

export interface RequestExecutionOutcome {
  request_id: string;
  outcome: RequestOutcome;
  result_id: string | null;
  details: string | null;
}

export interface Receipt {
  schema_version: string;
  receipt_id: string;
  incident_id: string;
  mode: Mode;
  outcome: 'recovered' | 'partial' | 'needs_attention';
  plan_id: string;
  approval_id: string;
  execution_id: string;
  applied_version: string | null;
  verification: VerificationCheck[];
  requests: RequestExecutionOutcome[];
  unresolved_request_ids: string[];
  timing_ms: Record<string, number>;
  usage: {
    model_id: string;
    input_tokens: number | null;
    output_tokens: number | null;
    estimated_cost_usd: number | null;
  };
  created_at: string;
}

export interface Operation {
  operation_id: string;
  kind: string;
  incident_id: string;
  status: OperationStatus;
  phase: OperationPhase | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  result: Record<string, unknown> | null;
}

export interface AcceptedResponse {
  resource_id: string;
  operation_id: string;
  status_url: string;
  status?: string;
  request_id?: string | null;
  incident_id?: string | null;
  approval_id?: string | null;
}

export interface DemoRun {
  demo_run_id: string;
  app_id: string;
  created_at: string;
  mode: Mode;
  max_requests: number;
}

/** States where the server still has work in flight, so the UI keeps polling. */
export const ACTIVE_STATES: ReadonlySet<IncidentState> = new Set<IncidentState>([
  'detected',
  'investigating',
  'applying',
  'verifying',
  'replaying',
]);

export const TERMINAL_STATES: ReadonlySet<IncidentState> = new Set<IncidentState>([
  'rejected',
  'recovered',
  'service_restored_pending_requests',
  'needs_attention',
]);
