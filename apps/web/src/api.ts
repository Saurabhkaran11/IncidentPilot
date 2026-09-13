/**
 * The only place this app talks to the network.
 *
 * Two rules the rest of the UI depends on:
 *  - every error surfaces as `ApiError` carrying the server's `code` and
 *    `message`, so components can branch on STALE_PLAN etc. and still show
 *    the server's own wording;
 *  - the operator token lives in this module's closure only. Never
 *    localStorage, never sessionStorage, never a URL -- a reload drops it,
 *    which matches a boot-scoped 30-minute token.
 */

import type {
  AcceptedResponse,
  Capabilities,
  DemoRun,
  Evidence,
  EventsResponse,
  Incident,
  IncidentListResponse,
  Operation,
  PlanResponse,
  Receipt,
} from './types';

let operatorToken: string | null = null;

export function setOperatorToken(token: string | null): void {
  operatorToken = token && token.trim() ? token.trim() : null;
}

export function hasOperatorToken(): boolean {
  return operatorToken !== null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryable: boolean,
    readonly requestId: string | null,
    readonly details: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
    request_id?: string | null;
    details?: unknown;
  };
}

async function request<T>(
  path: string,
  init: RequestInit & { idempotencyKey?: string } = {},
): Promise<T> {
  const { idempotencyKey, ...rest } = init;
  const headers = new Headers(rest.headers);
  if (rest.body) headers.set('Content-Type', 'application/json');
  if (idempotencyKey) headers.set('Idempotency-Key', idempotencyKey);
  if (operatorToken) headers.set('Authorization', `Bearer ${operatorToken}`);

  let response: Response;
  try {
    response = await fetch(path, { ...rest, headers });
  } catch (cause) {
    throw new ApiError(
      0,
      'NETWORK_UNAVAILABLE',
      'Could not reach the control API. Is it running on 127.0.0.1:8080?',
      true,
      null,
      cause,
    );
  }

  const text = await response.text();
  if (!response.ok) {
    let envelope: ErrorEnvelope = {};
    try {
      envelope = text ? (JSON.parse(text) as ErrorEnvelope) : {};
    } catch {
      /* non-JSON error body; fall through to the generic message below */
    }
    const error = envelope.error;
    throw new ApiError(
      response.status,
      error?.code ?? `HTTP_${response.status}`,
      error?.message ?? `Request failed with status ${response.status}.`,
      error?.retryable ?? false,
      error?.request_id ?? null,
      error?.details ?? null,
    );
  }
  return (text ? JSON.parse(text) : null) as T;
}

/** One key per user action; reuse it verbatim when the user retries. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

export const api = {
  capabilities: () => request<Capabilities>('/v1/capabilities'),

  listIncidents: () => request<IncidentListResponse>('/v1/incidents?limit=50'),

  getIncident: (id: string) => request<Incident>(`/v1/incidents/${id}`),

  listEvents: (id: string, afterSequence: number) =>
    request<EventsResponse>(`/v1/incidents/${id}/events?after_sequence=${afterSequence}&limit=100`),

  getEvidence: (incidentId: string, evidenceId: string) =>
    request<Evidence>(`/v1/incidents/${incidentId}/evidence/${evidenceId}`),

  getPlan: (incidentId: string, planId: string) =>
    request<PlanResponse>(`/v1/incidents/${incidentId}/plans/${planId}`),

  getOperation: (operationId: string) => request<Operation>(`/v1/operations/${operationId}`),

  getReceipt: (incidentId: string) => request<Receipt>(`/v1/incidents/${incidentId}/receipt`),

  getReceiptMarkdown: async (incidentId: string): Promise<string> => {
    const headers = new Headers();
    if (operatorToken) headers.set('Authorization', `Bearer ${operatorToken}`);
    const response = await fetch(`/v1/incidents/${incidentId}/receipt?format=markdown`, { headers });
    if (!response.ok) {
      throw new ApiError(response.status, 'RECEIPT_NOT_READY', 'The receipt is not available yet.', false, null, null);
    }
    return response.text();
  },

  decide: (
    incidentId: string,
    body: {
      plan_id: string;
      plan_digest: string;
      expected_incident_version: number;
      decision: 'approve' | 'reject';
    },
    idempotencyKey: string,
  ) =>
    request<AcceptedResponse>(`/v1/incidents/${incidentId}/decisions`, {
      method: 'POST',
      body: JSON.stringify(body),
      idempotencyKey,
    }),

  startInvestigation: (incidentId: string, expectedVersion: number, idempotencyKey: string) =>
    request<AcceptedResponse>(`/v1/incidents/${incidentId}/investigations`, {
      method: 'POST',
      body: JSON.stringify({ expected_incident_version: expectedVersion }),
      idempotencyKey,
    }),

  createDemoRun: (appId: string, idempotencyKey: string) =>
    request<DemoRun>('/v1/demo/runs', {
      method: 'POST',
      body: JSON.stringify({ app_id: appId }),
      idempotencyKey,
    }),

  submitDemoRequest: (
    demoRunId: string,
    fixtureId: string,
    idempotencyKey: string,
  ) =>
    request<AcceptedResponse>('/v1/demo/requests', {
      method: 'POST',
      body: JSON.stringify({
        demo_run_id: demoRunId,
        document: { fixture_id: fixtureId, document_type: 'invoice', page_count: 2 },
      }),
      idempotencyKey,
    }),
};
