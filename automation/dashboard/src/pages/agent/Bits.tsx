import { useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { mono, pre, wrapText } from './ui';

export function Banner({ color, icon, children }: { color: string; icon: ReactNode; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '10px 14px', marginBottom: 16, maxWidth: '100%',
                  boxSizing: 'border-box', borderRadius: 'var(--radius-md)', color, background: `${color}14`,
                  border: `1px solid ${color}33` }}>
      <span style={{ flexShrink: 0, display: 'inline-flex' }}>{icon}</span>
      <span style={{ flex: 1, ...wrapText, whiteSpace: 'pre-wrap' }}>{children}</span>
    </div>
  );
}

/** A run id as a link to its run page (route /run/:id). */
export function RunLink({ id }: { id?: string | null }) {
  if (!id) return <span style={{ color: 'var(--text-muted)' }}>—</span>;
  return (
    <Link to={`/run/${id}`} title={id} style={{ ...mono, color: 'var(--accent-primary)' }}>
      {id.slice(0, 8)}
    </Link>
  );
}

/** Long text in a <pre>, showing the first `lines` lines until opened. */
export function ClampedPre({ text, lines = 3 }: { text: string; lines?: number }) {
  const [open, setOpen] = useState(false);
  const all = text.replace(/\s+$/, '').split('\n');
  if (!text.trim()) return null;
  const long = all.length > lines;
  return (
    <div style={{ marginTop: 6 }}>
      <pre style={{ ...pre, padding: '8px 10px' }}>{open || !long ? all.join('\n') : all.slice(0, lines).join('\n')}</pre>
      {long && (
        <button onClick={() => setOpen(o => !o)}
                style={{ background: 'none', border: 'none', color: 'var(--accent-primary)', cursor: 'pointer',
                         font: 'inherit', fontSize: '0.78rem', padding: '4px 0' }}>
          {open ? 'Show less' : `Show all ${all.length} lines`}
        </button>
      )}
    </div>
  );
}

/** A unified diff with added lines green and removed lines red. */
export function DiffView({ diff, maxHeight = 520 }: { diff: string; maxHeight?: number }) {
  if (!diff.trim()) return <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>No change recorded.</p>;
  return (
    <div style={{ ...mono, background: 'var(--bg-primary)', borderRadius: 8, overflow: 'auto', maxHeight, maxWidth: '100%',
                  minWidth: 0, boxSizing: 'border-box', border: '1px solid var(--border-color)', padding: '8px 0', lineHeight: 1.5 }}>
      {/* Lines keep their exact spacing and scroll sideways inside this box. */}
      <div style={{ width: 'max-content', minWidth: '100%' }}>
      {diff.split('\n').map((l, i) => {
        const file = l.startsWith('diff --git') || l.startsWith('+++') || l.startsWith('---');
        const add = !file && l.startsWith('+');
        const del = !file && l.startsWith('-');
        const hunk = l.startsWith('@@');
        return (
          <div key={i} style={{
            padding: '0 12px', whiteSpace: 'pre', minHeight: '1.5em',
            color: add ? '#34d399' : del ? '#f87171' : hunk ? '#818cf8' : file ? 'var(--text-primary)' : 'var(--text-secondary)',
            background: add ? 'rgba(52,211,153,0.08)' : del ? 'rgba(248,113,113,0.08)' : 'transparent',
            fontWeight: file ? 700 : 400,
            borderTop: l.startsWith('diff --git') && i ? '1px solid var(--border-color)' : 'none',
            marginTop: l.startsWith('diff --git') && i ? 6 : 0, paddingTop: l.startsWith('diff --git') && i ? 6 : 0,
          }}>{l}</div>
        );
      })}
      </div>
    </div>
  );
}
