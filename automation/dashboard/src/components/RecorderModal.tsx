import { useEffect, useRef, useState } from 'react';
import { X, Circle, Square, Save, Loader2, Trash2, RefreshCw, MousePointerClick } from 'lucide-react';
import {
  recorderStart, recorderFrame, recorderTap, recorderSetSteps, recorderSave, recorderStop,
} from '../api';
import type { Project, SimDevice } from '../api';
import ModalPortal from './ModalPortal';

const input: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', background: 'rgba(255,255,255,0.05)',
  border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)',
  color: 'var(--text-primary)', padding: '8px 10px', fontSize: '0.85rem', fontFamily: 'inherit',
};

/** Record a scenario by clicking the live simulator mirror. Each click taps the
 *  real sim through Appium and captures the tapped element as an editable step. */
export default function RecorderModal({ projects, sims, onClose, onSaved }: {
  projects: Project[]; sims: SimDevice[]; onClose: () => void; onSaved: () => void;
}) {
  const [projectId, setProjectId] = useState(projects[0]?.id || '');
  const [deviceId, setDeviceId] = useState(sims.find(s => s.state === 'Booted')?.udid || sims[0]?.udid || '');

  // The lists may arrive after this modal mounts; a controlled <select> then
  // shows the first option while its value is still '' (so Start thinks nothing
  // is picked). Backfill the state to match what's displayed.
  useEffect(() => { if (!projectId && projects.length) setProjectId(projects[0].id); }, [projects, projectId]);
  useEffect(() => {
    if (!deviceId && sims.length) setDeviceId(sims.find(s => s.state === 'Booted')?.udid || sims[0].udid);
  }, [sims, deviceId]);
  const [sid, setSid] = useState<string | null>(null);
  const [frame, setFrame] = useState<string | null>(null);
  const [steps, setSteps] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [tapping, setTapping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const imgRef = useRef<HTMLImageElement>(null);
  const sidRef = useRef<string | null>(null);
  const tappingRef = useRef(false);

  const [dead, setDead] = useState(false);

  const refreshFrame = async (id: string) => {
    try { const f = await recorderFrame(id); setFrame(f.image); setSteps(f.steps); setDead(false); }
    catch (e: any) {
      // 410 = the Appium session ended; stop polling and tell the user.
      if (String(e?.message || '').includes('ended') || String(e?.status) === '410') {
        setDead(true);
        setError('The recording session ended (Appium stopped or was restarted). Close and start a new recording.');
      }
    }
  };

  const start = async () => {
    if (!projectId || !deviceId) { setError('Pick an app and a device.'); return; }
    setBusy(true); setError(null);
    try {
      const r = await recorderStart({ project_id: projectId, device_id: deviceId });
      setSid(r.session_id); sidRef.current = r.session_id;
      await refreshFrame(r.session_id);
    } catch (e: any) { setError(e?.message || 'Could not start recording'); }
    setBusy(false);
  };

  // Keep the mirror live — self-scheduling so frame fetches never overlap (an
  // Appium session is not concurrency-safe, and each frame now also reads the UI
  // tree). Pauses itself while a tap is in flight.
  useEffect(() => {
    if (!sid || dead) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const loop = async () => {
      if (cancelled) return;
      if (!tappingRef.current) await refreshFrame(sid);
      if (!cancelled) timer = setTimeout(loop, 500);
    };
    timer = setTimeout(loop, 400);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [sid, dead]);

  // Release the Appium session if the modal closes mid-recording.
  useEffect(() => () => { if (sidRef.current) recorderStop(sidRef.current); }, []);

  const onMirrorClick = async (e: React.MouseEvent<HTMLImageElement>) => {
    if (!sid || tapping || dead) return;
    const rect = imgRef.current!.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    setTapping(true); tappingRef.current = true;
    try {
      const r = await recorderTap(sid, x, y);
      setSteps(r.steps);
      if (r.image) setFrame(r.image);   // tap returns the new frame — no extra round trip
    } catch (err: any) { setError(err?.message || 'Tap failed'); }
    setTapping(false); tappingRef.current = false;
  };

  const removeStep = async (i: number) => {
    const next = steps.filter((_, idx) => idx !== i);
    setSteps(next);
    if (sid) await recorderSetSteps(sid, next);
  };
  const editStep = (i: number, v: string) => setSteps(steps.map((s, idx) => idx === i ? v : s));
  const commitSteps = async () => { if (sid) await recorderSetSteps(sid, steps); };

  const save = async () => {
    if (!sid) return;
    if (!name.trim()) { setError('Give the scenario a name.'); return; }
    setBusy(true); setError(null);
    try {
      await recorderSetSteps(sid, steps);
      await recorderSave(sid, name.trim());
      await recorderStop(sid); sidRef.current = null;
      onSaved();
    } catch (e: any) { setError(e?.message || 'Could not save'); setBusy(false); }
  };

  const stop = async () => { if (sid) { await recorderStop(sid); sidRef.current = null; } onClose(); };

  return (
    <ModalPortal onClose={stop}>
      <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)', backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 999, padding: 16 }} onClick={stop}>
        <div className="card modal-pop" style={{ width: 900, maxWidth: '96vw', maxHeight: '92vh', display: 'flex', flexDirection: 'column', padding: 22, position: 'relative' }} onClick={e => e.stopPropagation()}>
          <button onClick={stop} style={{ position: 'absolute', top: 14, right: 14, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex' }}><X size={18} /></button>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
            <Circle size={16} fill="var(--danger)" color="var(--danger)" />
            <h3 style={{ margin: 0 }}>Record Scenario</h3>
          </div>

          {!sid ? (
            <>
              <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginTop: 6 }}>
                Pick the app and simulator. The live sim appears here — click it to tap through and capture each step.
              </p>
              <div style={{ display: 'flex', gap: 12, marginTop: 8 }}>
                <div style={{ flex: 1 }}>
                  <label style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', fontWeight: 600 }}>App</label>
                  <select value={projectId} onChange={e => setProjectId(e.target.value)} style={{ ...input, cursor: 'pointer' }}>
                    {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </div>
                <div style={{ flex: 1 }}>
                  <label style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', fontWeight: 600 }}>Device</label>
                  <select value={deviceId} onChange={e => setDeviceId(e.target.value)} style={{ ...input, cursor: 'pointer' }}>
                    {sims.map(s => <option key={s.udid} value={s.udid}>{s.name}{s.state === 'Booted' ? ' ● booted' : ''}</option>)}
                  </select>
                </div>
              </div>
              {error && <p style={{ color: 'var(--danger)', fontSize: '0.82rem' }}>{error}</p>}
              <button className="btn" onClick={start} disabled={busy} style={{ marginTop: 16, display: 'inline-flex', alignItems: 'center', gap: 8, alignSelf: 'flex-start', padding: '10px 20px' }}>
                {busy ? <Loader2 size={15} className="spin" /> : <Circle size={14} fill="currentColor" />}
                {busy ? 'Starting the simulator…' : 'Start recording'}
              </button>
            </>
          ) : (
            <div style={{ display: 'flex', gap: 18, marginTop: 12, minHeight: 0, flex: 1 }}>
              {/* Live mirror */}
              <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
                <div style={{ position: 'relative', borderRadius: 14, overflow: 'hidden', border: '1px solid var(--border-color)', maxHeight: '70vh' }}>
                  {frame
                    ? <img ref={imgRef} src={frame} onClick={onMirrorClick} alt="simulator"
                        style={{ display: 'block', height: '70vh', width: 'auto', cursor: tapping ? 'wait' : 'crosshair' }} />
                    : <div style={{ width: 300, height: 500, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Loader2 size={20} className="spin" /></div>}
                  {tapping && <div style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.15)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Loader2 size={22} className="spin" color="#fff" /></div>}
                </div>
                <div style={{ fontSize: '0.74rem', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
                  <MousePointerClick size={13} /> click the screen to tap + capture
                  <button onClick={() => sid && refreshFrame(sid)} title="Refresh" style={{ background: 'transparent', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', display: 'flex' }}><RefreshCw size={13} /></button>
                </div>
              </div>

              {/* Recorded steps */}
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', fontWeight: 600, marginBottom: 8 }}>
                  Recorded steps ({steps.length})
                </div>
                <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6, minHeight: 120 }}>
                  {steps.length === 0 && <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>Click the simulator to record your first step.</div>}
                  {steps.map((s, i) => (
                    <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ width: 20, fontSize: '0.75rem', color: 'var(--text-muted)' }}>{i + 1}.</span>
                      <input value={s} onChange={e => editStep(i, e.target.value)} onBlur={commitSteps} style={{ ...input, flex: 1 }} />
                      <button onClick={() => removeStep(i)} style={{ background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 6, color: 'var(--danger)', cursor: 'pointer', width: 30, height: 32, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><Trash2 size={13} /></button>
                    </div>
                  ))}
                </div>

                <div style={{ borderTop: '1px solid var(--border-color)', marginTop: 12, paddingTop: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <input value={name} onChange={e => setName(e.target.value)} placeholder="Scenario name" style={input} />
                  {error && <p style={{ color: 'var(--danger)', fontSize: '0.8rem', margin: 0 }}>{error}</p>}
                  <div style={{ display: 'flex', gap: 10 }}>
                    <button className="btn" onClick={save} disabled={busy || steps.length === 0} style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '9px 18px' }}>
                      {busy ? <Loader2 size={14} className="spin" /> : <Save size={14} />} Save scenario
                    </button>
                    <button onClick={stop} style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '9px 16px', background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', color: 'var(--text-secondary)', cursor: 'pointer' }}>
                      <Square size={13} /> Stop
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </ModalPortal>
  );
}
