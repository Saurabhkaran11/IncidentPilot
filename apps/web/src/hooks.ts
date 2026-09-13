/**
 * Polling primitives.
 *
 * The API contract asks for: poll every two seconds while the incident is
 * active, back off on failure, and stop on a terminal state or a hidden tab.
 * `usePoll` is the one place all three rules live.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, api } from './api';
import type { Incident, IncidentEvent, IncidentListItem } from './types';
import { ACTIVE_STATES } from './types';

const BASE_INTERVAL_MS = 2000;
const MAX_INTERVAL_MS = 30_000;

function useLatest<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

/**
 * Runs `task` immediately, then on an interval while `enabled` and the tab is
 * visible. A rejected task doubles the delay until it succeeds again.
 */
function usePoll(task: () => Promise<void>, enabled: boolean, intervalMs = BASE_INTERVAL_MS): void {
  const taskRef = useLatest(task);
  const enabledRef = useLatest(enabled);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let delay = intervalMs;

    const schedule = () => {
      if (cancelled || !enabledRef.current || document.visibilityState !== 'visible') return;
      timer = window.setTimeout(run, delay);
    };

    const run = async () => {
      if (cancelled || document.visibilityState !== 'visible') return;
      try {
        await taskRef.current();
        delay = intervalMs;
      } catch {
        delay = Math.min(delay * 2, MAX_INTERVAL_MS);
      }
      schedule();
    };

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        window.clearTimeout(timer);
        void run();
      } else {
        window.clearTimeout(timer);
      }
    };

    void run();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // `enabled` is read through a ref inside the loop, but a change to it must
    // restart (or stop) the loop, so it stays in the dependency list.
  }, [enabled, intervalMs, enabledRef, taskRef]);
}

export interface IncidentListState {
  items: IncidentListItem[];
  loading: boolean;
  error: ApiError | null;
  reload: () => void;
}

export function useIncidentList(): IncidentListState {
  const [items, setItems] = useState<IncidentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await api.listIncidents();
      setItems(response.items);
      setError(null);
    } catch (caught) {
      setError(caught as ApiError);
      throw caught;
    } finally {
      setLoading(false);
    }
  }, []);

  usePoll(load, true, 5000);
  const reload = useCallback(() => void load().catch(() => undefined), [load]);
  return { items, loading, error, reload };
}

export interface IncidentStreamState {
  incident: Incident | null;
  events: IncidentEvent[];
  loading: boolean;
  error: ApiError | null;
  /** True while the server still has work in flight for this incident. */
  live: boolean;
  reload: () => void;
}

export function useIncidentStream(incidentId: string | null): IncidentStreamState {
  const [incident, setIncident] = useState<Incident | null>(null);
  const [events, setEvents] = useState<IncidentEvent[]>([]);
  const [loading, setLoading] = useState(Boolean(incidentId));
  const [error, setError] = useState<ApiError | null>(null);
  const lastSequence = useRef(0);

  useEffect(() => {
    setIncident(null);
    setEvents([]);
    setError(null);
    setLoading(Boolean(incidentId));
    lastSequence.current = 0;
  }, [incidentId]);

  const load = useCallback(async () => {
    if (!incidentId) return;
    try {
      const next = await api.getIncident(incidentId);
      setIncident(next);
      const page = await api.listEvents(incidentId, lastSequence.current);
      if (page.items.length) {
        lastSequence.current = Math.max(lastSequence.current, page.last_sequence);
        // Two overlapping fetches (a manual reload racing the poll, or React's
        // double-invoked effects in development) can both start from the same
        // cursor, so the merge dedupes rather than trusting the cursor alone.
        setEvents((previous) => {
          const seen = new Set(previous.map((event) => event.event_id));
          const additions = page.items.filter((event) => !seen.has(event.event_id));
          return additions.length ? [...previous, ...additions] : previous;
        });
      }
      setError(null);
    } catch (caught) {
      setError(caught as ApiError);
      throw caught;
    } finally {
      setLoading(false);
    }
  }, [incidentId]);

  const live = incident !== null && ACTIVE_STATES.has(incident.state);
  // Keep polling until we have loaded the incident at least once, then only
  // while it is doing something. A terminal incident stops the timer entirely.
  usePoll(load, Boolean(incidentId) && (incident === null || live));

  const reload = useCallback(() => void load().catch(() => undefined), [load]);
  return { incident, events, loading, error, live, reload };
}

/** Ticking clock for the plan-expiry countdown. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}
