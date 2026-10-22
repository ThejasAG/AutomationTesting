import { useState } from 'react';
import type { ReactNode } from 'react';
import { ChevronDown, ChevronRight, HeartPulse, Loader2, Microscope, Sparkles, Wrench } from 'lucide-react';
import { agentApi } from '../../agentApi';
import type { AgentFlow, StartSession } from '../../agentApi';
import { ghost, h3, input, muted, section, wrapText } from './ui';

/** The four ways to start a Claude Code session. Only one session runs at a time. */
export default function StartPanel({ disabled, busy, defaultMaxNew, onStart }: {
  disabled: boolean; busy: string; defaultMaxNew: number;
  onStart: (label: string, body: StartSession) => void;
}) {
  const [flows, setFlows] = useState<AgentFlow[] | null>(null);
  const [flowErr, setFlowErr] = useState('');
  const [showFlows, setShowFlows] = useState(false);
  const [chosen, setChosen] = useState<string[]>([]);
  const [runId, setRunId] = useState('');
  const [mode, setMode] = useState<'delta' | 'full'>('delta');
  const [focus, setFocus] = useState('');
  const [maxNew, setMaxNew] = useState<number | ''>('');

  const off = disabled || !!busy;
  const openFlows = () => {
    setShowFlows(v => !v);
    if (!flows) agentApi.flows().then(r => setFlows(r.flows)).catch(e => setFlowErr((e as Error).message));
  };
  // Accept a pasted run page URL as well as a bare id.
  const cleanRun = runId.trim().replace(/[?#].*$/, '').split('/').filter(Boolean).pop() ?? '';
  const spin = (label: string, icon: ReactNode) => (busy === label ? <Loader2 size={14} className="spin" /> : icon);
  const btn = { padding: '8px 14px' } as const;

  return (
    <div style={section}>
      <h3 style={h3}><Sparkles size={18} /> Start a session</h3>
      {disabled && (
        <p style={{ ...muted, marginBottom: 12 }}>A session is running. Start another when it finishes, or stop it.</p>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(280px, 100%), 1fr))', gap: 16 }}>
        <Card title="Run & heal" icon={<HeartPulse size={16} />}
              text="Runs every flow (or the ones you pick) and fixes what the automation got wrong. App bugs are reported, not fixed.">
          <button style={{ ...ghost, padding: '5px 10px', fontSize: '0.8rem', marginBottom: 10 }} onClick={openFlows}>
            {showFlows ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            {chosen.length ? `${chosen.length} flow${chosen.length > 1 ? 's' : ''} chosen` : 'Choose flows (optional)'}
          </button>
          {showFlows && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10, maxHeight: 200, overflowY: 'auto' }}>
              {flowErr && <span style={{ color: '#f87171', fontSize: '0.8rem', ...wrapText }}>{flowErr}</span>}
              {!flows && !flowErr && <span style={muted}><Loader2 size={13} className="spin" /> Loading flows…</span>}
              {flows?.map(f => (
                <label key={f.id} title={f.id}
                       style={{ display: 'inline-flex', gap: 6, alignItems: 'center', fontSize: '0.8rem', padding: '3px 10px',
                                borderRadius: 99, border: '1px solid var(--border-highlight)', cursor: 'pointer',
                                maxWidth: '100%', boxSizing: 'border-box', ...wrapText }}>
                  <input type="checkbox" checked={chosen.includes(f.id)}
                         onChange={e => setChosen(c => (e.target.checked ? [...c, f.id] : c.filter(x => x !== f.id)))} />
                  {f.name}
                  {f.demo && <span style={{ color: 'var(--text-muted)' }}>demo</span>}
                  {f.draft && <span style={{ color: '#fbbf24' }}>draft</span>}
                </label>
              ))}
              {chosen.length > 0 && (
                <button style={{ ...ghost, padding: '3px 10px', fontSize: '0.78rem' }} onClick={() => setChosen([])}>Clear</button>
              )}
            </div>
          )}
          <button className="btn" style={btn} disabled={off}
                  onClick={() => onStart('heal', { kind: 'heal', ...(chosen.length ? { flow_ids: chosen } : {}) })}>
            {spin('heal', <HeartPulse size={14} />)} {chosen.length ? `Run ${chosen.length} & heal` : 'Run all & heal'}
          </button>
        </Card>

        <Card title="Fix a failed run" icon={<Wrench size={16} />}
              text="Paste a failed run id. Claude Code finds why it failed and fixes the test if the test was wrong.">
          <div style={{ display: 'flex', gap: 8 }}>
            <input style={{ ...input, flex: 1, minWidth: 0 }} placeholder="run id" value={runId}
                   onChange={e => setRunId(e.target.value)} />
            <button className="btn" style={btn} disabled={off || !cleanRun}
                    onClick={() => onStart('fix_run', { kind: 'fix_run', run_ids: [cleanRun] })}>
              {spin('fix_run', <Wrench size={14} />)} Fix
            </button>
          </div>
        </Card>

        <Card title="Analyze apps" icon={<Microscope size={16} />}
              text="Reads both apps' code and maps which features the tests cover. Changes nothing.">
          <div style={{ display: 'flex', gap: 8 }}>
            <select style={{ ...input, flex: 1, minWidth: 0 }} value={mode} onChange={e => setMode(e.target.value as 'delta' | 'full')}>
              <option value="delta">What changed since the last analysis</option>
              <option value="full">Everything (slower)</option>
            </select>
            <button className="btn" style={btn} disabled={off} onClick={() => onStart('analyze', { kind: 'analyze', mode })}>
              {spin('analyze', <Microscope size={14} />)} Analyze
            </button>
          </div>
        </Card>

        <Card title="Create scenarios" icon={<Sparkles size={16} />}
              text="Writes new scenarios for untested features and keeps only the ones that pass. Or tick gaps in the Feature map below.">
          <textarea style={{ ...input, width: '100%', minHeight: 54, resize: 'vertical', marginBottom: 8, boxSizing: 'border-box' }}
                    placeholder="Focus (optional), e.g. refunds, split bill" value={focus} onChange={e => setFocus(e.target.value)} />
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span style={muted}>At most</span>
            <input type="number" min={1} max={20} style={{ ...input, width: 70 }} placeholder={String(defaultMaxNew)}
                   value={maxNew} onChange={e => setMaxNew(e.target.value === '' ? '' : Math.max(1, Number(e.target.value)))} />
            <span style={{ ...muted, flex: 1 }}>new</span>
            <button className="btn" style={btn} disabled={off}
                    onClick={() => onStart('generate', {
                      kind: 'generate', ...(focus.trim() ? { focus: focus.trim() } : {}), ...(maxNew ? { max_new: maxNew } : {}),
                    })}>
              {spin('generate', <Sparkles size={14} />)} Create
            </button>
          </div>
        </Card>
      </div>
    </div>
  );
}

function Card({ title, icon, text, children }: { title: string; icon: ReactNode; text: string; children: ReactNode }) {
  return (
    <div className="card" style={{ padding: 20, transform: 'none', display: 'flex', flexDirection: 'column', minWidth: 0, ...wrapText }}>
      <div style={{ fontWeight: 600, marginBottom: 6, display: 'flex', gap: 8, alignItems: 'center' }}>{icon}{title}</div>
      <p style={{ ...muted, marginBottom: 12, flex: 1 }}>{text}</p>
      <div>{children}</div>
    </div>
  );
}
