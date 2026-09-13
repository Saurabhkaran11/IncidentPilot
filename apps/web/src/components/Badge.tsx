import type { ReactNode } from 'react';

import { STATE_META } from '../format';
import type { IncidentState } from '../types';

export type Tone = 'neutral' | 'busy' | 'pending' | 'good' | 'warn' | 'bad';

export function Badge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`badge tone-${tone}`}>
      <span className="dot" aria-hidden="true" />
      {children}
    </span>
  );
}

/** State is always spelled out; the colour only reinforces the word. */
export function StateBadge({ state }: { state: IncidentState }) {
  const meta = STATE_META[state];
  return <Badge tone={meta.tone}>{meta.label}</Badge>;
}
