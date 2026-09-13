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

function Provenance({ evidence }: { evidence: Evidence }) {
  return (
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
  );
}

/**
 * One card per evidence source. A source that produced several snapshots (the
 * log tool returns one per failed invocation) shows the first in full and
 * folds the rest away, so the section reads as four kinds of evidence rather
 * than a wall of near-identical excerpts.
 */
function EvidenceCard({ group }: { group: Evidence[] }) {
  const [primary, ...rest] = group;
  return (
    <article className="evidence-card">
      <header>
        <h4>
          {sourceLabel(primary.source_type)}
          {rest.length > 0 && <span className="state-blurb"> · {group.length} snapshots</span>}
        </h4>
        <span className="observed-tag">Observed</span>
      </header>
      <EvidenceBody evidence={primary} />
      <Provenance evidence={primary} />
      {primary.truncated && <p className="state-blurb">This snapshot was truncated when stored.</p>}
      {primary.warnings.map((warning) => (
        <p className="state-blurb" key={warning}>
          {warning}
        </p>
      ))}
      <details className="raw">
        <summary>Stored snapshot (JSON)</summary>
        <pre>{JSON.stringify(primary.payload, null, 2)}</pre>
      </details>
      {rest.length > 0 && (
        <details className="raw">
          <summary>{rest.length} further snapshot(s) from this source</summary>
          <pre>
            {rest
              .map((item) => `${item.evidence_id}  ${item.observed_at}  ${item.source_ref}`)
              .join('\n')}
          </pre>
        </details>
      )}
    </article>
  );
}

/** Groups resolved snapshots by source type, preserving first-seen order. */
function groupBySource(evidenceIds: string[], slots: Record<string, Slot>): Evidence[][] {
  const groups = new Map<string, Evidence[]>();
  for (const id of evidenceIds) {
    const slot = slots[id];
    if (slot?.status !== 'ok') continue;
    const bucket = groups.get(slot.evidence.source_type);
    if (bucket) bucket.push(slot.evidence);
    else groups.set(slot.evidence.source_type, [slot.evidence]);
  }
  return [...groups.values()];
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
              {groupBySource(diagnosis.evidence_ids, slots).map((group) => (
                <EvidenceCard group={group} key={group[0].evidence_id} />
              ))}

              {diagnosis.evidence_ids
                .filter((id) => slots[id]?.status === 'error')
                .map((id) => {
                  const slot = slots[id] as Extract<Slot, { status: 'error' }>;
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
                })}

              {diagnosis.evidence_ids.some((id) => !slots[id] || slots[id].status === 'loading') && (
                <div className="evidence-card" aria-busy="true" aria-label="Loading stored snapshots">
                  <div className="skeleton" style={{ width: '40%' }} />
                  <div className="skeleton" />
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
