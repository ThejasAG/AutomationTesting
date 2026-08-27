import { useEffect, useRef, useState } from 'react';
import {
  Plus, Play, Pencil, Trash2, X, Loader2,
  Smartphone, CheckCircle2, AlertTriangle, ListChecks, PlayCircle,
} from 'lucide-react';
import {
  getScenarios, createScenario, updateScenario, deleteScenario, getScenarioDevices,
  getProjects, runScenarioStream, runScenariosBatch, getEngineStatus,
  listCrossAppFlows, runCrossAppFlow,
} from '../api';
import type { SavedScenario, ScenarioInput, SimDevice, Project, ScenarioEvent, EngineStatus, CrossAppFlow } from '../api';
import ModalPortal from '../components/ModalPortal';
import ScenarioEditorModal from '../components/ScenarioEditorModal';
import RecorderModal from '../components/RecorderModal';
import { Circle } from 'lucide-react';

const inputStyle: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', background: 'rgba(255,255,255,0.05)',
  border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)',
  color: 'var(--text-primary)', padding: '9px 11px', fontSize: '0.88rem', fontFamily: 'inherit',
};

/** Which app a scenario drives — read from its project, so it follows the data
 *  rather than a naming convention. Cross-app SUITES are a separate concept: they
 *  orchestrate several apps at once and live in cross_app_flows, not here. */
type AppGroup = 'consumer' | 'business' | 'other';
const groupOf = (projectName: string): AppGroup => {
  const n = (projectName || '').toLowerCase();
  if (n.includes('consumer')) return 'consumer';
  if (n.includes('business') || n.includes('buisness')) return 'business';
  return 'other';
};
const GROUP_META: Record<AppGroup, { label: string; hint: string; color: string }> = {
  consumer: { label: 'Consumer app', hint: 'Diner journeys — booking, pre-order, wallet', color: '#34d399' },
  business: { label: 'Business app', hint: 'Waiter and kitchen journeys on the iPad', color: '#60a5fa' },
  other:    { label: 'Unassigned',   hint: 'No project set — pick one so it lands in a section', color: '#94a3b8' },
};

const empty = (): ScenarioInput => ({ name: '', description: '', project_id: '', device_id: '', steps: [], covers: [] });

export default function ScenariosPage() {
  const [scenarios, setScenarios] = useState<SavedScenario[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [sims, setSims] = useState<SimDevice[]>([]);
  // Cross-app SUITES orchestrate consumer + waiter + kitchen together. They are not
  // saved scenarios, so they get their own section rather than being invisible here.
  const [flows, setFlows] = useState<CrossAppFlow[]>([]);
  const [flowBusy, setFlowBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<{ id: string | null; data: ScenarioInput } | null>(null);
  const [running, setRunning] = useState<SavedScenario | null>(null);
  const [runAll, setRunAll] = useState(false);
  const [recording, setRecording] = useState(false);
  const [engine, setEngine] = useState<EngineStatus | null>(null);

  useEffect(() => {
    const tick = () => getEngineStatus().then(setEngine).catch(() => {});
    tick();
    const t = setInterval(tick, 8000);
    return () => clearInterval(t);
  }, []);

  // Scenarios that are fully configured and therefore runnable.
  const runnable = scenarios.filter(s => s.project_id && s.device_id && s.steps.length > 0);

  const refresh = async () => setScenarios(await getScenarios());

  useEffect(() => {
    (async () => {
      try {
        const [s, p, d] = await Promise.all([getScenarios(), getProjects(), getScenarioDevices()]);
        setScenarios(s); setProjects(p); setSims(d.simulators);
        // Suites are fetched separately so a flow-listing hiccup never blanks the page.
        listCrossAppFlows().then(r => setFlows(r.flows)).catch(() => {});
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
          {engine && (
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: 14, marginTop: 8, fontSize: '0.78rem' }}>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: engine.appium.healthy ? 'var(--success)' : '#fbbf24' }}>
                <span style={{ width: 8, height: 8, borderRadius: 99, background: engine.appium.healthy ? 'var(--success)' : '#fbbf24', display: 'inline-block' }} />
                Automation engine: {engine.appium.healthy ? 'ready' : 'will auto-start on run'}
              </span>
              <span style={{ color: 'var(--text-muted)' }}>
                {engine.booted_simulators.length ? `${engine.booted_simulators.length} sim booted` : 'no sim booted (auto-boots on run)'}
              </span>
            </div>
          )}
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
        <>
        {(['consumer', 'business', 'other'] as AppGroup[]).map(gk => {
          const meta = GROUP_META[gk];
          const inGroup = scenarios.filter(s => groupOf(projName(s.project_id)) === gk);
          if (inGroup.length === 0) return null;
          return (
          <section key={gk} style={{ marginBottom: 30 }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12,
                          borderLeft: `3px solid ${meta.color}`, paddingLeft: 10 }}>
              <h2 style={{ margin: 0, fontSize: '1.02rem' }}>{meta.label}</h2>
              <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
                {inGroup.length} scenario{inGroup.length === 1 ? '' : 's'} · {meta.hint}
              </span>
            </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 16 }}>
          {inGroup.map(s => (
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
                <button onClick={() => setEditing({ id: s.id, data: { name: s.name, description: s.description || '', project_id: s.project_id || '', bundle_id: s.bundle_id || '', device_id: s.device_id || '', steps: s.steps, covers: s.covers || [] } })}
                  title="Edit" style={iconBtn}><Pencil size={14} /></button>
                <button onClick={() => onDelete(s)} title="Delete" style={{ ...iconBtn, color: 'var(--danger)' }}><Trash2 size={14} /></button>
              </div>
            </div>
          ))}
        </div>
          </section>);
        })}

        {flows.length > 0 && (
          <section style={{ marginBottom: 30 }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12,
                          borderLeft: '3px solid #a78bfa', paddingLeft: 10 }}>
              <h2 style={{ margin: 0, fontSize: '1.02rem' }}>Cross-app suites</h2>
              <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
                {flows.length} suites · one journey across consumer, waiter and kitchen
              </span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 16 }}>
              {flows.map(f => (
                <div key={f.id} className="card" style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <div style={{ fontWeight: 600, fontSize: '1rem' }}>
                    {f.name}
                    {f.edited && (
                      <span style={{ marginLeft: 8, fontSize: '0.62rem', padding: '2px 7px', borderRadius: 20,
                                     background: 'rgba(251,191,36,0.15)', color: '#fbbf24' }}>edited</span>
                    )}
                  </div>
                  <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                    {f.segments.length} segment{f.segments.length === 1 ? '' : 's'} ·{' '}
                    {f.segments.map(sg => sg.role).join(' → ')}
                  </div>
                  <button className="btn" disabled={!!flowBusy}
                    onClick={async () => {
                      setFlowBusy(f.id);
                      try { await runCrossAppFlow(f.id, 'staging', 'tablet'); }
                      finally { setFlowBusy(null); }
                    }}
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '8px 14px', alignSelf: 'flex-start' }}>
                    {flowBusy === f.id ? <Loader2 size={13} className="spin" /> : <Play size={13} />} Run suite
                  </button>
                </div>
              ))}
            </div>
          </section>
        )}
        </>
      )}

      {editing && (
        <ScenarioEditorModal
          initial={editing.data} isNew={editing.id === null} projects={projects} sims={sims}
          onClose={() => setEditing(null)}
          onSave={async (data) => {
            if (editing.id) await updateScenario(editing.id, data);
            else await createScenario(data);
            setEditing(null); await refresh();
          }}
        />
      )}

      {running && <RunModal scenario={running} projects={projects} sims={sims} onClose={() => setRunning(null)} />}
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

function simNameOf(sims: SimDevice[], udid: string | null): string {
  return sims.find(d => d.udid === udid)?.name || (udid ? udid.slice(0, 8) : 'its saved device');
}

const iconBtn: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'center', width: 34, height: 34,
  background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)',
  color: 'var(--text-secondary)', cursor: 'pointer',
};

// ── Editor ───────────────────────────────────────────────────────────────────
// ── Live run ─────────────────────────────────────────────────────────────────
function RunModal({ scenario, projects, sims, onClose }: { scenario: SavedScenario; projects: Project[]; sims: SimDevice[]; onClose: () => void }) {
  const [envId, setEnvId] = useState(scenario.project_id || '');
  // Per-run device override. The Business scenarios are all saved against the iPad, but
  // the same journeys have to be runnable on the iPhone — the app ships both builds. This
  // does NOT rewrite the scenario: the saved device stays the default, so "run this one on
  // the phone" is a one-off rather than a destructive edit to a shared scenario.
  const [deviceId, setDeviceId] = useState(scenario.device_id || '');
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
      { project_id: envId, steps: scenario.steps, device_id: deviceId, name: scenario.name, save: false, prepare },
      (ev) => { if (!ac.signal.aborted) setEvents(prev => [...prev, ev]); },
      ac.signal,
    ).catch(e => { if (!ac.signal.aborted) setFatal(e?.message || 'Run failed to start'); })
      .finally(() => { if (!ac.signal.aborted) setBusy(false); });
    return () => ac.abort();
  }, [started]);

  const deviceName = sims.find(d => d.udid === deviceId)?.name || 'device';
  const bundleOf = (pid: string) => projects.find(p => p.id === pid)?.app_bundle_id || '';
  /** Is this env's app on this device? null = we cannot know (sim shut down) — and an
   *  unknown must never be shown as missing, or every shut-down sim looks broken. */
  const hasApp = (udid: string, pid: string): boolean | null => {
    const apps = sims.find(d => d.udid === udid)?.apps;
    const bundle = bundleOf(pid);
    if (apps === null || apps === undefined || !bundle) return null;
    return apps.includes(bundle);
  };
  const installed = hasApp(deviceId, envId);

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
              {projects.map(p => {
                const ok = hasApp(deviceId, p.id);
                return (
                  <option key={p.id} value={p.id}>
                    {p.name}{ok === true ? ' ✓ installed' : ok === false ? ' — not on this device' : ''}
                  </option>
                );
              })}
            </select>
            <label style={{ display: 'block', fontSize: '0.68rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', margin: '14px 0 5px', fontWeight: 600 }}>Device</label>
            <select value={deviceId} onChange={e => setDeviceId(e.target.value)} style={{ ...inputStyle, cursor: 'pointer' }}>
              {sims.map(d => {
                const ok = hasApp(d.udid, envId);
                return (
                  <option key={d.udid} value={d.udid}>
                    {d.name}{d.udid === scenario.device_id ? ' (saved default)' : ''}
                    {d.state === 'Booted' ? ' ● booted' : ''}
                    {ok === true ? ' ✓ app installed' : ok === false ? ' — app not installed' : ''}
                  </option>
                );
              })}
            </select>
            {deviceId !== scenario.device_id && (
              <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 5 }}>
                One-off — the scenario still defaults to {simNameOf(sims, scenario.device_id)}.
              </div>
            )}
            {installed === false && (
              <div style={{
                marginTop: 8, padding: '8px 10px', borderRadius: 6, fontSize: '0.78rem',
                background: 'rgba(251,191,36,0.12)', border: '1px solid rgba(251,191,36,0.35)',
                color: 'var(--text-secondary)',
              }}>
                <strong>{projects.find(p => p.id === envId)?.name}</strong> ({bundleOf(envId)}) is
                not installed on {deviceName}. Pick an environment marked ✓, or install it
                via Latest build.
              </div>
            )}
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 14, fontSize: '0.85rem', cursor: 'pointer' }}>
              <input type="checkbox" checked={prepare} onChange={e => setPrepare(e.target.checked)} />
              Pull latest &amp; rebuild before running <span style={{ color: 'var(--text-muted)' }}>(use for staging — daily builds)</span>
            </label>
            {/* Blocked only on a DEFINITE no. `installed === null` means the simulator is
                shut down so we could not look — that stays runnable, and the backend
                preflight catches it in ~2s with the same explanation. */}
            <button className="btn" onClick={() => setStarted(true)} disabled={!envId || !deviceId || installed === false}
              title={installed === false ? 'That app is not installed on this simulator' : 'Run this scenario'}
              style={{ marginTop: 18, display: 'inline-flex', alignItems: 'center', gap: 7, padding: '9px 20px' }}>
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
          <h3 style={{ margin: '0 0 4px' }}>Running: {scenario.name} <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)', fontWeight: 400 }}>· {envName} · {deviceName}</span></h3>
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
