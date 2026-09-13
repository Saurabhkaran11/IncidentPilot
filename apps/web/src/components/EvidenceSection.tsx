import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';

import { ApiError, api } from '../api';
import { formatDateTime, shortHash, sourceLabel } from '../format';
import type { Diagnosis, Evidence } from '../types';

type Slot = { status: 'loading' } | { status: 'ok'; evidence: Evidence } | { status: 'error'; error: ApiError };

function useEvidence(incidentId: string, evidenceIds: string[]) {
  const [slots, setSlots] = useState<Record<string, Slot>>({});
  const key = evidenceIds.join(',');

  const fetchOne = useCallback(
    async (id: string) => {
      setSlots((previous) => ({ ...previous, [id]: { status: 'loading' } }));
      try {
        const evidence = await api.getEvidence(incidentId, id);
        setSlots((previous) => ({ ...previous, [id]: { status: 'ok', evidence } }));
      } catch (caught) {
        setSlots((previous) => ({ ...previous, [id]: { status: 'error', error: caught as ApiError } }));
      }
    },
    [incidentId],
  );

  useEffect(() => {
    let cancelled = false;
    setSlots({});
    void (async () => {
      for (const id of key ? key.split(',') : []) {
        if (cancelled) return;
        await fetchOne(id);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [key, fetchOne]);

  return { slots, retry: fetchOne };
}

function str(payload: Record<string, unknown>, field: string): string | null {
  const value = payload[field];
  return typeof value === 'string' || typeof value === 'number' ? String(value) : null;
}

/** Renders the observed snapshot for each source type. Text only, escaped by React. */
function EvidenceBody({ evidence }: { evidence: Evidence }): ReactNode {
  const p = evidence.payload;

  switch (evidence.source_type) {
    case 'cloudwatch_log':
      return (
        <>
          <p className="evidence-quote">
            {str(p, 'error_type')}: {str(p, 'message')}
          </p>
          {str(p, 'request_id') && (
            <dl className="evidence-facts" style={{ marginTop: 8 }}>
              <dt>Request</dt>
              <dd>{str(p, 'request_id')}</dd>
            </dl>
          )}
        </>
      );

    case 'lambda_configuration': {
      const changes = Array.isArray(p.changes) ? (p.changes as Record<string, unknown>[]) : [];
      return (
        <>
          <dl className="evidence-facts">
            <dt>Alias</dt>
            <dd>
              {str(p, 'alias_name')} → version {str(p, 'current_version')}
            </dd>
            <dt>Known good</dt>
            <dd>version {str(p, 'known_good_version')}</dd>
            <dt>Weighted routing</dt>
            <dd>{p.weighted_routing ? 'yes' : 'no'}</dd>
            <dt>Schema compatible</dt>
            <dd>{p.schema_compatible ? 'yes' : 'no'}</dd>
          </dl>
          {changes.map((change, index) => (
            <p className="evidence-quote" key={index}>
              {String(change.field)}: {String(change.before)} → {String(change.after)}
            </p>
          ))}
        </>
      );
    }

    case 'deployment_manifest':
      return (
        <dl className="evidence-facts">
          <dt>Expected results table</dt>
          <dd>{str(p, 'expected_results_table_ref')}</dd>
          <dt>Failed requests</dt>
          <dd>{str(p, 'failed_request_count')}</dd>
          <dt>Runbook</dt>
          <dd>{str(p, 'allowed_runbook_id')}</dd>
        </dl>
      );

    case 'request_inbox':
      return (
        <dl className="evidence-facts">
          <dt>Failed</dt>
          <dd>{str(p, 'failed')}</dd>
          <dt>Completed</dt>
          <dd>{str(p, 'completed')}</dd>
          <dt>Pending</dt>
          <dd>{str(p, 'pending')}</dd>
          <dt>Total in run</dt>
          <dd>{str(p, 'total')}</dd>
        </dl>
      );

    case 'runbook':
      return (
        <dl className="evidence-facts">
          <dt>Runbook</dt>
          <dd>
            {str(p, 'runbook_id')} v{str(p, 'version')}
          </dd>
          <dt>Title</dt>
          <dd>{str(p, 'title')}</dd>
        </dl>
      );

    default:
      return null;
  }
}

function EvidenceCard({ evidence }: { evidence: Evidence }) {
  return (
    <article className="evidence-card">
      <header>
        <h4>{sourceLabel(evidence.source_type)}</h4>
        <span className="observed-tag">Observed</span>
      </header>
      <EvidenceBody evidence={evidence} />
      <dl className="evidence-facts" style={{ marginTop: 8 }}>
        <dt>Source</dt>
        <dd>{evidence.source_ref}</dd>
        <dt>Observed at</dt>
        <dd>{formatDateTime(evidence.observed_at)}</dd>
        <dt>Snapshot</dt>
        <dd title={evidence.payload_sha256}>
          {evidence.evidence_id} · sha256 {shortHash(evidence.payload_sha256)}
        </dd>
      </dl>
      {evidence.truncated && <p className="state-blurb">This snapshot was truncated when stored.</p>}
      {evidence.warnings.length > 0 && (
        <ul className="plain">
          {evidence.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
      <details className="raw">
        <summary>Stored snapshot (JSON)</summary>
        <pre>{JSON.stringify(evidence.payload, null, 2)}</pre>
      </details>
    </article>
  );
}

/**
 * Section 2. Stored snapshots are rendered as "Observed" cards; the agent's
 * reading of them sits in a visually distinct interpretation block so the two
 * are never confused.
 */
export function EvidenceSection({
  incidentId,
  diagnosis,
}: {
  incidentId: string;
  diagnosis: Diagnosis | null;
}) {
  const { slots, retry } = useEvidence(incidentId, diagnosis?.evidence_ids ?? []);

  return (
    <section className="section" aria-labelledby="evidence-heading">
      <header>
        <h3 id="evidence-heading">Evidence</h3>
        <span className="step">Section 2 of 4</span>
      </header>
      <div className="section-body">
        {!diagnosis && (
          <p className="state-blurb">
            No evidence has been collected yet. It appears here once the investigation runs.
          </p>
        )}

        {diagnosis && (
          <>
            <div className="interpretation">
              <div className="tag">
                <span aria-hidden="true">◆</span> Model interpretation — not an observation
              </div>
              <p>{diagnosis.summary}</p>
              {diagnosis.hypotheses.length > 0 && (
                <>
                  <div className="field-label">Causes considered</div>
                  <ul className="plain">
                    {diagnosis.hypotheses.map((hypothesis) => (
                      <li key={hypothesis.description}>
                        {hypothesis.description}{' '}
                        <span className="state-blurb">
                          ({hypothesis.supporting_evidence_ids.length} supporting,{' '}
                          {hypothesis.contradicting_evidence_ids.length} contradicting)
                        </span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              <p className="model-note">
                assessment={diagnosis.assessment} · agent_mode={diagnosis.agent_mode} · model=
                {diagnosis.model_id} · {diagnosis.tool_call_count} tool calls · {diagnosis.duration_ms} ms
              </p>
            </div>

            <div className="field-label">Stored source snapshots</div>
            <div className="evidence-grid">
              {diagnosis.evidence_ids.map((id) => {
                const slot = slots[id];
                if (!slot || slot.status === 'loading') {
                  return (
                    <div className="evidence-card" key={id} aria-busy="true">
                      <div className="skeleton" style={{ width: '40%' }} />
                      <div className="skeleton" />
                    </div>
                  );
                }
                if (slot.status === 'error') {
                  return (
                    <article className="evidence-card unavailable" key={id}>
                      <header>
                        <h4>Evidence unavailable</h4>
                        <span className="observed-tag">{id}</span>
                      </header>
                      <p>{slot.error.message}</p>
                      <button type="button" className="button small" onClick={() => void retry(id)}>
                        Retry
                      </button>
                    </article>
                  );
                }
                return <EvidenceCard evidence={slot.evidence} key={id} />;
              })}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
