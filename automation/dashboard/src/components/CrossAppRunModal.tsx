import { useEffect, useState } from 'react';
import { X, Play, Loader2, Smartphone, Users, Info, GitBranch, ChevronDown, ChevronRight, Pencil, Plus } from 'lucide-react';
import FlowEditorModal from './FlowEditorModal';
import { getCrossAppConfig, runCrossAppSuite, listCrossAppFlows, runCrossAppFlow, runAllCrossAppFlows } from '../api';
import type { CrossAppConfig, CrossAppFlow, FlowEnv, BusinessDevice } from '../api';
import ModalPortal from './ModalPortal';

type Role = 'consumer' | 'waiter' | 'kitchen';
const ROLES: { key: Role; label: string; hint: string }[] = [
  { key: 'consumer', label: 'Consumer (diner)', hint: 'iPhone — books, orders, pays' },
  { key: 'waiter', label: 'Waiter', hint: 'Business app — accepts the reservation' },
  { key: 'kitchen', label: 'Kitchen', hint: 'Business app — accepts & serves the order' },
];

/** Pre-run dialog for the cross-app suite: assign each role to a simulator and
 *  set/save the logins. Waiter and kitchen on the SAME device → the run switches
 *  accounts on that device; on DIFFERENT devices → two Business instances. */
export default function CrossAppRunModal({ onClose, onStarted }: {
  onClose: () => void;
  onStarted: (runId: string) => void;
}) {
  const [cfg, setCfg] = useState<CrossAppConfig | null>(null);
  const [devices, setDevices] = useState<Record<Role, string>>({ consumer: '', waiter: '', kitchen: '' });
  const [emails, setEmails] = useState<Record<Role, string>>({ consumer: '', waiter: '', kitchen: '' });
  const [passwords, setPasswords] = useState<Record<Role, string>>({ consumer: '', waiter: '', kitchen: '' });
  const [hasPw, setHasPw] = useState<Record<Role, boolean>>({ consumer: false, waiter: false, kitchen: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [flows, setFlows] = useState<CrossAppFlow[]>([]);
  // null = closed; {flow: null} = create a new flow; {flow: f} = edit f
  const [editing, setEditing] = useState<{ flow: CrossAppFlow | null } | null>(null);
  const [flowBusy, setFlowBusy] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  // Persisted under 'flowEnv' so it also governs single scenario runs (api.ts sends it
  // as `env`, and the backend maps the bundle via bundle_for_env). One switch, both paths.
  const [env, setEnv] = useState<FlowEnv>(
    () => (localStorage.getItem('flowEnv') as FlowEnv) || 'staging');
  const [bizDevice, setBizDevice] = useState<BusinessDevice>('tablet');

  useEffect(() => {
    getCrossAppConfig()
      .then(c => {
        setCfg(c);
        setDevices(c.devices);
        setEmails({ consumer: c.credentials.consumer.email, waiter: c.credentials.waiter.email, kitchen: c.credentials.kitchen.email });
        setHasPw({ consumer: c.credentials.consumer.has_password, waiter: c.credentials.waiter.has_password, kitchen: c.credentials.kitchen.has_password });
      })
      .catch(e => setError(e?.message || 'Could not load config'));
    reloadFlows();
  }, []);

  const reloadFlows = () => { listCrossAppFlows().then(r => setFlows(r.flows)).catch(() => {}); };

  const startFlow = async (id: string) => {
    setFlowBusy(id); setError(null);
    try {
      const r = await runCrossAppFlow(id, env, bizDevice);
      onStarted(r.run_id);
    } catch (e: any) {
      setError(e?.message || 'Could not start the flow');
    } finally {
      // Always clear busy — otherwise EVERY Run button stays disabled (disabled={!!flowBusy})
      // after a start, so the modal's Run buttons become unclickable until it's reopened.
      setFlowBusy(null);
    }
  };
  const startAll = async () => {
    setFlowBusy('__all__'); setError(null);
    try {
      await runAllCrossAppFlows(env);
      onClose();   // runs stream into the dashboard; close the dialog
    } catch (e: any) {
      setError(e?.message || 'Could not start all flows');
      setFlowBusy(null);
    }
  };
  const roleColor = (role: string) =>
    role === 'consumer' ? '#3182ce' : role === 'kitchen' ? '#dd6b20' : '#805ad5';

  const sameWK = devices.waiter && devices.waiter === devices.kitchen;

  const start = async () => {
    setBusy(true); setError(null);
    try {
      const credentials: Record<string, { email?: string; password?: string }> = {};
      (['consumer', 'waiter', 'kitchen'] as Role[]).forEach(r => {
        credentials[r] = { email: emails[r], password: passwords[r] || undefined };
      });
      const r = await runCrossAppSuite({ devices, credentials, save: true });
      onStarted(r.run_id);
    } catch (e: any) {
      setError(e?.message || 'Could not start the run');
      setBusy(false);
    }
  };

  const inputStyle: React.CSSProperties = {
    width: '100%', boxSizing: 'border-box', background: 'rgba(255,255,255,0.05)',
    border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)',
    color: 'var(--text-primary)', padding: '8px 10px', fontSize: '0.85rem', fontFamily: 'inherit',
  };
  const label: React.CSSProperties = {
    display: 'block', fontSize: '0.68rem', color: 'var(--text-muted)', textTransform: 'uppercase',
    letterSpacing: '0.06em', marginBottom: 4, fontWeight: 600,
  };

  return (
    <ModalPortal onClose={onClose}>
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)', backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 999, padding: 16 }} onClick={onClose}>
      <div className="modal-pop" style={{ width: 620, maxWidth: '94vw', maxHeight: '90vh', overflowY: 'auto', padding: 28, position: 'relative', background: 'var(--bg-card, #16172a)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-lg, 16px)', boxShadow: '0 24px 60px rgba(0,0,0,0.55)' }} onClick={e => e.stopPropagation()}>
        <button onClick={onClose} style={{ position: 'absolute', top: 16, right: 16, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex' }}><X size={18} /></button>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
          <Users size={20} color="var(--accent-primary)" />
          <h3 style={{ margin: 0 }}>Run Cross-App Suite</h3>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.84rem', marginTop: 0, marginBottom: 18 }}>
          Assign each role to a simulator and set the logins. Waiter and kitchen on the <strong>same</strong> device
          switch accounts on that device; on <strong>different</strong> devices they run as two Business instances.
        </p>

        {flows.length > 0 && (
          <div style={{ marginBottom: 20 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
              <GitBranch size={16} color="var(--accent-primary)" />
              <strong style={{ fontSize: '0.9rem' }}>Major flows</strong>
              {/* Environment selector: New Staging (STG-* apps) vs Old Vya (prod) */}
              <div style={{ display: 'inline-flex', border: '1px solid var(--border-color)', borderRadius: 20, overflow: 'hidden', marginLeft: 4 }}>
                {(['staging', 'prod'] as FlowEnv[]).map(e => (
                  <button key={e} onClick={() => { setEnv(e); localStorage.setItem('flowEnv', e); }}
                    style={{
                      padding: '4px 12px', fontSize: '0.72rem', fontWeight: 600, cursor: 'pointer', border: 'none',
                      background: env === e ? 'var(--accent-primary)' : 'transparent',
                      color: env === e ? '#fff' : 'var(--text-secondary)',
                    }}>
                    {e === 'staging' ? 'New Staging' : 'Old Vya'}
                  </button>
                ))}
              </div>
              {/* B-app device: run the waiter + kitchen roles on the iPad (tablet) or iPhone (phone) */}
              <div title="Which device runs the B-app (waiter + kitchen)" style={{ display: 'inline-flex', border: '1px solid var(--border-color)', borderRadius: 20, overflow: 'hidden', marginLeft: 4 }}>
                {(['tablet', 'phone'] as BusinessDevice[]).map(dv => (
                  <button key={dv} onClick={() => setBizDevice(dv)}
                    style={{
                      padding: '4px 12px', fontSize: '0.72rem', fontWeight: 600, cursor: 'pointer', border: 'none',
                      background: bizDevice === dv ? 'var(--accent-primary)' : 'transparent',
                      color: bizDevice === dv ? '#fff' : 'var(--text-secondary)',
                    }}>
                    {dv === 'tablet' ? '🖥️ Tablet' : '📱 Phone'}
                  </button>
                ))}
              </div>
              <button className="btn" onClick={() => setEditing({ flow: null })}
                title="Build a new cross-app flow from scratch"
                style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6, padding: '5px 12px', fontSize: '0.76rem' }}>
                <Plus size={13} /> New flow
              </button>
              <button className="btn" onClick={startAll} disabled={!!flowBusy}
                style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '5px 12px', fontSize: '0.76rem' }}>
                {flowBusy === '__all__' ? <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Play size={13} />} Run all
              </button>
            </div>
            <div style={{ color: 'var(--text-muted)', fontSize: '0.72rem', marginBottom: 10 }}>
              Target: <strong style={{ color: 'var(--text-secondary)' }}>{env === 'staging' ? 'STG-VyaConsumer + STG-VyaBusiness (staging)' : 'Vya Consumer + Vya Business (prod)'}</strong>
              {' · '}B-app on <strong style={{ color: 'var(--text-secondary)' }}>{bizDevice === 'tablet' ? '🖥️ iPad' : '📱 iPhone 16'}</strong>
            </div>
            {flows.map(f => (
              <div key={f.id} style={{ border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: '10px 12px', marginBottom: 8 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <button onClick={() => setExpanded(expanded === f.id ? null : f.id)}
                    style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex', padding: 0 }}>
                    {expanded === f.id ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                  </button>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: '0.86rem', fontWeight: 600 }}>{f.name}</div>
                    <div style={{ display: 'flex', gap: 4, marginTop: 5, flexWrap: 'wrap' }}>
                      {f.segments.map(s => (
                        <span key={s.num} title={s.name}
                          style={{ fontSize: '0.62rem', padding: '2px 7px', borderRadius: 20, color: '#fff', background: roleColor(s.role) }}>
                          {s.num}. {s.role}
                        </span>
                      ))}
                    </div>
                  </div>
                  {f.edited && (
                    <span title="A saved edit is overriding the built-in definition"
                      style={{ fontSize: '0.62rem', padding: '2px 7px', borderRadius: 20, background: 'rgba(251,191,36,0.15)', color: '#fbbf24' }}>
                      edited
                    </span>
                  )}
                  <button className="btn" onClick={() => setEditing({ flow: f })} title="Edit this flow's segments and steps"
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 12px', fontSize: '0.8rem' }}>
                    <Pencil size={13} /> Edit
                  </button>
                  <button className="btn" onClick={() => startFlow(f.id)} disabled={!!flowBusy}
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 14px', fontSize: '0.8rem' }}>
                    {flowBusy === f.id ? <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} /> : <Play size={13} />} Run
                  </button>
                </div>
                {expanded === f.id && (
                  <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--border-color)' }}>
                    <div style={{ color: 'var(--text-secondary)', fontSize: '0.78rem', marginBottom: 10 }}>{f.description}</div>
                    {f.segments.map(s => (
                      <div key={s.num} style={{ marginBottom: 8 }}>
                        <div style={{ fontSize: '0.76rem', fontWeight: 600, color: roleColor(s.role) }}>{s.num}. {s.name}</div>
                        <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', fontFamily: 'monospace', lineHeight: 1.6, marginTop: 2 }}>
                          {s.steps.join(' → ')}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {!cfg ? (
          <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-secondary)' }}>
            <Loader2 size={18} style={{ animation: 'spin 1s linear infinite' }} /> Loading simulators…
          </div>
        ) : (
          <>
            {ROLES.map(({ key, label: rl, hint }) => (
              <div key={key} style={{ border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: 14, marginBottom: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
                  <Smartphone size={15} color="var(--accent-primary)" />
                  <strong style={{ fontSize: '0.9rem' }}>{rl}</strong>
                  <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>· {hint}</span>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 10 }}>
                  <div>
                    <label style={label}>Simulator</label>
                    <select value={devices[key]} onChange={e => setDevices({ ...devices, [key]: e.target.value })} style={{ ...inputStyle, cursor: 'pointer' }}>
                      {cfg.simulators.map(s => (
                        <option key={s.udid} value={s.udid}>{s.name} {s.state === 'Booted' ? '● booted' : ''} — iOS {s.ios}</option>
                      ))}
                    </select>
                  </div>
                  <div style={{ display: 'flex', gap: 10 }}>
                    <div style={{ flex: 1 }}>
                      <label style={label}>Email</label>
                      <input value={emails[key]} onChange={e => setEmails({ ...emails, [key]: e.target.value })} placeholder="email@…" style={inputStyle} />
                    </div>
                    <div style={{ flex: 1 }}>
                      <label style={label}>Password {hasPw[key] && !passwords[key] ? '(saved)' : ''}</label>
                      <input type="password" value={passwords[key]} onChange={e => setPasswords({ ...passwords, [key]: e.target.value })} placeholder={hasPw[key] ? '•••••• (leave blank to keep)' : 'password'} style={inputStyle} />
                    </div>
                  </div>
                </div>
              </div>
            ))}

            <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-muted)', fontSize: '0.78rem', margin: '4px 0 16px' }}>
              <Info size={13} />
              {sameWK ? 'Waiter + kitchen share one device (accounts switch).' : 'Waiter + kitchen on separate devices.'} Logins are saved for next time (passwords stay local, never committed).
            </div>

            {error && <p style={{ color: 'var(--danger)', fontSize: '0.82rem', marginBottom: 12 }}>{error}</p>}

            <button className="btn" onClick={start} disabled={busy} style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '10px 20px' }}>
              {busy ? <Loader2 size={15} style={{ animation: 'spin 1s linear infinite' }} /> : <Play size={15} />}
              {busy ? 'Starting…' : 'Run on selected simulators'}
            </button>
          </>
        )}
      </div>
    </div>
    {editing && (
      <FlowEditorModal
        flow={editing.flow}
        onClose={() => setEditing(null)}
        onSaved={reloadFlows}
      />
    )}
    </ModalPortal>
  );
}
