import { STATE_META, formatDateTime } from '../format';
import type { IncidentStreamState } from '../hooks';
import { StateBadge } from './Badge';
import { DecisionSection } from './DecisionSection';
import { EvidenceSection } from './EvidenceSection';
import { ImpactSection } from './ImpactSection';
import { ReceiptSection } from './ReceiptSection';

export function IncidentDetail({
  stream,
  onChanged,
}: {
  stream: IncidentStreamState;
  onChanged: () => void;
}) {
  const { incident, loading, error, live } = stream;

  if (loading && !incident) {
    return (
      <div className="section" aria-busy="true" aria-label="Loading incident">
        <div className="section-body">
          <div className="skeleton" style={{ width: '35%', height: 22 }} />
          <div className="skeleton" style={{ width: '70%' }} />
          <div className="skeleton" />
          <div className="skeleton" style={{ width: '55%' }} />
        </div>
      </div>
    );
  }

  if (error && !incident) {
    return (
      <div className="alert error" role="alert">
        <span aria-hidden="true">!</span>
        <div className="body">
          <h4>Could not load this incident</h4>
          <p>{error.message}</p>
          <button type="button" className="button small" onClick={onChanged}>
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (!incident) return null;

  const meta = STATE_META[incident.state];

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 14 }}>
        <h1 style={{ fontSize: 18 }}>
          {incident.impact.failed_requests} failed request
          {incident.impact.failed_requests === 1 ? '' : 's'} · {incident.app_id}
        </h1>
        <StateBadge state={incident.state} />
        <span className="state-blurb" style={{ fontFamily: 'var(--mono)', fontSize: 11.5 }}>
          {incident.incident_id} · v{incident.version} · updated {formatDateTime(incident.updated_at)}
        </span>
      </div>

      {/* One live region for every in-flight state, so a screen reader hears
          progress without the page re-announcing everything. */}
      <div aria-live="polite">
        {live && (
          <div className="alert info">
            <span aria-hidden="true">↻</span>
            <div className="body">
              <h4>{meta.label}</h4>
              <p>{meta.blurb}</p>
            </div>
          </div>
        )}
      </div>

      {error && incident && (
        <div className="alert warn" role="status">
          <span aria-hidden="true">!</span>
          <div className="body">
            <p>
              Showing the last known state — the last refresh failed: {error.message}
            </p>
          </div>
        </div>
      )}

      <ImpactSection incident={incident} />
      <EvidenceSection incidentId={incident.incident_id} diagnosis={incident.diagnosis} />
      <DecisionSection incident={incident} onChanged={onChanged} />
      <ReceiptSection incident={incident} />
    </>
  );
}
