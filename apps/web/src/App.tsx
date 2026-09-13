import { useCallback, useEffect, useState } from 'react';

import { ApiError, api, hasOperatorToken, setOperatorToken } from './api';
import { DemoPanel } from './components/DemoPanel';
import { IncidentDetail } from './components/IncidentDetail';
import { IncidentList } from './components/IncidentList';
import { ModeBanner } from './components/ModeBanner';
import { Timeline } from './components/Timeline';
import { useIncidentList, useIncidentStream } from './hooks';
import type { Capabilities } from './types';

/**
 * The selected incident lives in the URL hash, so a refresh restores the same
 * work item. No router dependency for one route.
 */
function useSelectedIncident(): [string | null, (id: string) => void] {
  const read = () => {
    const match = /^#\/incidents\/([\w-]+)$/.exec(window.location.hash);
    return match ? match[1] : null;
  };
  const [selected, setSelected] = useState<string | null>(read);

  useEffect(() => {
    const onHashChange = () => setSelected(read());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const select = useCallback((id: string) => {
    window.location.hash = `#/incidents/${id}`;
  }, []);

  return [selected, select];
}

function TokenControl() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState('');
  const [set, setSet] = useState(hasOperatorToken);

  return open ? (
    <form
      className="token-form"
      onSubmit={(event) => {
        event.preventDefault();
        setOperatorToken(value);
        setSet(Boolean(value.trim()));
        setValue('');
        setOpen(false);
      }}
    >
      <label htmlFor="operator-token" className="visually-hidden">
        Operator bearer token
      </label>
      <input
        id="operator-token"
        type="password"
        autoComplete="off"
        placeholder="Operator token (live mode)"
        value={value}
        onChange={(event) => setValue(event.target.value)}
      />
      <button type="submit" className="button small primary">
        Use token
      </button>
      <button type="button" className="button small" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </form>
  ) : (
    <button
      type="button"
      className="button small"
      onClick={() => setOpen(true)}
      title="Kept in memory for this tab only — never stored, never in a URL. Not required in fixture mode."
    >
      {set ? 'Operator token set' : 'Set operator token'}
    </button>
  );
}

export default function App() {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [capabilitiesError, setCapabilitiesError] = useState<ApiError | null>(null);
  const [selectedId, select] = useSelectedIncident();
  const list = useIncidentList();
  const stream = useIncidentStream(selectedId);

  const loadCapabilities = useCallback(() => {
    void api
      .capabilities()
      .then((next) => {
        setCapabilities(next);
        setCapabilitiesError(null);
      })
      .catch((caught: ApiError) => setCapabilitiesError(caught));
  }, []);

  useEffect(loadCapabilities, [loadCapabilities]);

  const onChanged = useCallback(() => {
    stream.reload();
    list.reload();
  }, [stream, list]);

  if (capabilitiesError) {
    return (
      <div className="app">
        <div className="pane pane-center">
          <div className="alert error" role="alert">
            <span aria-hidden="true">!</span>
            <div className="body">
              <h4>Cannot reach the IncidentPilot control API</h4>
              <p>{capabilitiesError.message}</p>
              <p>
                Start it with <code>.venv/Scripts/python scripts/run_api.py</code> and the worker with{' '}
                <code>.venv/Scripts/python scripts/run_worker.py</code>.
              </p>
              <button type="button" className="button small" onClick={loadCapabilities}>
                Retry
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <a className="skip-link" href="#incident">
        Skip to the selected incident
      </a>

      <header className="masthead">
        <div className="wordmark">
          <strong>IncidentPilot</strong>
          <span>Incident workbench</span>
        </div>
        <div className="masthead-spacer" />
        <TokenControl />
      </header>

      {capabilities ? (
        <ModeBanner capabilities={capabilities} />
      ) : (
        <div className="mode-banner" role="status">
          Checking deployment mode…
        </div>
      )}

      <div className="workbench">
        <nav className="pane pane-left" aria-label="Incidents">
          {capabilities && <DemoPanel capabilities={capabilities} onSubmitted={list.reload} />}
          <div className="pane-header">
            <h2>Incidents</h2>
            <button type="button" className="button link" onClick={list.reload}>
              Refresh
            </button>
          </div>
          {list.error && (
            <div className="alert error" role="alert">
              <span aria-hidden="true">!</span>
              <div className="body">
                <p>{list.error.message}</p>
              </div>
            </div>
          )}
          <IncidentList
            items={list.items}
            loading={list.loading}
            selectedId={selectedId}
            onSelect={select}
          />
        </nav>

        <main className="pane pane-center" id="incident">
          {selectedId ? (
            <IncidentDetail stream={stream} onChanged={onChanged} />
          ) : (
            <div className="empty">
              <h3>{list.items.length > 0 ? 'Select an incident' : 'No incidents yet'}</h3>
              <p>
                {list.items.length > 0
                  ? 'Pick an incident from the list to see its impact, evidence, proposed action and recovery receipt.'
                  : 'Create a demo run, inject the fault from your terminal, then submit three requests. An incident opens as soon as the first request fails.'}
              </p>
            </div>
          )}
        </main>

        <aside className="pane pane-right" aria-label="Activity timeline">
          <div className="pane-header">
            <h2>Activity</h2>
          </div>
          {selectedId ? (
            <Timeline events={stream.events} live={stream.live} />
          ) : (
            <p className="state-blurb">Select an incident to see its activity timeline.</p>
          )}
        </aside>
      </div>
    </div>
  );
}
