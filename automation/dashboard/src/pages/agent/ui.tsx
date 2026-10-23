// Shared look for the AI Agent page: the same chip / ghost button / input / section
// helpers the page has always used, plus the session labels. No components here, so
// fast refresh keeps working for the files that import it.
import type { CSSProperties, ReactNode } from 'react';
import { parseServerDate } from '../../time';

/** A coloured pill. Never wider than its box: a long label is cut with an ellipsis
 *  (full text on hover), or wraps when `wrap` is set. */
export const chip = (text: ReactNode, color: string, wrap = false): ReactNode => (
  <span title={typeof text === 'string' ? text : undefined}
        style={{ padding: '3px 10px', borderRadius: wrap ? 12 : 99, fontSize: '0.72rem', fontWeight: 600,
                 color, background: `${color}1f`, border: `1px solid ${color}40`,
                 display: 'inline-block', maxWidth: '100%', verticalAlign: 'middle', boxSizing: 'border-box',
                 ...(wrap ? { whiteSpace: 'normal', overflowWrap: 'anywhere' }
                   : { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }) }}>
    {text}
  </span>
);

/** For any text that may be long (errors, phases, paths, branches): wrap inside its box. */
export const wrapText: CSSProperties = { overflowWrap: 'anywhere', wordBreak: 'break-word', minWidth: 0 };

export const ghost: CSSProperties = {
  background: 'transparent', border: '1px solid var(--border-highlight)', color: 'var(--text-primary)',
  padding: '8px 14px', borderRadius: 'var(--radius-md)', cursor: 'pointer', display: 'inline-flex',
  alignItems: 'center', gap: 6, font: 'inherit', fontSize: '0.85rem',
};
export const input: CSSProperties = {
  background: 'var(--bg-tertiary)', border: '1px solid var(--border-highlight)', color: 'var(--text-primary)',
  padding: '8px 10px', borderRadius: 'var(--radius-sm)', font: 'inherit', fontSize: '0.85rem',
};
export const section: CSSProperties = { marginTop: 24 };
export const h3: CSSProperties = { fontSize: '1.05rem', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 };
export const muted: CSSProperties = { color: 'var(--text-secondary)', fontSize: '0.85rem' };
export const mono: CSSProperties = {
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: '0.78rem',
};
export const pre: CSSProperties = {
  ...mono, background: 'var(--bg-primary)', padding: 12, borderRadius: 8, overflow: 'auto',
  whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflowWrap: 'anywhere', margin: 0,
  maxWidth: '100%', boxSizing: 'border-box',
};
export const th: CSSProperties = {
  padding: '8px 10px', textAlign: 'left', color: 'var(--text-secondary)', fontSize: '0.72rem',
  textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600,
};
export const td: CSSProperties = {
  padding: '8px 10px', borderTop: '1px solid var(--border-color)', verticalAlign: 'top', overflowWrap: 'anywhere',
};

/** Restarting the backend to load an approved change. */
export type RestartState = { phase: 'idle' | 'restarting' | 'done'; error?: string };

export const GREEN = '#34d399';
export const AMBER = '#fbbf24';
export const RED = '#f87171';
export const BLUE = '#60a5fa';
export const GREY = '#94a3b8';

export const when = (iso?: string | null) => (iso ? parseServerDate(iso).toLocaleString() : '—');
export const ms = (iso?: string | null) => (iso ? parseServerDate(iso).getTime() : NaN);

/** 42s · 12m 3s · 1h 4m */
export function fmtDur(millis: number): string {
  if (!Number.isFinite(millis) || millis < 0) return '—';
  const s = Math.round(millis / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export const KIND: Record<string, string> = {
  heal: 'Run & heal', fix_run: 'Fix a run', analyze: 'App analysis', generate: 'New scenarios', nightly: 'Nightly',
};
export const TRIGGER: Record<string, string> = {
  manual: 'Manual', run_all: 'After Run all', nightly: 'Nightly', app_change: 'App change',
};

export const STATUS: Record<string, { label: string; color: string }> = {
  queued: { label: 'Queued', color: BLUE },
  preparing: { label: 'Preparing', color: BLUE },
  running: { label: 'Running', color: BLUE },
  checking: { label: 'Checking', color: BLUE },
  review: { label: 'Waiting for approval', color: AMBER },
  approved: { label: 'Approved', color: GREEN },
  no_changes: { label: 'No changes', color: GREY },
  gates_failed: { label: 'Check failed', color: RED },
  failed: { label: 'Failed', color: RED },
  rejected: { label: 'Rejected', color: GREY },
  stopped: { label: 'Stopped', color: GREY },
  interrupted: { label: 'Interrupted', color: GREY },
};
export const statusChip = (s: string) => {
  const v = STATUS[s] ?? { label: s, color: GREY };
  return chip(v.label, v.color);
};

/** Colour for a flow-run status (passed / failed / …). */
export const runColor = (s?: string | null) => {
  const v = (s || '').toLowerCase();
  if (v === 'passed' || v === 'fixed') return GREEN;
  if (v === 'flaky' || v === 'passed_on_retry') return AMBER;
  if (v === 'failed' || v === 'error') return RED;
  if (v === 'running' || v === 'queued') return BLUE;
  return GREY;
};
