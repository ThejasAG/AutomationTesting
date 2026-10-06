import { useEffect, useState } from 'react';
import {
  X, Plus, Trash2, ArrowUp, ArrowDown, Save, RotateCcw, Loader2, ChevronDown,
} from 'lucide-react';
import { getFlowStepCatalog, saveCrossAppFlow, deleteCrossAppFlow } from '../api';
import type { CrossAppFlow, FlowSegment, StepCatalogEntry } from '../api';
import ModalPortal from './ModalPortal';

const ROLES = ['consumer', 'waiter', 'kitchen'];

/** Edit a cross-app flow: rename it, add/remove/reorder segments, and edit the steps
 *  inside each one. Saving a BUILT-IN flow stores an override that future runs use;
 *  "Revert" deletes that override and restores the shipped definition, so editing can
 *  never permanently break a working flow. */
export default function FlowEditorModal({ flow, onClose, onSaved }: {
  flow: CrossAppFlow | null;               // null = create a brand-new flow
  onClose: () => void;
  onSaved: () => void;
}) {
  const isNew = !flow;
  const [flowId, setFlowId] = useState(flow?.id ?? '');
  const [name, setName] = useState(flow?.name ?? '');
  const [description, setDescription] = useState(flow?.description ?? '');
  const [segments, setSegments] = useState<FlowSegment[]>(
    flow?.segments?.map((s) => ({ ...s, steps: [...s.steps] }))
      ?? [{ num: '1', name: 'Consumer: ', role: 'consumer', steps: [''] }],
  );
  const [catalog, setCatalog] = useState<Record<string, StepCatalogEntry[]>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [showPicker, setShowPicker] = useState<string | null>(null);   // "segIdx:stepIdx"

  useEffect(() => { getFlowStepCatalog().then((r) => setCatalog(r.catalog)).catch(() => {}); }, []);

  const patchSeg = (i: number, patch: Partial<FlowSegment>) =>
    setSegments((prev) => prev.map((s, k) => (k === i ? { ...s, ...patch } : s)));

  const moveSeg = (i: number, dir: -1 | 1) => setSegments((prev) => {
    const j = i + dir;
    if (j < 0 || j >= prev.length) return prev;
    const next = [...prev];
    [next[i], next[j]] = [next[j], next[i]];
    return next.map((s, k) => ({ ...s, num: String(k + 1) }));
  });

  const setStep = (si: number, ki: number, v: string) =>
    patchSeg(si, { steps: segments[si].steps.map((s, k) => (k === ki ? v : s)) });

  const addStep = (si: number, at?: number, value = '') => {
    const steps = [...segments[si].steps];
    steps.splice(at === undefined ? steps.length : at + 1, 0, value);
    patchSeg(si, { steps });
  };

  const removeStep = (si: number, ki: number) =>
    patchSeg(si, { steps: segments[si].steps.filter((_, k) => k !== ki) });

  const moveStep = (si: number, ki: number, dir: -1 | 1) => {
    const steps = [...segments[si].steps];
    const j = ki + dir;
    if (j < 0 || j >= steps.length) return;
    [steps[ki], steps[j]] = [steps[j], steps[ki]];
    patchSeg(si, { steps });
  };

  const save = async () => {
    setError('');
    const id = (isNew ? flowId : flow!.id).trim();
    if (!id) return setError('Give the flow an id (e.g. my_checkout_flow).');
    if (!/^[a-zA-Z0-9_-]+$/.test(id)) return setError('Id: letters, numbers, _ and - only.');
    if (!name.trim()) return setError('Give the flow a name.');
    const cleaned = segments.map((s, i) => ({
      ...s, num: String(i + 1), steps: s.steps.map((x) => x.trim()).filter(Boolean),
    }));
    if (cleaned.some((s) => !s.steps.length)) return setError('Every segment needs at least one step.');
    setBusy(true);
    try {
      await saveCrossAppFlow(id, { name: name.trim(), description, segments: cleaned });
      onSaved(); onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed');
    } finally { setBusy(false); }
  };

  const revert = async () => {
    if (!flow) return;
    setBusy(true); setError('');
    try { await deleteCrossAppFlow(flow.id); onSaved(); onClose(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Revert failed'); }
    finally { setBusy(false); }
  };

  const inputStyle: React.CSSProperties = {
    background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.12)',
    borderRadius: 6, color: 'inherit', padding: '6px 9px', font: 'inherit', width: '100%',
  };
  const iconBtn: React.CSSProperties = {
    background: 'transparent', border: 'none', color: 'inherit', opacity: 0.6,
    cursor: 'pointer', padding: 3, display: 'flex', alignItems: 'center',
  };

  return (
    <ModalPortal>
      <div style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 1000,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <div style={{
          background: 'var(--card-bg,#111827)', color: 'var(--text,#e5e7eb)',
          borderRadius: 12, width: 'min(860px, 94vw)', maxHeight: '90vh',
          display: 'flex', flexDirection: 'column', border: '1px solid rgba(255,255,255,0.08)',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 12, padding: '18px 22px',
            borderBottom: '1px solid rgba(255,255,255,0.08)',
          }}>
            <h2 style={{ margin: 0, fontSize: 18, flex: 1 }}>
              {isNew ? 'New cross-app flow' : `Edit — ${flow!.name}`}
            </h2>
            {flow?.edited && (
              <span style={{
                fontSize: 11, padding: '2px 8px', borderRadius: 999,
                background: 'rgba(251,191,36,0.15)', color: '#fbbf24',
              }}>edited</span>
            )}
            <button onClick={onClose} style={iconBtn}><X size={20} /></button>
          </div>

          <div style={{ padding: '18px 22px', overflowY: 'auto', flex: 1 }}>
            {error && (
              <div style={{
                background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)',
                borderRadius: 8, padding: 10, marginBottom: 14, fontSize: 13,
              }}>{error}</div>
            )}

            <div style={{ display: 'grid', gap: 10, marginBottom: 20 }}>
              {isNew && (
                <label style={{ fontSize: 13 }}>
                  <div style={{ opacity: 0.7, marginBottom: 4 }}>Flow id</div>
                  <input value={flowId} onChange={(e) => setFlowId(e.target.value)}
                         placeholder="my_checkout_flow" style={inputStyle} />
                </label>
              )}
              <label style={{ fontSize: 13 }}>
                <div style={{ opacity: 0.7, marginBottom: 4 }}>Name</div>
                <input value={name} onChange={(e) => setName(e.target.value)} style={inputStyle} />
              </label>
              <label style={{ fontSize: 13 }}>
                <div style={{ opacity: 0.7, marginBottom: 4 }}>Description</div>
                <textarea value={description} onChange={(e) => setDescription(e.target.value)}
                          rows={2} style={{ ...inputStyle, resize: 'vertical' }} />
              </label>
            </div>

            {segments.map((seg, si) => (
              <div key={si} style={{
                border: '1px solid rgba(255,255,255,0.10)', borderRadius: 10,
                padding: 14, marginBottom: 14,
              }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10 }}>
                  <span style={{
                    background: 'rgba(124,58,237,0.25)', borderRadius: 6,
                    padding: '2px 8px', fontSize: 12, fontWeight: 700,
                  }}>{si + 1}</span>
                  <select value={seg.role} onChange={(e) => patchSeg(si, { role: e.target.value })}
                          style={{ ...inputStyle, width: 120 }}>
                    {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                  <input value={seg.name} onChange={(e) => patchSeg(si, { name: e.target.value })}
                         placeholder="Segment name" style={{ ...inputStyle, flex: 1 }} />
                  <button onClick={() => moveSeg(si, -1)} disabled={si === 0} style={iconBtn}><ArrowUp size={16} /></button>
                  <button onClick={() => moveSeg(si, 1)} disabled={si === segments.length - 1} style={iconBtn}><ArrowDown size={16} /></button>
                  <button onClick={() => setSegments(segments.filter((_, k) => k !== si))}
                          disabled={segments.length === 1} style={iconBtn}><Trash2 size={16} /></button>
                </div>

                {seg.steps.map((step, ki) => (
                  <div key={ki} style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
                    <span style={{ opacity: 0.4, fontSize: 12, width: 20, textAlign: 'right' }}>{ki + 1}</span>
                    <input value={step} onChange={(e) => setStep(si, ki, e.target.value)}
                           placeholder="e.g. click saveBtn  ·  @kitchen_ready  ·  type roopa in firstName"
                           style={{ ...inputStyle, flex: 1, fontFamily: 'ui-monospace, monospace', fontSize: 12 }} />
                    <button title="Insert from catalog" style={iconBtn}
                            onClick={() => setShowPicker(showPicker === `${si}:${ki}` ? null : `${si}:${ki}`)}>
                      <ChevronDown size={16} />
                    </button>
                    <button onClick={() => moveStep(si, ki, -1)} disabled={ki === 0} style={iconBtn}><ArrowUp size={14} /></button>
                    <button onClick={() => moveStep(si, ki, 1)} disabled={ki === seg.steps.length - 1} style={iconBtn}><ArrowDown size={14} /></button>
                    <button onClick={() => removeStep(si, ki)} style={iconBtn}><Trash2 size={14} /></button>
                  </div>
                ))}

                {showPicker?.startsWith(`${si}:`) && (
                  <div style={{
                    background: 'rgba(0,0,0,0.35)', borderRadius: 8, padding: 10,
                    margin: '6px 0 8px 26px', maxHeight: 220, overflowY: 'auto',
                  }}>
                    {Object.entries(catalog).map(([group, entries]) => (
                      <div key={group} style={{ marginBottom: 8 }}>
                        <div style={{ opacity: 0.55, fontSize: 11, marginBottom: 4 }}>{group}</div>
                        {entries.map((e) => (
                          <button key={e.step}
                                  onClick={() => {
                                    const ki = Number(showPicker.split(':')[1]);
                                    addStep(si, ki, e.step);
                                    setShowPicker(null);
                                  }}
                                  style={{
                                    display: 'block', width: '100%', textAlign: 'left',
                                    background: 'transparent', border: 'none', color: 'inherit',
                                    cursor: 'pointer', padding: '3px 4px', fontSize: 12,
                                  }}>
                            <code style={{ color: '#a78bfa' }}>{e.step}</code>
                            <span style={{ opacity: 0.55 }}> — {e.help}</span>
                          </button>
                        ))}
                      </div>
                    ))}
                  </div>
                )}

                <button onClick={() => addStep(si)} style={{
                  ...iconBtn, opacity: 0.85, gap: 6, marginLeft: 26, fontSize: 12,
                }}><Plus size={14} /> Add step</button>
              </div>
            ))}

            <button
              onClick={() => setSegments([...segments, {
                num: String(segments.length + 1), name: '', role: 'waiter', steps: [''],
              }])}
              style={{
                background: 'rgba(255,255,255,0.06)', border: '1px dashed rgba(255,255,255,0.2)',
                borderRadius: 8, color: 'inherit', padding: '9px 14px', cursor: 'pointer',
                width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
              }}><Plus size={16} /> Add segment</button>
          </div>

          <div style={{
            display: 'flex', gap: 10, padding: '14px 22px',
            borderTop: '1px solid rgba(255,255,255,0.08)',
          }}>
            {flow?.edited && (
              <button onClick={revert} disabled={busy} style={{
                background: 'transparent', border: '1px solid rgba(255,255,255,0.18)',
                borderRadius: 8, color: 'inherit', padding: '9px 14px', cursor: 'pointer',
                display: 'flex', alignItems: 'center', gap: 7, fontSize: 13,
              }}>
                <RotateCcw size={15} /> {flow.builtin ? 'Revert to built-in' : 'Delete flow'}
              </button>
            )}
            <div style={{ flex: 1 }} />
            <button onClick={onClose} style={{
              background: 'transparent', border: '1px solid rgba(255,255,255,0.18)',
              borderRadius: 8, color: 'inherit', padding: '9px 16px', cursor: 'pointer',
            }}>Cancel</button>
            <button onClick={save} disabled={busy} style={{
              background: '#7c3aed', border: 'none', borderRadius: 8, color: '#fff',
              padding: '9px 18px', cursor: busy ? 'wait' : 'pointer', fontWeight: 600,
              display: 'flex', alignItems: 'center', gap: 7,
            }}>
              {busy ? <Loader2 size={15} className="spin" /> : <Save size={15} />} Save
            </button>
          </div>
        </div>
      </div>
    </ModalPortal>
  );
}
