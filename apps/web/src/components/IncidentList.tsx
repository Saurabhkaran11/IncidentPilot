import { formatTime } from '../format';
import type { IncidentListItem } from '../types';
import { StateBadge } from './Badge';

interface Props {
  items: IncidentListItem[];
  loading: boolean;
  selectedId: string | null;
  onSelect: (incidentId: string) => void;
}

export function IncidentList({ items, loading, selectedId, onSelect }: Props) {
  if (loading && items.length === 0) {
    return (
      <div aria-busy="true" aria-label="Loading incidents">
        <div className="skeleton" style={{ height: 62 }} />
        <div className="skeleton" style={{ height: 62 }} />
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <p className="state-blurb">
        No incidents yet. Start a demo run below, inject the fault, then submit requests.
      </p>
    );
  }

  return (
    <ul className="incident-list">
      {items.map((item) => (
        <li className="incident-item" key={item.incident_id}>
          <button
            type="button"
            aria-current={item.incident_id === selectedId}
            onClick={() => onSelect(item.incident_id)}
          >
            <div className="row">
              <StateBadge state={item.state} />
              <span className="meta">{formatTime(item.updated_at)}</span>
            </div>
            <div className="headline">
              {item.failed_requests} request{item.failed_requests === 1 ? '' : 's'} failed
            </div>
            <div className="meta">
              {item.app_id} · {item.incident_id}
            </div>
          </button>
        </li>
      ))}
    </ul>
  );
}
