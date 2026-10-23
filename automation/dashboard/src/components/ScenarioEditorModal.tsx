import { useEffect, useState } from 'react';
import { X, Plus, ArrowUp, ArrowDown, GripVertical, Loader2, Sparkles } from 'lucide-react';
import {
  suggestCovers, getProjects, getScenarioDevices, createScenario, updateScenario,
} from '../api';
import type { ScenarioInput, Project, SimDevice, SavedScenario } from '../api';
import ModalPortal from './ModalPortal';

/** THE scenario editor — one component, used everywhere a scenario is shown
 *  (Scenarios page, Pull Requests, anywhere else). It was previously private to
 *  ScenariosPage, so every other surface could only *display* scenarios. Editing
 *  belongs wherever you see one, and a second copy would drift from this one.
 *
 *  `projects`/`sims` are optional: pass them if the host page already loaded them,
 *  otherwise the modal fetches them itself so a caller can open it with just a
 *  scenario and an onSaved callback. */

const inputStyle: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', background: 'rgba(255,255,255,0.05)',
  border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)',
  color: 'var(--text-primary)', padding: '9px 11px', fontSize: '0.88rem', fontFamily: 'inherit',
};
const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '0.68rem', color: 'var(--text-muted)', textTransform: 'uppercase',
  letterSpacing: '0.06em', marginBottom: 5, fontWeight: 600,
};
const overlay: React.CSSProperties = {
  position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)', backdropFilter: 'blur(6px)',
  display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 999, padding: 16,
};
const closeBtn: React.CSSProperties = {
  position: 'absolute', top: 16, right: 16, background: 'transparent', border: 'none',
  cursor: 'pointer', color: 'var(--text-muted)', display: 'flex',
};
const stepBtn: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'center', width: 28, height: 32,
  background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 6,
  color: 'var(--text-secondary)', cursor: 'pointer',
};
const plainBtn: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'center', height: 34,
  background: 'transparent', border: '1px solid var(--border-color)',
  borderRadius: 'var(--radius-sm)', color: 'var(--text-secondary)', cursor: 'pointer',
  padding: '0 16px',
};

export const emptyScenario = (): ScenarioInput =>
  ({ name: '', description: '', project_id: '', device_id: '', steps: [], covers: [] });

export default function ScenarioEditorModal({
  initial, isNew, projects: projectsIn, sims: simsIn, onClose, onSave, onSaved,
}: {
  initial: ScenarioInput & { id?: string };
  isNew: boolean;
  projects?: Project[];
  sims?: SimDevice[];
  onClose: () => void;
  /** Host controls persistence. Omit it and the modal saves via the API itself. */
  onSave?: (d: ScenarioInput) => Promise<void>;
  onSaved?: (s?: SavedScenario) => void;
}) {
  const [data, setData] = useState<ScenarioInput>(initial);
  const [projects, setProjects] = useState<Project[]>(projectsIn ?? []);
  const [sims, setSims] = useState<SimDevice[]>(simsIn ?? []);
  const [newStep, setNewStep] = useState('');
  const [saving, setSaving] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Self-sufficient: a caller that hasn't loaded these can still open the editor.
  useEffect(() => {
    if (!projectsIn) getProjects().then(setProjects).catch(() => {});
    if (!simsIn) getScenarioDevices().then(r => setSims(r.simulators)).catch(() => {});
  }, [projectsIn, simsIn]);

  const set = (patch: Partial<ScenarioInput>) => setData(d => ({ ...d, ...patch }));

  const suggest = async () => {
    if (!data.project_id || data.steps.length === 0) return;
    setSuggesting(true); setError(null);
    try {
      const r = await suggestCovers({ project_id: data.project_id, name: data.name, steps: data.steps });
      set({ covers: Array.from(new Set([...(data.covers || []), ...r.covers])) });
    } catch (e: any) { setError(e?.message || 'Could not suggest coverage'); }
    setSuggesting(false);
  };
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
    const clean = { ...data, steps: data.steps.filter(s => s.trim()) };
    try {
      if (onSave) {
        await onSave(clean);
      } else {                                   // stand-alone: persist here
        const saved = initial.id
          ? await updateScenario(initial.id, clean)
          : await createScenario(clean);
        onSaved?.(saved);
        onClose();
      }
    } catch (e: any) { setError(e?.message || 'Could not save.'); setSaving(false); }
  };

  return (
    <ModalPortal onClose={onClose}>
      <div style={overlay} onClick={onClose}>
        <div className="card modal-pop"
          style={{ width: 640, maxWidth: '94vw', maxHeight: '90vh', overflowY: 'auto', padding: 28, position: 'relative' }}
          onClick={e => e.stopPropagation()}>
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
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <label style={labelStyle}>Covers (screens / modules) — for smart PR selection</label>
                <button type="button" onClick={suggest} disabled={suggesting || !data.project_id || data.steps.length === 0}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: '0.72rem', background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 6, color: 'var(--accent-primary)', padding: '3px 8px', cursor: 'pointer', marginBottom: 5 }}>
                  {suggesting ? <Loader2 size={11} className="spin" /> : <Sparkles size={11} />} Suggest
                </button>
              </div>
              <input
                value={(data.covers || []).join(', ')}
                onChange={e => set({ covers: e.target.value.split(',').map(c => c.trim()).filter(Boolean) })}
                placeholder="e.g. Store, Cart, Checkout"
                style={inputStyle}
              />
              <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: 3 }}>
                A PR runs this scenario only when its changed files affect one of these. Leave blank to always run. <strong>Suggest</strong> maps your steps to the app's real screens.
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
              <button onClick={onClose} style={plainBtn}>Cancel</button>
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
