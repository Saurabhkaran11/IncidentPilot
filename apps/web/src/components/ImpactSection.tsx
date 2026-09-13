import { STATE_META, formatDateTime } from '../format';
import type { Incident } from '../types';
import { StateBadge } from './Badge';

/**
 * Section 1. Every number here comes from stored request records via
 * `incident.impact` -- nothing is derived from the diagnosis text.
 */
export function ImpactSection({ incident }: { incident: Incident }) {
  const { impact } = incident;
  const meta = STATE_META[incident.state];

  return (
    <section className="section" aria-labelledby="impact-heading">
      <header>
        <h3 id="impact-heading">Impact</h3>
        <span className="step">Section 1 of 4</span>
      </header>
      <div className="section-body">
        <p className="impact-headline">
          <span className="count">{impact.failed_requests}</span> document request
          {impact.failed_requests === 1 ? '' : 's'} failed
          {impact.completed_requests > 0 && (
            <>
              {' '}
              · <span className="count">{impact.completed_requests}</span> completed
            </>
          )}
        </p>
        <p className="state-blurb">{meta.blurb}</p>
      </div>
      <dl className="impact-grid">
        <div className="impact-cell">
          <dt>First failure</dt>
          <dd>{formatDateTime(impact.first_failure_at)}</dd>
        </div>
        <div className="impact-cell">
          <dt>Service</dt>
          <dd>{impact.service}</dd>
        </div>
        <div className="impact-cell">
          <dt>Mode</dt>
          <dd>{incident.mode === 'fixture' ? 'Fixture (simulated)' : 'Live AWS'}</dd>
        </div>
        <div className="impact-cell">
          <dt>Recovery status</dt>
          <dd>
            <StateBadge state={incident.state} />
          </dd>
        </div>
      </dl>
    </section>
  );
}
