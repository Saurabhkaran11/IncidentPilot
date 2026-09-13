import { useEffect, useState } from 'react';

import { ApiError, api } from '../api';
import { REQUEST_OUTCOME_META, formatDateTime, formatDuration } from '../format';
import type { Incident, Receipt } from '../types';
import { Badge } from './Badge';

const RECEIPT_OUTCOME_META = {
  recovered: { tone: 'good' as const, label: 'Recovered' },
  partial: { tone: 'warn' as const, label: 'Partial recovery' },
  needs_attention: { tone: 'bad' as const, label: 'Needs attention' },
};

const CHECK_LABELS: Record<string, string> = {
  alias_version: 'Alias points at the approved version',
  canary_invocation: 'Canary invocation executed on that version',
  canary_result_and_hash: 'Canary wrote a durable result with a matching hash',
};

function download(filename: string, contents: string) {
  const url = URL.createObjectURL(new Blob([contents], { type: 'text/markdown;charset=utf-8' }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

/**
 * Section 4 -- the product's central artifact. Two rules it enforces: a failed
 * check never renders green, and unresolved requests stay visible.
 */
export function ReceiptSection({ incident }: { incident: Incident }) {
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [exporting, setExporting] = useState(false);

  // The incident's `latest_receipt_id` is not always populated, so the
  // terminal state is what tells us a receipt should exist by now.
  const expectReceipt =
    incident.latest_receipt_id !== null ||
    incident.state === 'recovered' ||
    incident.state === 'service_restored_pending_requests' ||
    incident.state === 'needs_attention';

  useEffect(() => {
    let cancelled = false;
    if (!expectReceipt) {
      setReceipt(null);
      setError(null);
      return;
    }
    void (async () => {
      try {
        const next = await api.getReceipt(incident.incident_id);
        if (!cancelled) {
          setReceipt(next);
          setError(null);
        }
      } catch (caught) {
        if (!cancelled) setError(caught as ApiError);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [incident.incident_id, incident.state, incident.latest_receipt_id, expectReceipt]);

  const exportMarkdown = async () => {
    setExporting(true);
    try {
      const markdown = await api.getReceiptMarkdown(incident.incident_id);
      download(`receipt-${incident.incident_id}.md`, markdown);
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setExporting(false);
    }
  };

  const outcome = receipt ? RECEIPT_OUTCOME_META[receipt.outcome] : null;
  const failedChecks = receipt?.verification.filter((check) => !check.passed) ?? [];

  return (
    <section className="section" aria-labelledby="receipt-heading">
      <header>
        <h3 id="receipt-heading">Recovery receipt</h3>
        <span className="step">Section 4 of 4</span>
      </header>
      <div className="section-body">
        {!expectReceipt && (
          <p className="state-blurb">
            The receipt is written once an approved action has been applied and verified. It will account
            for every affected request, recovered or not.
          </p>
        )}

        {expectReceipt && !receipt && error?.code === 'RECEIPT_NOT_READY' && (
          <p className="state-blurb" role="status">
            The execution finished but the receipt is not written yet. It appears here shortly.
          </p>
        )}

        {expectReceipt && !receipt && error && error.code !== 'RECEIPT_NOT_READY' && (
          <div className="alert error" role="alert">
            <span aria-hidden="true">!</span>
            <div className="body">
              <h4>Could not load the receipt</h4>
              <p>{error.message}</p>
            </div>
          </div>
        )}

        {expectReceipt && !receipt && !error && (
          <div aria-busy="true">
            <div className="skeleton" style={{ width: '50%' }} />
            <div className="skeleton" />
          </div>
        )}

        {receipt && outcome && (
          <>
            <div className="alert" role="status" style={{ alignItems: 'center' }}>
              <Badge tone={outcome.tone}>{outcome.label}</Badge>
              <div className="body">
                <p>
                  Rolled <code>{receipt.applied_version ? `to version ${receipt.applied_version}` : 'no version'}</code>{' '}
                  under approval <code>{receipt.approval_id}</code>. Receipt {receipt.receipt_id}, written{' '}
                  {formatDateTime(receipt.created_at)}.
                </p>
              </div>
            </div>

            {failedChecks.length > 0 && (
              <div className="alert error" role="alert">
                <span aria-hidden="true">!</span>
                <div className="body">
                  <h4>
                    {failedChecks.length} verification check{failedChecks.length === 1 ? '' : 's'} failed
                  </h4>
                  <p>This incident is not recovered. Do not treat the service as healthy.</p>
                </div>
              </div>
            )}

            <div className="field-label">Verification checks</div>
            <ul className="check-list">
              {receipt.verification.map((check) => (
                <li className={`check ${check.passed ? 'passed' : 'failed'}`} key={check.check}>
                  <span className="mark" aria-hidden="true">
                    {check.passed ? '✓' : '✕'}
                  </span>
                  <div>
                    <div className="name">
                      {CHECK_LABELS[check.check] ?? check.check}{' '}
                      <span className="state-blurb">— {check.passed ? 'passed' : 'FAILED'}</span>
                    </div>
                    <div className="details">
                      {check.details ?? check.check} · {formatDateTime(check.observed_at)}
                    </div>
                  </div>
                </li>
              ))}
            </ul>

            <div className="field-label">Per-request outcomes ({receipt.requests.length})</div>
            <ul className="request-list">
              {receipt.requests.map((request) => {
                const meta = REQUEST_OUTCOME_META[request.outcome];
                return (
                  <li className="request-row" key={request.request_id}>
                    <code>{request.request_id}</code>
                    <Badge tone={meta.tone}>{meta.label}</Badge>
                  </li>
                );
              })}
            </ul>

            {receipt.unresolved_request_ids.length > 0 ? (
              <div className="unresolved" role="alert">
                <strong>
                  {receipt.unresolved_request_ids.length} request
                  {receipt.unresolved_request_ids.length === 1 ? '' : 's'} still unresolved
                </strong>
                <ul className="chip-list" style={{ marginTop: 6 }}>
                  {receipt.unresolved_request_ids.map((id) => (
                    <li className="chip" key={id}>
                      {id}
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="state-blurb" style={{ marginTop: 10 }}>
                No unresolved requests. Every request in the approved batch has a recorded outcome.
              </p>
            )}

            <div className="field-label">Elapsed</div>
            <dl className="kv">
              {Object.entries(receipt.timing_ms).map(([phase, ms]) => (
                <div key={phase} style={{ display: 'contents' }}>
                  <dt>{phase}</dt>
                  <dd>{formatDuration(ms)}</dd>
                </div>
              ))}
              <dt>model</dt>
              <dd>{receipt.usage.model_id}</dd>
            </dl>

            <div className="decision-actions" style={{ marginTop: 16 }}>
              <button type="button" className="button" onClick={() => void exportMarkdown()} disabled={exporting}>
                {exporting ? 'Exporting…' : 'Export receipt (Markdown)'}
              </button>
            </div>

            <details className="raw">
              <summary>Receipt JSON</summary>
              <pre>{JSON.stringify(receipt, null, 2)}</pre>
            </details>
          </>
        )}
      </div>
    </section>
  );
}
