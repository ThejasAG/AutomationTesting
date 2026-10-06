import { useEffect, useState } from 'react';
import { Wand2, Loader2, CheckCircle2, XCircle, ChevronDown } from 'lucide-react';
import { getDevices, runScenarioStream } from '../api';
import type { Device, ScenarioRunResult, ScenarioStepResult } from '../api';
import BundleIdSelect from './BundleIdSelect';

/** Plain-language scenario → drives the app live and records a script.
 *  Drop-in for the Scripts page, PR execution, and normal execution. */
export default function ScenarioPanel({
  projectId,
  defaultBundleId = '',
  onSaved,
}: {
  projectId: string;
  defaultBundleId?: string;
  onSaved?: (path: string, script: string) => void;
}) {
  const [steps, setSteps] = useState('');
  const [bundleId, setBundleId] = useState(defaultBundleId);
  const [name, setName] = useState('scenario');
  const [devices, setDevices] = useState<Device[]>([]);
  const [deviceId, setDeviceId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ScenarioRunResult | null>(null);

  // Live progress: what the runner is doing right now, and the steps it has
  // finished so far. Both fill in as the run streams.
  const [phase, setPhase] = useState('');
  const [current, setCurrent] = useState<string | null>(null);
  const [liveSteps, setLiveSteps] = useState<ScenarioStepResult[]>([]);

  useEffect(() => {
    getDevices().then(r => {
      setDevices(r.devices);
      if (r.devices.length) setDeviceId(r.devices[0].id);
    }).catch(() => {});
  }, []);

  const lines = steps.split('\n').map(s => s.trim()).filter(Boolean);
  const canRun = projectId && deviceId && bundleId.trim() && lines.length > 0 && !busy;

  const run = async () => {
    setBusy(true); setError(null); setResult(null);
    setLiveSteps([]); setCurrent(null); setPhase('Getting the app ready…');
    try {
      await runScenarioStream(
        {
          project_id: projectId, steps: lines, device_id: deviceId,
          bundle_id: bundleId.trim(), name: name.trim() || 'scenario', save: true,
        },
        ev => {
          switch (ev.type) {
            case 'phase':
              setPhase(ev.message);
              break;
            case 'step_start':
              setPhase(`Step ${ev.index + 1} of ${ev.total}`);
              setCurrent(ev.step);
              break;
            case 'step':
              setCurrent(null);
              setLiveSteps(prev => [...prev, {
                step: ev.step, ok: ev.ok, action: ev.action,
                detail: ev.detail, screenshot: ev.screenshot,
              }]);
              break;
            case 'done':
              setCurrent(null); setPhase('');
              setResult({
                ok: ev.ok, passed: ev.passed, total: ev.total,
                saved_to: ev.saved_to, script: ev.script, steps: [],
              });
              if (ev.saved_to && onSaved) onSaved(ev.saved_to, ev.script);
              break;
            case 'error':
              setCurrent(null); setPhase('');
              setError(ev.detail);
              break;
          }
        },
      );
    } catch (e: any) {
      setError(e?.message || 'Scenario run failed');
    } finally {
      setBusy(false); setCurrent(null); setPhase('');
    }
  };

  const input: React.CSSProperties = {
    width: '100%', boxSizing: 'border-box', background: 'rgba(255,255,255,0.05)',
    border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)',
    color: 'var(--text-primary)', padding: '9px 12px', fontSize: '0.9rem', fontFamily: 'inherit',
  };
  const label: React.CSSProperties = {
    display: 'block', fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase',
    letterSpacing: '0.08em', marginBottom: 5, fontWeight: 600,
  };

  return (
    <div className="card">
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
        <Wand2 size={18} color="var(--accent-primary)" />
        <h3 style={{ margin: 0, fontSize: '1.02rem' }}>Describe a scenario</h3>
      </div>
      <p style={{ color: 'var(--text-secondary)', fontSize: '0.82rem', marginTop: 0, marginBottom: 16 }}>
        Write plain steps — one per line. It drives the running app, records a real test, and saves it to <code>e2e/</code>.
      </p>

      <label style={label}>Steps</label>
      <textarea
        value={steps}
        onChange={e => setSteps(e.target.value)}
        rows={6}
        placeholder={'open the app\nopen Nylai Kitchen2\nselect a time slot\nbook a table\nverify the app is running'}
        style={{ ...input, resize: 'vertical', fontFamily: 'monospace', fontSize: '0.82rem', marginBottom: 12 }}
      />

      <div style={{ display: 'flex', gap: 12, marginBottom: 14, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 160 }}>
          <label style={label}>Device</label>
          <div style={{ position: 'relative' }}>
            <select value={deviceId} onChange={e => setDeviceId(e.target.value)} style={{ ...input, appearance: 'none', cursor: 'pointer' }}>
              {devices.length === 0 && <option value="">No devices online</option>}
              {devices.map(d => <option key={d.id} value={d.id}>{d.name || d.id}</option>)}
            </select>
            <ChevronDown size={13} style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
          </div>
        </div>
        <div style={{ flex: 1.4, minWidth: 200 }}>
          <label style={label}>App bundle id</label>
          <BundleIdSelect
            value={bundleId}
            onChange={setBundleId}
            style={input}
            projectId={projectId}
            placeholder="org.vyapy.sarls.vyaconsumer"
          />
        </div>
        <div style={{ flex: 0.8, minWidth: 120 }}>
          <label style={label}>Test name</label>
          <input value={name} onChange={e => setName(e.target.value)} placeholder="scenario" style={input} />
        </div>
      </div>

      <button
        className="btn"
        onClick={run}
        disabled={!canRun}
        style={{ padding: '9px 18px', fontSize: '0.88rem', opacity: canRun ? 1 : 0.5 }}
      >
        {busy ? <Loader2 size={14} style={{ animation: 'spin 1s linear infinite' }} /> : <Wand2 size={14} />}
        {busy ? 'Running…' : 'Run scenario'}
      </button>

      {error && (
        <div style={{ marginTop: 12, color: 'var(--danger)', fontSize: '0.82rem', display: 'flex', gap: 8, alignItems: 'center' }}>
          <XCircle size={14} /> {error}
        </div>
      )}

      {(busy || liveSteps.length > 0 || result) && (
        <div style={{ marginTop: 18 }}>
          {/* What is happening right now — setup phases land here before any
              step exists, so the panel is never silent. */}
          {busy && phase && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: 10 }}>
              <Loader2 size={12} style={{ animation: 'spin 1s linear infinite', flexShrink: 0 }} />
              {phase}
            </div>
          )}

          {result && (
            <div style={{ fontSize: '0.85rem', marginBottom: 10, color: result.ok ? 'var(--success)' : 'var(--warning)' }}>
              <strong>{result.passed}/{result.total} steps succeeded</strong>
              {result.saved_to && <span style={{ color: 'var(--text-muted)' }}> · saved to {result.saved_to}</span>}
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {liveSteps.map((s, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: '0.8rem' }}>
                {s.ok
                  ? <CheckCircle2 size={14} color="var(--success)" style={{ flexShrink: 0, marginTop: 2 }} />
                  : <XCircle size={14} color="var(--danger)" style={{ flexShrink: 0, marginTop: 2 }} />}
                <div>
                  <span style={{ fontFamily: 'monospace', color: 'var(--text-primary)' }}>{s.step}</span>
                  <span style={{ color: 'var(--text-muted)' }}> — {s.action}</span>
                  {!s.ok && s.detail && <div style={{ color: 'var(--danger)', fontSize: '0.75rem' }}>{s.detail}</div>}
                </div>
              </div>
            ))}

            {/* The step being performed on the device at this moment. */}
            {current && (
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: '0.8rem' }}>
                <Loader2 size={14} color="var(--accent-primary)" style={{ flexShrink: 0, marginTop: 2, animation: 'spin 1s linear infinite' }} />
                <div>
                  <span style={{ fontFamily: 'monospace', color: 'var(--text-primary)' }}>{current}</span>
                  <span style={{ color: 'var(--text-muted)' }}> — running on the device…</span>
                </div>
              </div>
            )}
          </div>

          {result && (
            <details style={{ marginTop: 14 }}>
              <summary style={{ cursor: 'pointer', fontSize: '0.8rem', color: 'var(--accent-primary)' }}>View generated script</summary>
              <pre style={{ marginTop: 8, background: 'rgba(0,0,0,0.35)', padding: 12, borderRadius: 8, overflowX: 'auto', fontSize: '0.74rem', color: 'var(--text-secondary)' }}>{result.script}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
