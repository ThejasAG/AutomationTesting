import { useEffect, useState } from 'react';
import { Plug, Plus, Trash2, Pencil, Loader2, CheckCircle2, AlertTriangle, Smartphone } from 'lucide-react';
import {
  getMcpServers, getMcpPresets, saveMcpServer, deleteMcpServer, testMcpServer,
} from '../api';
import type { McpServer, McpServerInput, McpTestResult } from '../api';

const EMPTY: McpServerInput = {
  name: '', transport: 'stdio', command: '', args: [], env: {}, url: '', headers: {},
  enabled: true, drives_devices: false, description: '',
};

// "KEY=value" lines <-> {KEY: value}. Masked values (••••••) round-trip as-is,
// and the backend keeps the stored secret for them.
const toLines = (o: Record<string, string>) => Object.entries(o).map(([k, v]) => `${k}=${v}`).join('\n');
const fromLines = (s: string) => Object.fromEntries(
  s.split('\n').map(l => l.trim()).filter(l => l.includes('='))
    .map(l => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]));

const input: React.CSSProperties = {
  width: '100%', boxSizing: 'border-box', padding: 8, borderRadius: 6, fontSize: '0.82rem',
  background: 'var(--bg-base, #0d1117)', color: 'var(--text-primary)',
  border: '1px solid rgba(255,255,255,0.1)', fontFamily: 'inherit',
};
const btn = (primary = false): React.CSSProperties => ({
  display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 12px', borderRadius: 6,
  fontSize: '0.8rem', cursor: 'pointer', fontFamily: 'inherit',
  border: primary ? 'none' : '1px solid rgba(255,255,255,0.12)',
  background: primary ? 'var(--accent-primary)' : 'transparent',
  color: primary ? '#fff' : 'var(--text-primary)',
});

/** Settings → MCP servers. The AI Chat can call the tools of every enabled server. */
export default function McpServersCard({ isAdmin }: { isAdmin: boolean }) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [presets, setPresets] = useState<McpServerInput[]>([]);
  const [editing, setEditing] = useState<{ id?: string; form: McpServerInput } | null>(null);
  const [envText, setEnvText] = useState('');
  const [headerText, setHeaderText] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, McpTestResult>>({});
  const [error, setError] = useState<string | null>(null);

  const load = () => getMcpServers().then(setServers).catch(e => setError(e.message));
  useEffect(() => {
    load();
    getMcpPresets().then(setPresets).catch(() => {});
  }, []);

  const open = (form: McpServerInput, id?: string) => {
    setEditing({ id, form: { ...form } });
    setEnvText(toLines(form.env));
    setHeaderText(toLines(form.headers));
    setError(null);
  };
  const set = (patch: Partial<McpServerInput>) =>
    setEditing(e => (e ? { ...e, form: { ...e.form, ...patch } } : e));

  const save = async () => {
    if (!editing) return;
    setBusy('save'); setError(null);
    try {
      const saved = await saveMcpServer(
        { ...editing.form, env: fromLines(envText), headers: fromLines(headerText) }, editing.id);
      setEditing(null);
      await load();
      await test(saved.id);               // show straight away whether it works
    } catch (e: any) {
      setError(e.message || 'Could not save the server.');
    } finally {
      setBusy(null);
    }
  };

  const test = async (id: string) => {
    setBusy(id);
    try {
      const r = await testMcpServer(id);
      setResults(prev => ({ ...prev, [id]: r }));
    } catch (e: any) {
      setResults(prev => ({ ...prev, [id]: { ok: false, error: e.message, tools: [] } }));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (s: McpServer) => {
    if (!confirm(`Remove the MCP server "${s.name}"? The AI Chat will no longer use its tools.`)) return;
    setBusy(s.id);
    try { await deleteMcpServer(s.id); await load(); }
    catch (e: any) { setError(e.message); }
    finally { setBusy(null); }
  };

  const unusedPresets = presets.filter(p => !servers.some(s => s.name === p.name));

  return (
    <div className="card" style={{ marginBottom: '24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: 6 }}>
        <Plug size={18} color="var(--accent-primary)" />
        <h2 style={{ fontSize: '1.05rem', margin: 0 }}>MCP servers</h2>
        <div style={{ flex: 1 }} />
        {isAdmin && !editing && (
          <>
            {unusedPresets.map(p => (
              <button key={p.name} style={btn()} onClick={() => open(p)}>
                <Plus size={14} /> {p.name}
              </button>
            ))}
            <button style={btn(true)} onClick={() => open(EMPTY)}><Plus size={14} /> Add server</button>
          </>
        )}
      </div>
      <p style={{ margin: '0 0 16px', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
        The AI Chat can call the tools of every enabled server — e.g. "take a screenshot of the
        iPad" or "run this Maestro flow". Servers that drive the simulators are refused while a
        test run is using them.{!isAdmin && ' Only an admin can add or change servers.'}
      </p>

      {error && (
        <div style={{ color: 'var(--danger)', fontSize: '0.8rem', marginBottom: 12, display: 'flex', gap: 6 }}>
          <AlertTriangle size={14} /> {error}
        </div>
      )}

      {editing && (
        <div style={{ border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, padding: 14, marginBottom: 16,
          display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <label style={{ fontSize: '0.75rem' }}>Name
            <input style={input} value={editing.form.name} onChange={e => set({ name: e.target.value })} />
          </label>
          <label style={{ fontSize: '0.75rem' }}>Connection
            <select style={input} value={editing.form.transport}
              onChange={e => set({ transport: e.target.value as 'stdio' | 'http' })}>
              <option value="stdio">Local command (stdio)</option>
              <option value="http">Remote URL (HTTP)</option>
            </select>
          </label>
          {editing.form.transport === 'stdio' ? (
            <>
              <label style={{ fontSize: '0.75rem' }}>Command
                <input style={input} value={editing.form.command} placeholder="npx"
                  onChange={e => set({ command: e.target.value })} />
              </label>
              <label style={{ fontSize: '0.75rem' }}>Arguments (one per line)
                <textarea style={{ ...input, minHeight: 54 }} value={editing.form.args.join('\n')}
                  placeholder={'-y\nappium-mcp@1.95.1'}
                  onChange={e => set({ args: e.target.value.split('\n').filter(a => a.trim()) })} />
              </label>
              <label style={{ fontSize: '0.75rem', gridColumn: '1 / -1' }}>Environment (KEY=value per line)
                <textarea style={{ ...input, minHeight: 54, fontFamily: 'monospace' }} value={envText}
                  onChange={e => setEnvText(e.target.value)} />
              </label>
            </>
          ) : (
            <>
              <label style={{ fontSize: '0.75rem', gridColumn: '1 / -1' }}>URL
                <input style={input} value={editing.form.url} placeholder="https://example.com/mcp"
                  onChange={e => set({ url: e.target.value })} />
              </label>
              <label style={{ fontSize: '0.75rem', gridColumn: '1 / -1' }}>Headers (Name=value per line)
                <textarea style={{ ...input, minHeight: 54, fontFamily: 'monospace' }} value={headerText}
                  placeholder="Authorization=Bearer …" onChange={e => setHeaderText(e.target.value)} />
              </label>
            </>
          )}
          <label style={{ fontSize: '0.75rem', gridColumn: '1 / -1' }}>Description
            <input style={input} value={editing.form.description}
              onChange={e => set({ description: e.target.value })} />
          </label>
          <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 18, alignItems: 'center', fontSize: '0.8rem' }}>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <input type="checkbox" checked={editing.form.enabled} onChange={e => set({ enabled: e.target.checked })} />
              Enabled for the AI Chat
            </label>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <input type="checkbox" checked={editing.form.drives_devices}
                onChange={e => set({ drives_devices: e.target.checked })} />
              Drives the simulators (refuse during test runs)
            </label>
            <div style={{ flex: 1 }} />
            <button style={btn()} onClick={() => setEditing(null)}>Cancel</button>
            <button style={btn(true)} onClick={save} disabled={busy === 'save'}>
              {busy === 'save' ? <Loader2 size={14} className="spin" /> : null} Save &amp; test
            </button>
          </div>
        </div>
      )}

      {servers.length === 0 && !editing ? (
        <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>
          No MCP servers yet.{isAdmin && ' Add Appium or Maestro with one click above.'}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {servers.map(s => {
            const r = results[s.id];
            return (
              <div key={s.id} style={{ border: '1px solid rgba(255,255,255,0.08)', borderRadius: 8, padding: '10px 12px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                  <strong style={{ fontSize: '0.88rem' }}>{s.name}</strong>
                  <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                    {s.transport === 'http' ? s.url : [s.command, ...s.args].join(' ')}
                  </span>
                  {!s.enabled && <span className="badge" style={{ fontSize: '0.65rem' }}>disabled</span>}
                  {s.drives_devices && (
                    <span title="Refused while a test run is using the simulators"
                      style={{ display: 'inline-flex', gap: 4, alignItems: 'center', fontSize: '0.7rem', color: 'var(--warning)' }}>
                      <Smartphone size={12} /> simulators
                    </span>
                  )}
                  <div style={{ flex: 1 }} />
                  {isAdmin && (
                    <>
                      <button style={btn()} onClick={() => test(s.id)} disabled={busy === s.id}>
                        {busy === s.id ? <Loader2 size={13} className="spin" /> : <Plug size={13} />} Test
                      </button>
                      <button style={btn()} onClick={() => open(s, s.id)}><Pencil size={13} /> Edit</button>
                      <button style={btn()} onClick={() => remove(s)}><Trash2 size={13} /></button>
                    </>
                  )}
                </div>
                {s.description && (
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginTop: 4 }}>{s.description}</div>
                )}
                {r && (
                  <div style={{ marginTop: 8, fontSize: '0.75rem' }}>
                    {r.ok ? (
                      <>
                        <div style={{ color: 'var(--success)', display: 'flex', gap: 6, alignItems: 'center' }}>
                          <CheckCircle2 size={13} /> Connected{r.server?.name ? ` to ${r.server.name}` : ''} in {r.seconds}s
                          · {r.tools.length} tool{r.tools.length === 1 ? '' : 's'}
                        </div>
                        <div style={{ marginTop: 4, color: 'var(--text-muted)', fontFamily: 'monospace', lineHeight: 1.6 }}>
                          {r.tools.map(t => t.name).join(' · ')}
                        </div>
                      </>
                    ) : (
                      <div style={{ color: 'var(--danger)', display: 'flex', gap: 6 }}>
                        <AlertTriangle size={13} /> {r.error || 'Could not connect.'}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
