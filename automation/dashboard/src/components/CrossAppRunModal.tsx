import { useEffect, useState } from 'react';
import { X, Play, Loader2, Smartphone, Users, Info } from 'lucide-react';
import { getCrossAppConfig, runCrossAppSuite } from '../api';
import type { CrossAppConfig } from '../api';
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

  useEffect(() => {
    getCrossAppConfig()
      .then(c => {
        setCfg(c);
        setDevices(c.devices);
        setEmails({ consumer: c.credentials.consumer.email, waiter: c.credentials.waiter.email, kitchen: c.credentials.kitchen.email });
        setHasPw({ consumer: c.credentials.consumer.has_password, waiter: c.credentials.waiter.has_password, kitchen: c.credentials.kitchen.has_password });
      })
      .catch(e => setError(e?.message || 'Could not load config'));
  }, []);

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
    </ModalPortal>
  );
}
