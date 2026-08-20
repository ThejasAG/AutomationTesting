import { useEffect, useState } from 'react';
import { Settings, User, Shield, ScrollText, LogOut, Loader2 } from 'lucide-react';
import { getMe, getAuditLogs, setAuthToken, API_BASE } from '../api';
import { parseServerDate } from '../time';

interface Me { id: string; username: string; role: string; }
interface AuditLog {
  id: string;
  timestamp: string;
  user_id?: string;
  action: string;
  resource_type?: string;
  resource_id?: string;
}

export default function SettingsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [loadingLogs, setLoadingLogs] = useState(true);

  useEffect(() => {
    getMe().then(setMe).catch(console.error);
    getAuditLogs()
      .then(data => setLogs(data.logs || []))
      .catch(console.error)
      .finally(() => setLoadingLogs(false));
  }, []);

  const logout = () => {
    setAuthToken(null);
    window.location.href = '/login';
  };

  return (
    <div className="animate-fade-in">
      <header className="page-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <Settings size={28} color="var(--accent-primary)" />
          <div>
            <h1 className="page-title" style={{ margin: 0 }}>Settings</h1>
            <p className="page-subtitle" style={{ marginBottom: 0 }}>
              Account, access and the platform audit trail.
            </p>
          </div>
        </div>
      </header>

      {/* Account */}
      <div className="card" style={{ marginBottom: '24px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '18px' }}>
          <User size={18} color="var(--accent-primary)" />
          <h2 style={{ fontSize: '1.05rem', margin: 0 }}>Account</h2>
        </div>
        {me ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: '32px', flexWrap: 'wrap' }}>
            <Field label="Username" value={me.username} />
            <Field label="Role" value={
              <span style={{
                display: 'inline-flex', alignItems: 'center', gap: '6px',
                color: me.role === 'admin' ? 'var(--warning)' : 'var(--text-primary)',
              }}>
                {me.role === 'admin' && <Shield size={13} />}{me.role}
              </span>
            } />
            <Field label="Connected to" value={<code style={{ color: 'var(--text-secondary)' }}>{API_BASE}</code>} />
            <button
              onClick={logout}
              style={{
                marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '8px',
                background: 'transparent', border: '1px solid var(--border-color)',
                borderRadius: 'var(--radius-sm)', color: 'var(--danger)',
                padding: '8px 16px', cursor: 'pointer', fontSize: '0.85rem', fontFamily: 'inherit',
              }}
            >
              <LogOut size={14} /> Sign out
            </button>
          </div>
        ) : (
          <p style={{ color: 'var(--text-muted)' }}>Loading account…</p>
        )}
      </div>

      {/* Audit log */}
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '18px' }}>
          <ScrollText size={18} color="var(--accent-primary)" />
          <h2 style={{ fontSize: '1.05rem', margin: 0 }}>Audit Log</h2>
          <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
            Every action taken on the platform.
          </span>
        </div>

        {loadingLogs ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', padding: '16px 0' }}>
            <Loader2 size={15} style={{ animation: 'sp 1s linear infinite' }} /> Loading…
          </div>
        ) : logs.length === 0 ? (
          <p style={{ color: 'var(--text-muted)', padding: '8px 0' }}>No audit entries yet.</p>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
              <thead>
                <tr style={{ textAlign: 'left', color: 'var(--text-muted)', borderBottom: '1px solid var(--border-color)' }}>
                  <th style={th}>Time</th>
                  <th style={th}>User</th>
                  <th style={th}>Action</th>
                  <th style={th}>Resource</th>
                </tr>
              </thead>
              <tbody>
                {logs.map(l => (
                  <tr key={l.id} style={{ borderBottom: '1px solid var(--border-color)' }}>
                    <td style={{ ...td, color: 'var(--text-muted)' }}>{parseServerDate(l.timestamp).toLocaleString()}</td>
                    <td style={{ ...td, fontFamily: "'Fira Code', monospace", color: 'var(--accent-primary)' }}>{l.user_id || 'system'}</td>
                    <td style={{ ...td, color: 'var(--text-primary)' }}>{l.action}</td>
                    <td style={{ ...td, color: 'var(--text-secondary)' }}>{[l.resource_type, l.resource_id].filter(Boolean).join(' ') || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <style>{`@keyframes sp { from { transform: rotate(0) } to { transform: rotate(360deg) } }`}</style>
    </div>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '4px' }}>{label}</div>
      <div style={{ fontSize: '0.95rem', color: 'var(--text-primary)' }}>{value}</div>
    </div>
  );
}

const th: React.CSSProperties = { padding: '8px 12px', fontWeight: 600, whiteSpace: 'nowrap' };
const td: React.CSSProperties = { padding: '9px 12px', whiteSpace: 'nowrap' };
