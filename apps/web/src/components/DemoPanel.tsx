import { useState } from 'react';

import { ApiError, api, newIdempotencyKey } from '../api';
import type { Capabilities } from '../types';

const FIXTURES = ['invoice-example-a', 'invoice-example-b', 'invoice-example-c'];

/**
 * Drives the demo through the same public API the rest of the UI uses.
 *
 * Step 2 is deliberately a terminal command, not a button: injecting the fault
 * is an owner-only CLI action and has no HTTP route. A public visitor must not
 * be able to break the demo application.
 */
export function DemoPanel({ capabilities, onSubmitted }: { capabilities: Capabilities; onSubmitted: () => void }) {
  const appId = capabilities.applications[0]?.app_id ?? 'document-demo';
  const [runId, setRunId] = useState<string | null>(null);
  const [busy, setBusy] = useState<'run' | 'requests' | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [submitted, setSubmitted] = useState(0);
  const [keys] = useState<Record<string, string>>({});

  const keyFor = (scope: string) => (keys[scope] ??= newIdempotencyKey());

  const createRun = async () => {
    setBusy('run');
    setError(null);
    try {
      const run = await api.createDemoRun(appId, keyFor('run'));
      setRunId(run.demo_run_id);
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(null);
    }
  };

  const submitRequests = async () => {
    if (!runId) return;
    setBusy('requests');
    setError(null);
    try {
      for (const fixture of FIXTURES) {
        await api.submitDemoRequest(runId, fixture, keyFor(`${runId}:${fixture}`));
        setSubmitted((count) => count + 1);
      }
      onSubmitted();
    } catch (caught) {
      setError(caught as ApiError);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="demo-panel" aria-labelledby="demo-heading">
      <h2 id="demo-heading">Demo run</h2>

      <div className="demo-step">
        <span className={`n${runId ? ' done' : ''}`} aria-hidden="true">
          1
        </span>
        <div className="body">
          <p>Create a run for {appId}.</p>
          {runId ? (
            <code className="chip">{runId}</code>
          ) : (
            <button type="button" className="button small primary" onClick={() => void createRun()} disabled={busy !== null}>
              {busy === 'run' ? 'Creating…' : 'Create demo run'}
            </button>
          )}
        </div>
      </div>

      <div className="demo-step">
        <span className="n" aria-hidden="true">
          2
        </span>
        <div className="body">
          <p>Inject the fault from your terminal — there is no HTTP route for this.</p>
          <code className="command">{`.venv/Scripts/python -c "from services.api.config import get_gateway,get_store; s,g=get_store(),get_gateway(); c=s.get_app_config('${appId}'); g.force_set_alias(c.function_name,c.alias_name,'7')"`}</code>
        </div>
      </div>

      <div className="demo-step">
        <span className={`n${submitted >= FIXTURES.length ? ' done' : ''}`} aria-hidden="true">
          3
        </span>
        <div className="body">
          <p>Submit {FIXTURES.length} document requests against the broken alias.</p>
          <button
            type="button"
            className="button small primary"
            onClick={() => void submitRequests()}
            disabled={!runId || busy !== null}
          >
            {busy === 'requests' ? `Submitting ${submitted}/${FIXTURES.length}…` : `Submit ${FIXTURES.length} requests`}
          </button>
        </div>
      </div>

      {error && (
        <div className="alert error" role="alert" style={{ marginTop: 10, marginBottom: 0 }}>
          <span aria-hidden="true">!</span>
          <div className="body">
            <p>
              <code>{error.code}</code> {error.message}
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
