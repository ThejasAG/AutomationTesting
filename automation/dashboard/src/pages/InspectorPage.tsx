/** Live UI inspector — the screen next to its accessibility tree, with the elements
 *  that cannot be tapped called out.
 *
 *  Exists because three separate bugs on 2026-09-02 all looked like "the automation
 *  can't tap it" and were all "something is drawn over it" — found only by dumping
 *  frames and comparing rectangles by hand. */
import { useEffect, useState } from 'react';
import { AlertTriangle, Crosshair, Loader2, RefreshCw } from 'lucide-react';
import {
    getInspectorScreenshot, getInspectorTree, getScenarioDevices,
    type InspectorElement, type InspectorTree, type SimDevice,
} from '../api';

export default function InspectorPage() {
    const [sims, setSims] = useState<SimDevice[]>([]);
    const [udid, setUdid] = useState('');
    const [tree, setTree] = useState<InspectorTree | null>(null);
    const [shot, setShot] = useState<{ image: string; width: number; height: number } | null>(null);
    const [sel, setSel] = useState<InspectorElement | null>(null);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState('');

    useEffect(() => {
        getScenarioDevices()
            .then(d => {
                const booted = d.simulators.filter(s => /booted/i.test(s.state));
                setSims(booted);
                if (booted[0]) setUdid(booted[0].udid);
            })
            .catch(() => setErr('Could not list simulators'));
    }, []);

    async function load() {
        if (!udid) return;
        setBusy(true); setErr(''); setSel(null);
        try {
            // Both describe the same instant; fetching them together keeps the
            // overlay honest if the screen changes mid-inspection.
            const [t, s] = await Promise.all([
                getInspectorTree(udid), getInspectorScreenshot(udid),
            ]);
            setTree(t); setShot(s);
        } catch (e: any) {
            setErr(e?.message || 'Could not read the device');
            setTree(null); setShot(null);
        } finally { setBusy(false); }
    }

    // The screenshot is in PIXELS, frames are in POINTS, and on a landscape device
    // the two axes are swapped. Scale per axis rather than assuming a 2x factor.
    const VIEW_W = 380;
    const scale = shot && tree?.screen.width ? VIEW_W / tree.screen.width : 1;
    const viewH = tree ? tree.screen.height * scale : 0;

    const problemLabels = new Set((tree?.problems || []).map(p => p.label));

    return (
        <div style={{ padding: 24 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
                <Crosshair size={20} color="var(--accent-primary)" />
                <h1 style={{ margin: 0, fontSize: '1.3rem' }}>UI Inspector</h1>
                <select value={udid} onChange={e => setUdid(e.target.value)}
                        style={{ padding: '7px 10px', borderRadius: 8, background: 'transparent',
                                 color: 'var(--text-primary)', border: '1px solid var(--border-color)' }}>
                    {sims.length === 0 && <option value="">no booted simulator</option>}
                    {sims.map(s => <option key={s.udid} value={s.udid}>{s.name}</option>)}
                </select>
                <button className="btn" onClick={load} disabled={!udid || busy}
                        style={{ display: 'inline-flex', alignItems: 'center', gap: 7 }}>
                    {busy ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />} Inspect
                </button>
                {tree && (
                    <span style={{ color: 'var(--text-secondary)', fontSize: '0.82rem' }}>
                        {tree.screen.width}×{tree.screen.height} {tree.screen.orientation} ·{' '}
                        {tree.element_count} elements ·{' '}
                        <strong style={{ color: tree.problem_count ? 'var(--danger)' : 'var(--success)' }}>
                            {tree.problem_count} unreachable
                        </strong>
                    </span>
                )}
            </div>

            {err && <div style={{ color: 'var(--danger)', marginBottom: 12 }}>{err}</div>}

            <div style={{ display: 'grid', gridTemplateColumns: `${VIEW_W}px 1fr`, gap: 20, alignItems: 'start' }}>
                {/* Screen, with every element frame drawn over it. */}
                <div style={{ position: 'relative', width: VIEW_W, height: viewH || 200,
                              border: '1px solid var(--border-color)', borderRadius: 10,
                              overflow: 'hidden', background: '#000' }}>
                    {shot && <img src={shot.image} alt="device screen"
                                  style={{ width: '100%', height: '100%', objectFit: 'fill' }} />}
                    {(tree?.elements || []).map((e, i) => {
                        const bad = problemLabels.has(e.label) && e.label;
                        const on = sel && sel.label === e.label
                            && sel.frame.x === e.frame.x && sel.frame.y === e.frame.y;
                        return (
                            <div key={i} title={e.label || e.type || ''}
                                 onClick={() => setSel(e)}
                                 style={{
                                     position: 'absolute', cursor: 'pointer',
                                     left: e.frame.x * scale, top: e.frame.y * scale,
                                     width: Math.max(2, e.frame.w * scale),
                                     height: Math.max(2, e.frame.h * scale),
                                     border: `1px solid ${on ? '#22d3ee' : bad ? 'rgba(239,68,68,.9)' : 'rgba(99,102,241,.35)'}`,
                                     background: on ? 'rgba(34,211,238,.22)' : bad ? 'rgba(239,68,68,.12)' : 'transparent',
                                 }} />
                        );
                    })}
                </div>

                <div>
                    {sel && (
                        <div style={{ marginBottom: 14, padding: 12, borderRadius: 10,
                                      border: '1px solid var(--border-color)' }}>
                            <div style={{ fontWeight: 600 }}>{sel.label || <em>(no label)</em>}</div>
                            <div style={{ fontFamily: 'monospace', fontSize: '0.78rem',
                                          color: 'var(--text-secondary)', marginTop: 4 }}>
                                {sel.type} · x {sel.frame.x} y {sel.frame.y} · {sel.frame.w}×{sel.frame.h}
                                {' · tap point '}({sel.centre.x}, {sel.centre.y})
                            </div>
                        </div>
                    )}

                    <h2 style={{ fontSize: '0.95rem', margin: '0 0 8px',
                                 display: 'flex', alignItems: 'center', gap: 7 }}>
                        <AlertTriangle size={15} color="var(--danger)" /> Cannot be tapped as drawn
                    </h2>
                    {tree && tree.problem_count === 0 && (
                        <div style={{ color: 'var(--success)', fontSize: '0.85rem' }}>
                            Nothing is covered or out of reach on this screen.
                        </div>
                    )}
                    <div style={{ maxHeight: 460, overflowY: 'auto' }}>
                        {(tree?.problems || []).map((p, i) => (
                            <div key={i} style={{ padding: '8px 10px', borderRadius: 8, marginBottom: 6,
                                                  border: '1px solid var(--border-color)' }}>
                                <div style={{ fontFamily: 'monospace', fontSize: '0.82rem' }}>
                                    {p.label || <em>(no label)</em>}
                                </div>
                                {p.issues.map((is, j) => (
                                    <div key={j} style={{ fontSize: '0.76rem', color: 'var(--text-secondary)', marginTop: 3 }}>
                                        <span style={{ color: is.kind === 'covered' ? '#fbbf24' : 'var(--danger)' }}>
                                            {is.kind}
                                        </span>{is.by ? ` by "${is.by}"` : ''} — {is.detail}
                                    </div>
                                ))}
                            </div>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    );
}
