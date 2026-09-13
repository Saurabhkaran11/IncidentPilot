import { useEffect, useRef, useState } from 'react';

import { ApiError, api, newIdempotencyKey } from '../api';
import { formatCountdown, formatDateTime } from '../format';
import { useNow } from '../hooks';
import type { Incident, PlanResponse } from '../types';

/**
 * A refused decision is never retried silently. Each of these codes gets its
 * own explanation of *what changed*, and the only way forward is to reload and
 * look at the new plan.
 */
const REFUSAL_EXPLANATIONS: Record<string, string> = {
  STALE_PLAN: 'This plan was superseded by a newer investigation. Reload to see the current plan.',
  PLAN_DIGEST_MISMATCH:
    'The plan stored on the server is not the plan shown here. Reload and review the current plan before approving.',
  STALE_INCIDENT_VERSION:
    'The incident changed after this page loaded, so your approval would have applied to an older picture of it. Reload and review what changed.',
  PLAN_EXPIRED: 'This plan expired before it was approved. Run a new investigation to get a fresh plan.',
  DECISION_CONFLICT: 'This plan already has a decision recorded, or the incident is no longer awaiting one.',
};

interface Props {
  incident: Incident;
  onChanged: () => void;
}

export function DecisionSection({ incident, onChanged }: Props) {
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [planError, setPlanError] = useState<ApiError | null>(null);
  const [submitting, setSubmitting] = useState<'approve' | 'reject' | 'investigate' | null>(null);
  const [refusal, setRefusal] = useState<ApiError | null>(null);
  const [confirming, setConfirming] = useState(false);
  const now = useNow();

  // One idempotency key per user action, reused verbatim if the user retries
  // the same action -- that is what makes a double-clicked Approve safe.
  const keys = useRef<Record<string, string>>({});
  const keyFor = (action: string) => {
    const scope = `${action}:${incident.active_plan_id ?? incident.incident_id}:${incident.version}`;
    keys.current[scope] ??= newIdempotencyKey();
    return keys.current[scope];
  };

  const planId = incident.active_plan_id;
  useEffect(() => {
    let cancelled = false;
    setPlan(null);
    setPlanError(null);
    setConfirming(false);
    if (!planId) return;
    void (async () => {
      try {
        const response = await api.getPlan(incident.incident_id, planId);
        if (!cancelled) setPlan(response);
      } catch (caught) {
        if (!cancelled) setPlanError(caught as ApiError);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [incident.incident_id, planId]);

  const decide = async (decision: 'approve' | 'reject') => {
    if (!plan || !planId) return;
    setSubmitting(decision);
    setRefusal(null);
    try {
      await api.decide(
        incident.incident_id,
        {
          plan_id: planId,
          // Send back exactly what was displayed. If the server disagrees it
          // refuses, and we surface that rather than re-approving.
          plan_digest: plan.plan_digest,
          expected_incident_version: incident.version,
          decision,
        },
        keyFor(decision),
      );
      setConfirming(false);
      onChanged();
    } catch (caught) {
      setRefusal(caught as ApiError);
    } finally {
      setSubmitting(null);
    }
  };

  const reinvestigate = async () => {
    setSubmitting('investigate');
    setRefusal(null);
    try {
      await api.startInvestigation(incident.incident_id, incident.version, keyFor('investigate'));
      onChanged();
    } catch (caught) {
      setRefusal(caught as ApiError);
    } finally {
      setSubmitting(null);
    }
  };

  const expiresAt = plan?.presentation.expires_at ?? null;
  const remaining = expiresAt ? new Date(expiresAt).getTime() - now : 0;
  const expired = expiresAt !== null && remaining <= 0;
  const awaiting = incident.state === 'awaiting_approval';

  return (
    <section className="section" aria-labelledby="decision-heading">
      <header>
        <h3 id="decision-heading">Decision</h3>
        <span className="step">Section 3 of 4</span>
      </header>
      <div className="section-body">
        {refusal && (
          <div className="alert error" role="alert">
            <span aria-hidden="true">!</span>
            <div className="body">
              <h4>Refused: {refusal.code}</h4>
              <p>{refusal.message}</p>
              <p>{REFUSAL_EXPLANATIONS[refusal.code] ?? 'Reload the incident and review it again.'}</p>
              <button
                type="button"
                className="button small"
                onClick={() => {
                  setRefusal(null);
                  onChanged();
                }}
              >
                Reload incident
              </button>
            </div>
          </div>
        )}

        {incident.state === 'rejected' && (
          <div className="alert" role="status">
            <span aria-hidden="true">⊘</span>
            <div className="body">
              <h4>Plan rejected</h4>
              <p>An operator rejected this plan. Nothing was changed in the environment.</p>
            </div>
          </div>
        )}

        {incident.state === 'needs_information' && (
          <div className="alert warn" role="status">
            <span aria-hidden="true">?</span>
            <div className="body">
              <h4>Needs information</h4>
              <p>
                The evidence did not support a bounded action, so no plan was prepared. The missing
                evidence is listed below.
              </p>
            </div>
          </div>
        )}

        {incident.diagnosis && (
          <>
            <div className="field-label">Plain-language cause</div>
            <p>{incident.diagnosis.summary}</p>

            {incident.diagnosis.hypotheses.length > 1 && (
              <>
                <div className="field-label">Alternatives considered</div>
                <ul className="plain">
                  {incident.diagnosis.hypotheses.slice(1).map((hypothesis) => (
                    <li key={hypothesis.description}>
                      {hypothesis.description}
                      {hypothesis.contradicting_evidence_ids.length > 0 && (
                        <> — contradicted by {hypothesis.contradicting_evidence_ids.length} snapshot(s)</>
                      )}
                    </li>
                  ))}
                </ul>
              </>
            )}

            <div className="field-label">Unknowns</div>
            {incident.diagnosis.unknowns.length === 0 ? (
              <p className="state-blurb">The investigation reported no open unknowns.</p>
            ) : (
              <ul className="plain">
                {incident.diagnosis.unknowns.map((unknown) => (
                  <li key={unknown}>{unknown}</li>
                ))}
              </ul>
            )}
          </>
        )}

        {planError && (
          <div className="alert error" role="alert">
            <span aria-hidden="true">!</span>
            <div className="body">
              <h4>Could not load the plan</h4>
              <p>{planError.message}</p>
            </div>
          </div>
        )}

        {!planId && !planError && incident.state !== 'rejected' && (
          <p className="state-blurb" style={{ marginTop: 14 }}>
            No recovery plan is currently attached to this incident.
          </p>
        )}

        {planId && !plan && !planError && (
          <div aria-busy="true" style={{ marginTop: 14 }}>
            <div className="skeleton" style={{ width: '60%' }} />
            <div className="skeleton" />
          </div>
        )}

        {plan && (
          <>
            <div className="field-label">Exact change</div>
            <div className="change-line">
              <span>{plan.plan.function_name}</span>
              <span className="arrow" aria-hidden="true">
                ·
              </span>
              <span>alias {plan.plan.alias_name}</span>
              <span className="arrow" aria-hidden="true">
                →
              </span>
              <span className="from">version {plan.plan.from_version}</span>
              <span className="arrow" aria-hidden="true">
                →
              </span>
              <span className="to">version {plan.plan.to_version}</span>
            </div>
            <p className="state-blurb">{plan.presentation.action}</p>

            <div className="field-label">
              Affected request IDs ({plan.presentation.affected_request_ids.length})
            </div>
            {plan.presentation.affected_request_ids.length === 0 ? (
              <p className="state-blurb">
                No requests are included in this plan. Failed requests will stay pending manual
                reconciliation.
              </p>
            ) : (
              <ul className="chip-list">
                {plan.presentation.affected_request_ids.map((id) => (
                  <li className="chip" key={id}>
                    {id}
                  </li>
                ))}
              </ul>
            )}

            <div className="field-label">Plan expiry</div>
            <p className={`expiry${remaining < 60_000 ? ' urgent' : ''}`}>
              <span aria-hidden="true">{expired ? '⏱' : '⏳'}</span>
              <span>
                {expired ? 'Expired' : `Expires in ${formatCountdown(plan.presentation.expires_at, now)}`}
              </span>
              <span className="state-blurb">({formatDateTime(plan.presentation.expires_at)})</span>
            </p>

            {awaiting && expired && (
              <div className="alert warn" role="alert">
                <span aria-hidden="true">!</span>
                <div className="body">
                  <h4>Plan expired</h4>
                  <p>An expired plan cannot be approved. Run a new investigation to prepare a fresh one.</p>
                  <button
                    type="button"
                    className="button small"
                    onClick={() => void reinvestigate()}
                    disabled={submitting !== null}
                  >
                    Re-investigate
                  </button>
                </div>
              </div>
            )}

            {awaiting && !expired && (
              <>
                <p className="confirmation" id="approve-confirmation">
                  {plan.presentation.confirmation}
                </p>
                {confirming ? (
                  <div className="decision-actions sticky">
                    <button
                      type="button"
                      className="button primary"
                      onClick={() => void decide('approve')}
                      disabled={submitting !== null}
                      aria-describedby="approve-confirmation"
                    >
                      {submitting === 'approve' ? 'Approving…' : 'Confirm approval'}
                    </button>
                    <button
                      type="button"
                      className="button"
                      onClick={() => setConfirming(false)}
                      disabled={submitting !== null}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div className="decision-actions sticky">
                    <button
                      type="button"
                      className="button primary"
                      onClick={() => setConfirming(true)}
                      disabled={submitting !== null}
                      aria-describedby="approve-confirmation"
                    >
                      Approve this plan
                    </button>
                    <button
                      type="button"
                      className="button danger"
                      onClick={() => void decide('reject')}
                      disabled={submitting !== null}
                    >
                      {submitting === 'reject' ? 'Rejecting…' : 'Reject'}
                    </button>
                  </div>
                )}
              </>
            )}

            <details className="raw">
              <summary>Plan JSON and infrastructure identifiers</summary>
              <pre>
                {`plan_digest: ${plan.plan_digest}\naccount:     ${plan.plan.account_id}\nregion:      ${plan.plan.region}\nexpected alias revision: ${plan.plan.expected_alias_revision_id}\ngood version fingerprint: ${plan.plan.good_version_fingerprint}\nverification profile: ${plan.plan.verification_profile}\npolicy version: ${plan.plan.policy_version}\n\n${JSON.stringify(plan.plan, null, 2)}`}
              </pre>
            </details>
          </>
        )}

        {(incident.state === 'rejected' || incident.state === 'needs_information') && (
          <div className="decision-actions" style={{ marginTop: 14 }}>
            <button
              type="button"
              className="button"
              onClick={() => void reinvestigate()}
              disabled={submitting !== null}
            >
              {submitting === 'investigate' ? 'Queueing…' : 'Start a new investigation'}
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
