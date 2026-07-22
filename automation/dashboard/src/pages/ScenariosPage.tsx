import { useEffect, useRef, useState } from 'react';
import {
  Plus, Play, Pencil, Trash2, X, GripVertical, ArrowUp, ArrowDown, Loader2,
  Smartphone, CheckCircle2, AlertTriangle, ListChecks, PlayCircle,
} from 'lucide-react';
import {
  getScenarios, createScenario, updateScenario, deleteScenario, getScenarioDevices,
  getProjects, runScenarioStream, runScenariosBatch,
} from '../api';
import type { SavedScenario, ScenarioInput, SimDevice, Project, ScenarioEvent } from '../api';
import ModalPortal from '../components/ModalPortal';
import RecorderModal from '../components/RecorderModal';
import { Circle } from 'lucide-react';

const inputStyle: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', background: 'rgba(255,255,255,0.05)',
  border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)',
  color: 'var(--text-primary)', padding: '9px 11px', fontSize: '0.88rem', fontFamily: 'inherit',
};
const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '0.68rem', color: 'var(--text-muted)', textTransform: 'uppercase',
  letterSpacing: '0.06em', marginBottom: 5, fontWeight: 600,
};

const empty = (): ScenarioInput => ({ name: '', description: '', project_id: '', device_id: '', steps: [] });

export default function ScenariosPage() {
  const [scenarios, setScenarios] = useState<SavedScenario[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [sims, setSims] = useState<SimDevice[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<{ id: string | null; data: ScenarioInput } | null>(null);
  const [running, setRunning] = useState<SavedScenario | null>(null);
  const [runAll, setRunAll] = useState(false);
  const [recording, setRecording] = useState(false);

  // Scenarios that are fully configured and therefore runnable.
  const runnable = scenarios.filter(s => s.project_id && s.device_id && s.steps.length > 0);

  const refresh = async () => setScenarios(await getScenarios());

  useEffect(() => {
    (async () => {
      try {
        const [s, p, d] = await Promise.all([getScenarios(), getProjects(), getScenarioDevices()]);
        setScenarios(s); setProjects(p); setSims(d.simulators);
      } catch { /* surfaced by empty state */ }
      setLoading(false);
    })();
  }, []);

  const onDelete = async (s: SavedScenario) => {
    if (!confirm(`Delete scenario "${s.name}"?`)) return;
    await deleteScenario(s.id);
    await refresh();
  };

  const simName = (udid: string | null) => sims.find(x => x.udid === udid)?.name || (udid ? udid.slice(0, 8) : '—');
  const projName = (id: string | null) => projects.find(p => p.id === id)?.name || '—';

  return (
    <div className="animate-fade-in">
      <header className="page-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title">Scenarios</h1>
          <p className="page-subtitle">Build reusable step-by-step scenarios, pick a device, and run them on demand.</p>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          {runnable.length > 1 && (
            <button className="btn" onClick={() => setRunAll(true)}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '10px 18px', background: 'var(--success-bg, rgba(52,211,153,0.15))', color: 'var(--success, #34d399)', border: '1px solid rgba(52,211,153,0.3)' }}>
              <PlayCircle size={16} /> Run All ({runnable.length})
            </button>
          )}
          <button className="btn" onClick={() => setRecording(true)}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '10px 18px', background: 'rgba(248,113,113,0.14)', color: '#f87171', border: '1px solid rgba(248,113,113,0.3)' }}>
            <Circle size={14} fill="currentColor" /> Record
          </button>
          <button className="btn" onClick={() => setEditing({ id: null, data: empty() })}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '10px 18px' }}>
            <Plus size={16} /> New Scenario
          </button>
        </div>
      </header>

      {loading ? (
        <div style={{ padding: 40, color: 'var(--text-secondary)' }}><Loader2 size={18} className="spin" /> Loading…</div>
      ) : scenarios.length === 0 ? (
        <div className="card" style={{ textAlign: 'center', padding: 48, color: 'var(--text-muted)' }}>
          <ListChecks size={30} style={{ opacity: 0.5, marginBottom: 12 }} />
          <div style={{ fontSize: '1rem', marginBottom: 6, color: 'var(--text-secondary)' }}>No scenarios yet</div>
          <div style={{ fontSize: '0.85rem' }}>Click <strong>New Scenario</strong> to build your first step-by-step flow.</div>
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 16 }}>
          {scenarios.map(s => (
            <div key={s.id} className="card" style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
                <div>
                  <div style={{ fontWeight: 600, fontSize: '1rem' }}>{s.name}</div>
                  {s.description && <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)', marginTop: 3 }}>{s.description}</div>}
                </div>
                <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)', whiteSpace: 'nowrap', background: 'rgba(255,255,255,0.05)', padding: '3px 8px', borderRadius: 99 }}>
                  {s.steps.length} step{s.steps.length === 1 ? '' : 's'}
                </span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                <div><span style={{ color: 'var(--text-muted)' }}>App:</span> {projName(s.project_id)}</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}><Smartphone size={12} /> {simName(s.device_id)}</div>
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
                <button className="btn" onClick={() => setRunning(s)} disabled={!s.project_id || !s.device_id || s.steps.length === 0}
                  title={!s.project_id || !s.device_id || s.steps.length === 0 ? 'Set an app, device and at least one step first' : 'Run this scenario'}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 14px', fontSize: '0.82rem', flex: 1, justifyContent: 'center' }}>
                  <Play size={13} /> Run
                </button>
                <button onClick={() => setEditing({ id: s.id, data: { name: s.name, description: s.description || '', project_id: s.project_id || '', bundle_id: s.bundle_id || '', device_id: s.device_id || '', steps: s.steps } })}
                  title="Edit" style={iconBtn}><Pencil size={14} /></button>
                <button onClick={() => onDelete(s)} title="Delete" style={{ ...iconBtn, color: 'var(--danger)' }}><Trash2 size={14} /></button>
              </div>
            </div>
          ))}
        </div>
      )}

      {editing && (
        <ScenarioEditor
          initial={editing.data} isNew={editing.id === null} projects={projects} sims={sims}
          onClose={() => setEditing(null)}
          onSave={async (data) => {
            if (editing.id) await updateScenario(editing.id, data);
            else await createScenario(data);
            setEditing(null); await refresh();
          }}
        />
      )}

      {running && <RunModal scenario={running} projects={projects} onClose={() => setRunning(null)} />}
      {runAll && <RunAllModal scenarios={runnable} onClose={() => setRunAll(false)} />}
      {recording && (
        <RecorderModal
          projects={projects} sims={sims}
          onClose={() => setRecording(false)}
          onSaved={() => { setRecording(false); refresh(); }}
        />
      )}
    </div>
  );
}

const iconBtn: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'center', width: 34, height: 34,
  background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)',
  color: 'var(--text-secondary)', cursor: 'pointer',
};

// ── Editor ───────────────────────────────────────────────────────────────────
function ScenarioEditor({ initial, isNew, projects, sims, onClose, onSave }: {
  initial: ScenarioInput; isNew: boolean; projects: Project[]; sims: SimDevice[];
  onClose: () => void; onSave: (d: ScenarioInput) => Promise<void>;
}) {
  const [data, setData] = useState<ScenarioInput>(initial);
  const [newStep, setNewStep] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (patch: Partial<ScenarioInput>) => setData(d => ({ ...d, ...patch }));
  const addStep = () => { const s = newStep.trim(); if (!s) return; set({ steps: [...data.steps, s] }); setNewStep(''); };
  const removeStep = (i: number) => set({ steps: data.steps.filter((_, idx) => idx !== i) });
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir; if (j < 0 || j >= data.steps.length) return;
    const arr = [...data.steps]; [arr[i], arr[j]] = [arr[j], arr[i]]; set({ steps: arr });
  };
  const editStep = (i: number, v: string) => set({ steps: data.steps.map((s, idx) => idx === i ? v : s) });

  const save = async () => {
    if (!data.name.trim()) { setError('Give the scenario a name.'); return; }
    setSaving(true); setError(null);
    try { await onSave({ ...data, steps: data.steps.filter(s => s.trim()) }); }
    catch (e: any) { setError(e?.message || 'Could not save.'); setSaving(false); }
  };

  return (
    <ModalPortal onClose={onClose}>
      <div style={overlay} onClick={onClose}>
        <div className="card modal-pop" style={{ width: 640, maxWidth: '94vw', maxHeight: '90vh', overflowY: 'auto', padding: 28, position: 'relative' }} onClick={e => e.stopPropagation()}>
          <button onClick={onClose} style={closeBtn}><X size={18} /></button>
          <h3 style={{ margin: '0 0 18px' }}>{isNew ? 'New Scenario' : 'Edit Scenario'}</h3>

          <div style={{ display: 'grid', gap: 14 }}>
            <div>
              <label style={labelStyle}>Name</label>
              <input value={data.name} onChange={e => set({ name: e.target.value })} placeholder="e.g. Book table then cancel" style={inputStyle} />
            </div>
            <div>
              <label style={labelStyle}>Description (optional)</label>
              <input value={data.description || ''} onChange={e => set({ description: e.target.value })} placeholder="What this scenario checks" style={inputStyle} />
            </div>
            <div style={{ display: 'flex', gap: 12 }}>
              <div style={{ flex: 1 }}>
                <label style={labelStyle}>App (project)</label>
                <select value={data.project_id || ''} onChange={e => set({ project_id: e.target.value })} style={{ ...inputStyle, cursor: 'pointer' }}>
                  <option value="">— select —</option>
                  {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
              <div style={{ flex: 1 }}>
                <label style={labelStyle}>Device (simulator)</label>
                <select value={data.device_id || ''} onChange={e => set({ device_id: e.target.value })} style={{ ...inputStyle, cursor: 'pointer' }}>
                  <option value="">— select —</option>
                  {sims.map(s => <option key={s.udid} value={s.udid}>{s.name}{s.state === 'Booted' ? ' ● booted' : ''}</option>)}
                </select>
              </div>
            </div>

            <div>
              <label style={labelStyle}>Steps</label>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 10 }}>
                {data.steps.length === 0 && <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>No steps yet — add plain-language steps below (e.g. “tap Book Table”, “select date”, “confirm”).</div>}
                {data.steps.map((step, i) => (
                  <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <GripVertical size={14} color="var(--text-muted)" />
                    <span style={{ width: 20, fontSize: '0.75rem', color: 'var(--text-muted)' }}>{i + 1}.</span>
                    <input value={step} onChange={e => editStep(i, e.target.value)} style={{ ...inputStyle, flex: 1 }} />
                    <button onClick={() => move(i, -1)} disabled={i === 0} style={stepBtn} title="Up"><ArrowUp size={13} /></button>
                    <button onClick={() => move(i, 1)} disabled={i === data.steps.length - 1} style={stepBtn} title="Down"><ArrowDown size={13} /></button>
                    <button onClick={() => removeStep(i)} style={{ ...stepBtn, color: 'var(--danger)' }} title="Remove"><X size={13} /></button>
                  </div>
                ))}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <input value={newStep} onChange={e => setNewStep(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addStep(); } }}
                  placeholder="Add a step and press Enter" style={{ ...inputStyle, flex: 1 }} />
                <button className="btn" onClick={addStep} style={{ padding: '9px 14px', display: 'inline-flex', alignItems: 'center', gap: 5 }}><Plus size={14} /> Add</button>
              </div>
            </div>

            {error && <p style={{ color: 'var(--danger)', fontSize: '0.82rem', margin: 0 }}>{error}</p>}
            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 4 }}>
              <button onClick={onClose} style={{ ...iconBtn, width: 'auto', padding: '0 16px' }}>Cancel</button>
              <button className="btn" onClick={save} disabled={saving} style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '9px 20px' }}>
                {saving ? <Loader2 size={14} className="spin" /> : null} {isNew ? 'Create' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </ModalPortal>
  );
}

// ── Live run ─────────────────────────────────────────────────────────────────
function RunModal({ scenario, projects, onClose }: { scenario: SavedScenario; projects: Project[]; onClose: () => void }) {
  const [envId, setEnvId] = useState(scenario.project_id || '');
  const [prepare, setPrepare] = useState(false);
  const [started, setStarted] = useState(false);
  const [events, setEvents] = useState<ScenarioEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!started) return;
    setBusy(true);
    const ac = new AbortController();
    runScenarioStream(
      // No bundle_id: each environment (project) has its own app, so the backend
      // uses the chosen environment's bundle id — same steps, different app.
      { project_id: envId, steps: scenario.steps, device_id: scenario.device_id!, name: scenario.name, save: false, prepare },
      (ev) => { if (!ac.signal.aborted) setEvents(prev => [...prev, ev]); },
      ac.signal,
    ).catch(e => { if (!ac.signal.aborted) setFatal(e?.message || 'Run failed to start'); })
      .finally(() => { if (!ac.signal.aborted) setBusy(false); });
    return () => ac.abort();
  }, [started]);

  useEffect(() => { logRef.current?.scrollTo({ top: logRef.current.scrollHeight }); }, [events]);

  const done = events.find(e => e.type === 'done') as Extract<ScenarioEvent, { type: 'done' }> | undefined;
  const envName = projects.find(p => p.id === envId)?.name || 'default';

  if (!started) {
    return (
      <ModalPortal onClose={onClose}>
        <div style={overlay} onClick={onClose}>
          <div className="card modal-pop" style={{ width: 480, maxWidth: '94vw', padding: 26, position: 'relative' }} onClick={e => e.stopPropagation()}>
            <button onClick={onClose} style={closeBtn}><X size={18} /></button>
            <h3 style={{ margin: '0 0 4px' }}>Run: {scenario.name}</h3>
            <p style={{ fontSize: '0.83rem', color: 'var(--text-secondary)', marginTop: 4 }}>Same steps — choose which environment to run against.</p>
            <label style={{ display: 'block', fontSize: '0.68rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', margin: '14px 0 5px', fontWeight: 600 }}>Environment (app / repo)</label>
            <select value={envId} onChange={e => setEnvId(e.target.value)} style={{ ...inputStyle, cursor: 'pointer' }}>
              {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 14, fontSize: '0.85rem', cursor: 'pointer' }}>
              <input type="checkbox" checked={prepare} onChange={e => setPrepare(e.target.checked)} />
              Pull latest &amp; rebuild before running <span style={{ color: 'var(--text-muted)' }}>(use for staging — daily builds)</span>
            </label>
            <button className="btn" onClick={() => setStarted(true)} disabled={!envId} style={{ marginTop: 18, display: 'inline-flex', alignItems: 'center', gap: 7, padding: '9px 20px' }}>
              <Play size={14} /> Run{prepare ? ' (build + install first)' : ''}
            </button>
          </div>
        </div>
      </ModalPortal>
    );
  }

  return (
    <ModalPortal onClose={onClose}>
      <div style={overlay} onClick={onClose}>
        <div className="card modal-pop" style={{ width: 640, maxWidth: '94vw', maxHeight: '90vh', display: 'flex', flexDirection: 'column', padding: 24, position: 'relative' }} onClick={e => e.stopPropagation()}>
          <button onClick={onClose} style={closeBtn}><X size={18} /></button>
          <h3 style={{ margin: '0 0 4px' }}>Running: {scenario.name} <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)', fontWeight: 400 }}>· {envName}</span></h3>
          <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 14 }}>
            {busy ? <><Loader2 size={12} className="spin" /> live…</> : done ? (done.ok ? 'Completed' : 'Completed with failures') : fatal ? 'Failed to run' : 'Finished'}
          </div>

          <div ref={logRef} style={{ flex: 1, overflowY: 'auto', background: 'rgba(0,0,0,0.25)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: 12, display: 'flex', flexDirection: 'column', gap: 6, minHeight: 200 }}>
            {events.length === 0 && !fatal && <div style={{ color: 'var(--text-muted)', fontSize: '0.82rem' }}>Connecting to the simulator…</div>}
            {events.map((ev, i) => <EventRow key={i} ev={ev} />)}
            {fatal && <div style={{ color: 'var(--danger)', fontSize: '0.85rem', display: 'flex', gap: 6, alignItems: 'center' }}><AlertTriangle size={14} /> {fatal}</div>}
          </div>

          {done && (
            <div style={{ marginTop: 14, display: 'flex', alignItems: 'center', gap: 10 }}>
              <span className={`badge ${done.ok ? 'passed' : 'failed'}`}>{done.ok ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}{done.ok ? 'Passed' : 'Failed'}</span>
              <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>{done.passed}/{done.total} steps passed</span>
              {!!done.healed && <span title="Steps that recovered from a changed locator" style={{ fontSize: '0.72rem', background: 'rgba(251,191,36,0.16)', color: '#fbbf24', padding: '2px 8px', borderRadius: 99 }}>{done.healed} self-healed</span>}
            </div>
          )}
        </div>
      </div>
    </ModalPortal>
  );
}

// ── Run all scenarios in sequence ────────────────────────────────────────────
type BatchStatus = 'pending' | 'running' | 'passed' | 'failed';
function RunAllModal({ scenarios, onClose }: { scenarios: SavedScenario[]; onClose: () => void }) {
  const [status, setStatus] = useState<Record<string, BatchStatus>>(
    () => Object.fromEntries(scenarios.map(s => [s.id, 'pending' as BatchStatus])));
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [phase, setPhase] = useState('');
  const [finished, setFinished] = useState(false);

  useEffect(() => {
    const ac = new AbortController();
    // All scenarios share one Appium session (session setup paid once, not per
    // scenario) — dramatically faster than N separate runs. They must run on the
    // same environment + device, so we take the first scenario's project/device.
    const base = scenarios[0];
    runScenariosBatch(
      { project_id: base.project_id!, device_id: base.device_id!, bundle_id: base.bundle_id || undefined,
        scenarios: scenarios.map(s => ({ name: s.name, steps: s.steps })) },
      (ev) => {
        if (ac.signal.aborted) return;
        if (ev.type === 'phase') setPhase(ev.message);
        else if (ev.type === 'scenario_start') { setCurrentId(scenarios[ev.index]?.id ?? null); setPhase(''); setStatus(p => ({ ...p, [scenarios[ev.index].id]: 'running' })); }
        else if (ev.type === 'step') setPhase(`${ev.index + 1}/${ev.total} ${ev.step}`);
        else if (ev.type === 'scenario_done') setStatus(p => ({ ...p, [scenarios[ev.index].id]: ev.ok ? 'passed' : 'failed' }));
      },
      ac.signal,
    ).catch(() => { if (!ac.signal.aborted) setPhase('Batch run failed to start.'); })
      .finally(() => { if (!ac.signal.aborted) { setFinished(true); setCurrentId(null); } });
    return () => ac.abort();
  }, [scenarios]);

  const passed = Object.values(status).filter(s => s === 'passed').length;
  const failed = Object.values(status).filter(s => s === 'failed').length;
  const icon = (st: BatchStatus) =>
    st === 'passed' ? <CheckCircle2 size={15} color="var(--success)" />
    : st === 'failed' ? <AlertTriangle size={15} color="var(--danger)" />
    : st === 'running' ? <Loader2 size={15} className="spin" color="var(--accent-primary)" />
    : <span style={{ width: 15, display: 'inline-block', textAlign: 'center', color: 'var(--text-muted)' }}>·</span>;

  return (
    <ModalPortal onClose={onClose}>
      <div style={overlay} onClick={onClose}>
        <div className="card modal-pop" style={{ width: 560, maxWidth: '94vw', maxHeight: '90vh', display: 'flex', flexDirection: 'column', padding: 24, position: 'relative' }} onClick={e => e.stopPropagation()}>
          <button onClick={onClose} style={closeBtn}><X size={18} /></button>
          <h3 style={{ margin: '0 0 4px' }}>Run All Scenarios</h3>
          <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: 14 }}>
            {finished ? `Done — ${passed} passed, ${failed} failed` : <><Loader2 size={12} className="spin" /> running {scenarios.length} scenarios in sequence…</>}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, overflowY: 'auto' }}>
            {scenarios.map(sc => (
              <div key={sc.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px', borderRadius: 'var(--radius-sm)', background: currentId === sc.id ? 'rgba(255,255,255,0.05)' : 'transparent', border: '1px solid var(--border-color)' }}>
                {icon(status[sc.id])}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '0.88rem', fontWeight: 500 }}>{sc.name}</div>
                  {currentId === sc.id && phase && <div style={{ fontSize: '0.74rem', color: 'var(--text-muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{phase}</div>}
                </div>
                <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>{sc.steps.length} steps</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </ModalPortal>
  );
}

function EventRow({ ev }: { ev: ScenarioEvent }) {
  if (ev.type === 'phase') return <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>{ev.message}</div>;
  if (ev.type === 'step_start') return <div style={{ fontSize: '0.84rem', color: 'var(--text-secondary)', display: 'flex', gap: 6, alignItems: 'center' }}><Loader2 size={12} className="spin" /> <span style={{ opacity: 0.7 }}>{ev.index + 1}/{ev.total}</span> {ev.step}</div>;
  if (ev.type === 'step') return (
    <div style={{ fontSize: '0.84rem', display: 'flex', gap: 6, alignItems: 'flex-start', color: ev.ok ? 'var(--success)' : 'var(--danger)' }}>
      {ev.ok ? <CheckCircle2 size={13} style={{ marginTop: 2, flexShrink: 0 }} /> : <AlertTriangle size={13} style={{ marginTop: 2, flexShrink: 0 }} />}
      <span>
        <strong style={{ color: 'var(--text-primary)' }}>{ev.step}</strong>
        {ev.healed && <span title={ev.healed_note} style={{ marginLeft: 6, fontSize: '0.66rem', background: 'rgba(251,191,36,0.16)', color: '#fbbf24', padding: '1px 6px', borderRadius: 99 }}>self-healed</span>}
        {ev.detail ? <span style={{ color: 'var(--text-muted)' }}> — {ev.detail}</span> : null}
        {ev.healed && ev.healed_note ? <span style={{ color: 'var(--text-muted)', display: 'block', fontSize: '0.75rem' }}>{ev.healed_note}</span> : null}
      </span>
    </div>
  );
  if (ev.type === 'error') return <div style={{ color: 'var(--danger)', fontSize: '0.84rem', display: 'flex', gap: 6, alignItems: 'center' }}><AlertTriangle size={13} /> {ev.detail}</div>;
  return null;
}

const overlay: React.CSSProperties = {
  position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)', backdropFilter: 'blur(6px)',
  display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 999, padding: 16,
};
const closeBtn: React.CSSProperties = {
  position: 'absolute', top: 16, right: 16, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex',
};
const stepBtn: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'center', width: 28, height: 32,
  background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 6, color: 'var(--text-secondary)', cursor: 'pointer',
};
