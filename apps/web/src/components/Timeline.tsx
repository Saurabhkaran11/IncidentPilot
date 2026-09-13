import { eventLabel, formatTime } from '../format';
import type { IncidentEvent } from '../types';

/** Event types whose node gets a colour, so the eye finds the turning points. */
function toneFor(type: string): string {
  if (type === 'receipt.created' || type === 'verification.completed') return 'good';
  if (type === 'incident.needs_attention' || type === 'plan.invalidated') return 'bad';
  if (type === 'decision.recorded') return 'decision';
  return '';
}

/**
 * Sanitized event payloads only -- these are the server's own event records,
 * never model reasoning. Values are rendered as escaped text.
 */
function summarize(data: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(data)) {
    if (value === null || value === undefined) continue;
    if (key === 'operation_id') continue;
    const rendered = Array.isArray(value) ? `${value.length}` : String(value);
    if (rendered.length > 90) continue;
    parts.push(`${key}=${rendered}`);
  }
  return parts.join('  ');
}

export function Timeline({ events, live }: { events: IncidentEvent[]; live: boolean }) {
  if (events.length === 0) {
    return <p className="state-blurb">No activity recorded yet.</p>;
  }

  return (
    <>
      <ol className="timeline">
        {events.map((event) => (
          <li key={event.event_id} className={toneFor(event.type)}>
            <span className="node" aria-hidden="true" />
            <div className="when">
              {formatTime(event.occurred_at)} · #{event.sequence}
            </div>
            <div className="what">{eventLabel(event.type)}</div>
            <p className="payload">{summarize(event.data)}</p>
          </li>
        ))}
      </ol>
      {live && (
        <p className="state-blurb" role="status">
          Polling for new activity every 2 seconds…
        </p>
      )}
    </>
  );
}
