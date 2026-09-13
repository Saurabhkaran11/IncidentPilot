import type { Capabilities } from '../types';

/**
 * Says what this deployment actually is. Two honesty requirements live here:
 * fixture data must never look like a live AWS run, and a stub investigator
 * must never be described as an LLM.
 */
export function ModeBanner({ capabilities }: { capabilities: Capabilities }) {
  const live = capabilities.mode === 'aws_live';
  const stubAgent = capabilities.agent_mode !== 'bedrock';

  return (
    <div className={`mode-banner${live ? ' live' : ''}`} role="status">
      <span aria-hidden="true">{live ? '☁' : '⚗'}</span>
      <div>
        {live ? (
          <>
            <b>Live AWS mode.</b> Actions taken here change a real Lambda alias in{' '}
            account {capabilities.applications[0]?.app_id ?? 'the registered application'}.
          </>
        ) : (
          <>
            <b>Fixture mode — simulated data.</b> Nothing here touches AWS. The failure, the alias, the
            canary and the replayed requests are all local fixtures used for reproducible development
            and walkthroughs.
          </>
        )}{' '}
        <span className="detail">
          {stubAgent ? (
            <>
              Investigation ran with the deterministic <code>{capabilities.agent_mode}</code> analyser — no
              language model was called.
            </>
          ) : (
            <>Investigation ran on the Strands/Bedrock agent ({capabilities.agent_mode}).</>
          )}
        </span>
      </div>
    </div>
  );
}
