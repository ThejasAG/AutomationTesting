import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import ModalPortal from '../components/ModalPortal';
import {
  Save,
  Play,
  FileCode,
  ChevronDown,
  Loader2,
  CheckCircle2,
  AlertCircle,
  MonitorSmartphone,
  Wand2,
  X,
} from 'lucide-react';
import FileTree from '../components/FileTree';
import type { FileEntry } from '../components/FileTree';
import MonacoEditor from '../components/MonacoEditor';
import ScenarioPanel from '../components/ScenarioPanel';
import BundleIdSelect from '../components/BundleIdSelect';
import {
  getProjects,
  getProjectFiles,
  getProjectFileContent,
  saveProjectFile,
  runProjectFile,
  generateScript,
  captureLocators,
  getDevices,
  getScenarioDevices,
  getScenarios,
  createScenario,
  getTickets,
  createTicket,
  deleteTicket,
  runTicket,
  draftTicketScenarios,
} from '../api';
import type { Project, Device, SimDevice, SavedScenario, Ticket, DraftScenario } from '../api';

// ── Types ────────────────────────────────────────────────────────────────────

type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Infer Monaco language from a file extension. */
function detectLanguage(filePath: string | null): string {
  if (!filePath) return 'python';
  if (filePath.endsWith('.yaml') || filePath.endsWith('.yml')) return 'yaml';
  if (filePath.endsWith('.json')) return 'json';
  if (filePath.endsWith('.md')) return 'markdown';
  if (filePath.endsWith('.txt')) return 'plaintext';
  return 'python';
}

// ── Device Selector Modal ─────────────────────────────────────────────────────

interface DeviceModalProps {
  deviceId: string;
  devices: SimDevice[];
  onDeviceChange: (val: string) => void;
  onConfirm: () => void;
  onClose: () => void;
}

function DeviceModal({ deviceId, devices, onDeviceChange, onConfirm, onClose }: DeviceModalProps) {
  return (
    <ModalPortal onClose={onClose}>
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.72)',
        backdropFilter: 'blur(6px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 999,
        padding: 16,
      }}
      onClick={onClose}
    >
      <div
        className="card modal-pop"
        style={{ width: 420, maxWidth: '94vw', maxHeight: '90vh', overflowY: 'auto', padding: '32px', position: 'relative' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Close button */}
        <button
          onClick={onClose}
          style={{
            position: 'absolute',
            top: 16,
            right: 16,
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
            color: 'var(--text-muted)',
            display: 'flex',
          }}
        >
          <X size={18} />
        </button>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '20px' }}>
          <MonitorSmartphone size={24} color="var(--accent-primary)" />
          <h3 style={{ margin: 0 }}>Select Target Device</h3>
        </div>

        <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginBottom: '20px' }}>
          Pick the simulator to run this script on. Booted simulators are marked ●.
        </p>

        <select
          value={deviceId}
          onChange={e => onDeviceChange(e.target.value)}
          autoFocus
          style={{
            width: '100%', boxSizing: 'border-box',
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid var(--border-highlight)',
            borderRadius: 'var(--radius-sm)',
            color: 'var(--text-primary)',
            padding: '10px 14px',
            fontSize: '0.95rem',
            marginBottom: '24px',
            outline: 'none',
            cursor: 'pointer',
          }}
        >
          {devices.length === 0 && <option value="">No simulators found</option>}
          {devices.map(d => (
            <option key={d.udid} value={d.udid}>
              {d.name}{d.state === 'Booted' ? ' ● booted' : ''} — iOS {d.ios}
            </option>
          ))}
        </select>

        <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
          <button
            onClick={onClose}
            style={{
              background: 'transparent',
              border: '1px solid var(--border-color)',
              borderRadius: 'var(--radius-sm)',
              color: 'var(--text-secondary)',
              padding: '8px 20px',
              cursor: 'pointer',
              fontSize: '0.875rem',
              fontFamily: 'inherit',
            }}
          >
            Cancel
          </button>
          <button
            className="btn"
            disabled={!deviceId.trim()}
            onClick={onConfirm}
            style={{ padding: '8px 20px', fontSize: '0.875rem', opacity: deviceId.trim() ? 1 : 0.5 }}
          >
            <Play size={14} /> Run File
          </button>
        </div>
      </div>
    </div>
    </ModalPortal>
  );
}

// ── AI Generate Modal ─────────────────────────────────────────────────────────

interface GenModalProps {
  onClose: () => void;
  /** Called with the generated script + a suggested filename. */
  onGenerated: (script: string, filename: string) => void;
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  background: 'rgba(255,255,255,0.05)',
  border: '1px solid var(--border-highlight)',
  borderRadius: 'var(--radius-sm)',
  color: 'var(--text-primary)',
  padding: '9px 12px',
  fontSize: '0.9rem',
  outline: 'none',
  fontFamily: 'inherit',
};

function GenerateModal({ onClose, onGenerated }: GenModalProps) {
  const [provider, setProvider] = useState('ollama');   // local, no key, default
  const [apiKey, setApiKey] = useState('');
  const [appName, setAppName] = useState('');
  const [requirements, setRequirements] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [error, setError] = useState<string | null>(null);

  // Optional grounding: capture real testIDs from a running app first.
  const [ground, setGround] = useState(false);
  const [devices, setDevices] = useState<Device[]>([]);
  const [deviceId, setDeviceId] = useState('');
  const [bundleId, setBundleId] = useState('');

  const needsKey = provider !== 'ollama';

  useEffect(() => {
    if (!ground) return;
    getDevices().then(r => {
      setDevices(r.devices);
      if (r.devices.length && !deviceId) setDeviceId(r.devices[0].id);
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ground]);

  const canGenerate =
    appName.trim() && requirements.trim() && !busy &&
    (!needsKey || apiKey.trim()) &&
    (!ground || (deviceId && bundleId.trim()));

  const handleGenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      let known: string[] | undefined;
      let folded: string[] | undefined;
      if (ground) {
        setStatus('Capturing real locators from the running app…');
        const cap = await captureLocators(deviceId, bundleId.trim());
        known = cap.known_testids;
        folded = cap.folded_testids;
        setStatus(`Captured ${known.length} testIDs — generating…`);
      } else {
        setStatus('Generating…');
      }
      const { test_script } = await generateScript({
        provider,
        api_key: needsKey ? apiKey.trim() : undefined,
        app_name: appName.trim(),
        requirements: requirements.trim(),
        known_testids: known,
        folded_testids: folded,
      });
      const slug = appName.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
      onGenerated(test_script, `e2e/test_${slug || 'generated'}.py`);
    } catch (e: any) {
      setError(e?.message || 'Generation failed');
      setBusy(false);
      setStatus('');
    }
  };

  return (
    <ModalPortal onClose={onClose}>
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)',
        backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center',
        justifyContent: 'center', zIndex: 999, padding: 16,
      }}
      onClick={onClose}
    >
      <div
        className="card modal-pop"
        style={{ width: 520, maxWidth: '94vw', maxHeight: '90vh', overflowY: 'auto', padding: '32px', position: 'relative' }}
        onClick={e => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          style={{ position: 'absolute', top: 16, right: 16, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex' }}
        >
          <X size={18} />
        </button>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
          <Wand2 size={24} color="var(--accent-primary)" />
          <h3 style={{ margin: 0 }}>Generate Test Script with AI</h3>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: '22px' }}>
          Describe what to test. The generated script drops into the editor, where you can edit and save it.
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
          <div style={{ display: 'flex', gap: '12px' }}>
            <div style={{ flex: needsKey ? 1 : 2 }}>
              <label style={labelStyle}>Provider</label>
              <select value={provider} onChange={e => setProvider(e.target.value)} style={{ ...inputStyle, appearance: 'none', cursor: 'pointer' }}>
                <option value="ollama">Ollama (Local — no key)</option>
                <option value="claude">Claude</option>
                <option value="openai">OpenAI</option>
                <option value="gemini">Gemini</option>
              </select>
            </div>
            {needsKey && (
              <div style={{ flex: 2 }}>
                <label style={labelStyle}>API Key</label>
                <input type="password" placeholder="sk-…" value={apiKey} onChange={e => setApiKey(e.target.value)} style={inputStyle} />
              </div>
            )}
          </div>

          <div>
            <label style={labelStyle}>App name</label>
            <input type="text" placeholder="Consumer App" value={appName} onChange={e => setAppName(e.target.value)} style={inputStyle} />
          </div>

          <div>
            <label style={labelStyle}>What should the test cover?</label>
            <textarea
              placeholder="e.g. Verify the home feed loads restaurant cards from the API, then tap the first card and assert the detail screen opens."
              value={requirements}
              onChange={e => setRequirements(e.target.value)}
              rows={4}
              style={{ ...inputStyle, resize: 'vertical', fontFamily: 'inherit' }}
            />
          </div>

          {/* Optional grounding — capture real testIDs from the running app */}
          <div style={{ border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: '12px 14px' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
              <input type="checkbox" checked={ground} onChange={e => setGround(e.target.checked)} />
              Ground with real testIDs from the running app <span style={{ color: 'var(--text-muted)' }}>(recommended — stops the AI inventing selectors)</span>
            </label>
            {ground && (
              <div style={{ display: 'flex', gap: '10px', marginTop: '12px' }}>
                <div style={{ flex: 1 }}>
                  <label style={labelStyle}>Device</label>
                  <select value={deviceId} onChange={e => setDeviceId(e.target.value)} style={{ ...inputStyle, appearance: 'none', cursor: 'pointer' }}>
                    {devices.length === 0 && <option value="">No devices online</option>}
                    {devices.map(d => <option key={d.id} value={d.id}>{d.name || d.id}</option>)}
                  </select>
                </div>
                <div style={{ flex: 1 }}>
                  <label style={labelStyle}>App bundle id</label>
                  <BundleIdSelect
                    value={bundleId}
                    onChange={setBundleId}
                    style={inputStyle}
                    placeholder="org.vyapy.sarls.vyaconsumer"
                  />
                </div>
              </div>
            )}
          </div>

          {busy && status && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)', fontSize: '0.82rem' }}>
              <Loader2 size={13} style={{ animation: 'se-spin 1s linear infinite' }} /> {status}
            </div>
          )}
          {error && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--danger)', fontSize: '0.82rem' }}>
              <AlertCircle size={14} /> {error}
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end', marginTop: '24px' }}>
          <button
            onClick={onClose}
            style={{ background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', color: 'var(--text-secondary)', padding: '8px 20px', cursor: 'pointer', fontSize: '0.875rem', fontFamily: 'inherit' }}
          >
            Cancel
          </button>
          <button
            className="btn"
            disabled={!canGenerate}
            onClick={handleGenerate}
            style={{ padding: '8px 20px', fontSize: '0.875rem', opacity: canGenerate ? 1 : 0.5 }}
          >
            {busy ? <Loader2 size={14} style={{ animation: 'se-spin 1s linear infinite' }} /> : <Wand2 size={14} />}
            {busy ? 'Generating…' : 'Generate'}
          </button>
        </div>
      </div>
    </div>
    </ModalPortal>
  );
}

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '0.7rem', color: 'var(--text-muted)',
  textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '6px', fontWeight: 600,
};

// ── Main Page ─────────────────────────────────────────────────────────────────

// ── Tickets section (paste issue → link PR + scenarios → auto-test on push) ────
function statusChip(status: string) {
  const map: Record<string, { bg: string; fg: string; label: string }> = {
    passed: { bg: 'rgba(34,197,94,0.15)', fg: '#22c55e', label: 'PASSED' },
    failed: { bg: 'rgba(239,68,68,0.15)', fg: '#ef4444', label: 'FAILED' },
    running: { bg: 'rgba(99,102,241,0.15)', fg: '#818cf8', label: 'RUNNING' },
    untested: { bg: 'rgba(148,163,184,0.15)', fg: '#94a3b8', label: 'UNTESTED' },
  };
  const s = map[status] || map.untested;
  return <span style={{ background: s.bg, color: s.fg, padding: '2px 10px', borderRadius: 999, fontSize: '0.7rem', fontWeight: 700 }}>{s.label}</span>;
}

function TicketsSection({ projects }: { projects: Project[] }) {
  const navigate = useNavigate();
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [scenarios, setScenarios] = useState<SavedScenario[]>([]);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [prNumber, setPrNumber] = useState('');
  const [matchKey, setMatchKey] = useState('');
  const [projectId, setProjectId] = useState('');
  const [picked, setPicked] = useState<string[]>([]);
  const [ttype, setTtype] = useState('ui');
  const [setupId, setSetupId] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [drafts, setDrafts] = useState<DraftScenario[]>([]);
  const [drafting, setDrafting] = useState(false);

  const aiDraft = async () => {
    if (!description.trim()) { setMsg('Paste the ticket description first.'); return; }
    setDrafting(true); setMsg('AI is drafting scenarios…'); setDrafts([]);
    try {
      const r = await draftTicketScenarios(title, description, projectId);
      setDrafts(r.scenarios); setTtype(r.type || ttype);
      setMsg(r.scenarios.length ? `Drafted ${r.scenarios.length} scenario(s) — review, then save & link.` : 'No scenarios drafted.');
    } catch (e) { setMsg('AI draft failed: ' + String(e)); }
    finally { setDrafting(false); }
  };

  const saveDrafts = async () => {
    setBusy(true);
    try {
      const ids: string[] = [];
      for (const d of drafts) {
        const sc = await createScenario({ name: d.name, steps: d.steps, covers: [], project_id: projectId || null });
        ids.push(sc.id);
      }
      const fresh = await getScenarios(); setScenarios(fresh);
      setPicked(p => [...new Set([...p, ...ids])]);
      setDrafts([]); setMsg(`Saved ${ids.length} scenario(s) and linked them. Now Save the ticket.`);
    } catch (e) { setMsg('Saving drafts failed: ' + String(e)); }
    finally { setBusy(false); }
  };

  // ── edit the AI drafts before saving ──────────────────────────────────────
  const editName = (i: number, val: string) =>
    setDrafts(ds => ds.map((d, k) => k === i ? { ...d, name: val } : d));
  const editStep = (i: number, j: number, val: string) =>
    setDrafts(ds => ds.map((d, k) => k === i
      ? { ...d, steps: d.steps.map((s, m) => m === j ? val : s),
          unverified: (d.unverified || []).filter(u => u !== d.steps[j]) }  // clear the flag once edited
      : d));
  const removeStep = (i: number, j: number) =>
    setDrafts(ds => ds.map((d, k) => k === i ? { ...d, steps: d.steps.filter((_, m) => m !== j) } : d));
  const addStep = (i: number, at: number) =>
    setDrafts(ds => ds.map((d, k) => k === i
      ? { ...d, steps: [...d.steps.slice(0, at + 1), 'click ', ...d.steps.slice(at + 1)] } : d));
  const removeScenario = (i: number) => setDrafts(ds => ds.filter((_, k) => k !== i));

  const refresh = useCallback(() => { getTickets().then(setTickets).catch(() => {}); }, []);
  useEffect(() => { refresh(); getScenarios().then(setScenarios).catch(() => {}); }, [refresh]);

  const save = async () => {
    if (!title.trim()) { setMsg('Add a title.'); return; }
    setBusy(true); setMsg('');
    try {
      await createTicket({ title, description, pr_number: prNumber || null, match_key: matchKey || null, project_id: projectId || null, scenario_ids: picked, ticket_type: ttype, setup_scenario_id: setupId || null });
      setTitle(''); setDescription(''); setPrNumber(''); setMatchKey(''); setPicked([]); setSetupId('');
      setMsg('Ticket saved ✓'); refresh();
    } catch (e) { setMsg('Save failed: ' + String(e)); }
    finally { setBusy(false); }
  };

  const onRun = async (id: string) => {
    setMsg('Running linked scenarios…');
    try { await runTicket(id); refresh(); setTimeout(refresh, 4000); }
    catch (e) { setMsg('Run failed: ' + String(e)); }
  };
  const onDelete = async (id: string) => { await deleteTicket(id); refresh(); };
  const toggle = (id: string) => setPicked(p => p.includes(id) ? p.filter(x => x !== id) : [...p, id]);

  const inputStyle: React.CSSProperties = { width: '100%', padding: '8px 10px', borderRadius: 8, background: 'var(--bg-secondary)', color: 'var(--text-primary)', border: '1px solid var(--border-color)', fontFamily: 'inherit', fontSize: '0.85rem' };

  return (
    <div style={{ flex: 1, overflow: 'auto', display: 'grid', gridTemplateColumns: '420px 1fr', gap: 20 }}>
      {/* Paste + link form */}
      <div className="card" style={{ height: 'fit-content' }}>
        <h3 style={{ margin: '0 0 12px', fontSize: '1rem' }}>New ticket → link a PR</h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <input placeholder="Ticket title" value={title} onChange={e => setTitle(e.target.value)} style={inputStyle} />
          <textarea placeholder="Paste the full ticket description (issue, steps, acceptance criteria)…" value={description} onChange={e => setDescription(e.target.value)} rows={8} style={{ ...inputStyle, resize: 'vertical' }} />
          <button className="btn" onClick={aiDraft} disabled={drafting}
            style={{ background: 'var(--bg-elevated)', border: '1px solid var(--accent-primary)', color: 'var(--accent-primary)' }}>
            {drafting ? 'Drafting…' : '✨ AI Draft scenarios from ticket'}
          </button>
          {drafts.length > 0 && (
            <div style={{ border: '1px solid var(--accent-primary)', borderRadius: 8, padding: 10, background: 'rgba(99,102,241,0.05)' }}>
              <div style={{ fontSize: '0.8rem', fontWeight: 700, marginBottom: 8 }}>AI drafts — edit anything before saving:</div>
              {drafts.map((d, i) => {
                const unv = new Set(d.unverified || []);
                return (
                  <div key={i} style={{ marginBottom: 12, paddingBottom: 8, borderBottom: i < drafts.length - 1 ? '1px solid var(--border-color)' : 'none' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                      <input value={d.name} onChange={e => editName(i, e.target.value)}
                        style={{ ...inputStyle, fontSize: '0.82rem', fontWeight: 600, padding: '5px 8px', flex: 1 }} />
                      <button onClick={() => removeScenario(i)} title="Remove scenario"
                        style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '0.9rem' }}>🗑</button>
                    </div>
                    {d.steps.map((st, j) => {
                      const flagged = unv.has(st);
                      return (
                        <div key={j} style={{ display: 'flex', alignItems: 'center', gap: 5, marginLeft: 8, marginBottom: 3 }}>
                          <span title={flagged ? 'Not found in the app — fix or confirm before relying on it' : undefined}
                            style={{ fontSize: '0.75rem', width: 14, textAlign: 'center', color: flagged ? 'var(--warning, #f59e0b)' : 'var(--text-muted)' }}>
                            {flagged ? '⚠' : '•'}
                          </span>
                          <input value={st} onChange={e => editStep(i, j, e.target.value)}
                            title={flagged ? 'needs recording — fix this step' : undefined}
                            style={{ ...inputStyle, fontSize: '0.75rem', padding: '4px 7px', flex: 1,
                              fontFamily: 'monospace',
                              borderColor: flagged ? 'var(--warning, #f59e0b)' : 'var(--border-color)',
                              color: flagged ? 'var(--warning, #f59e0b)' : 'var(--text-primary)' }} />
                          <button onClick={() => addStep(i, j)} title="Add step below"
                            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '0.85rem' }}>＋</button>
                          <button onClick={() => removeStep(i, j)} title="Delete step"
                            style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '0.85rem' }}>✕</button>
                        </div>
                      );
                    })}
                  </div>
                );
              })}
              {drafts.some(d => (d.unverified || []).length > 0) && (
                <div style={{ fontSize: '0.72rem', color: 'var(--warning, #f59e0b)', marginTop: 2, marginBottom: 6 }}>
                  ⚠ highlighted steps couldn't be matched to a real element/screen — edit them (the ⚠ clears once you change the text), then save.
                </div>
              )}
              <button className="btn" onClick={saveDrafts} disabled={busy} style={{ marginTop: 4, fontSize: '0.8rem' }}>Save these as scenarios &amp; link</button>
            </div>
          )}
          <div>
            <input placeholder="Ticket / branch key — e.g. NEWVYA-1134 (links to the PR before it exists)"
              value={matchKey} onChange={e => setMatchKey(e.target.value)} style={inputStyle} />
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', margin: '3px 0 0' }}>
              When a PR whose branch/title contains this key is opened, it auto-links & tests — no PR number needed up front.
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <input placeholder="PR number (optional — auto-filled when the PR appears)" value={prNumber} onChange={e => setPrNumber(e.target.value)} style={inputStyle} />
            <select value={ttype} onChange={e => setTtype(e.target.value)} style={{ ...inputStyle, width: 130 }}>
              <option value="ui">UI</option><option value="calc">Calculation</option>
              <option value="data">Data/API</option><option value="crash">Crash</option><option value="mixed">Mixed</option>
            </select>
          </div>
          <select value={projectId} onChange={e => setProjectId(e.target.value)} style={inputStyle}>
            <option value="">(project — optional)</option>
            {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <div>
            <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', margin: '2px 0 4px' }}>Setup scenario — runs FIRST (e.g. "Book an event"), so the checks have something to act on:</div>
            <select value={setupId} onChange={e => setSetupId(e.target.value)} style={inputStyle}>
              <option value="">(no setup — checks run as-is)</option>
              {scenarios.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>
          <div>
            <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', margin: '2px 0 6px' }}>Link scenarios that verify this ticket:</div>
            <div style={{ maxHeight: 160, overflow: 'auto', border: '1px solid var(--border-color)', borderRadius: 8, padding: 6 }}>
              {scenarios.length === 0 ? <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem', padding: 6 }}>No scenarios yet — record some in the Scenarios tab.</div>
                : scenarios.map(s => (
                <label key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 6px', fontSize: '0.85rem', cursor: 'pointer' }}>
                  <input type="checkbox" checked={picked.includes(s.id)} onChange={() => toggle(s.id)} />
                  {s.name} <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>({s.steps.length} steps)</span>
                </label>
              ))}
            </div>
          </div>
          <button className="btn" onClick={save} disabled={busy} style={{ marginTop: 4 }}>{busy ? 'Saving…' : 'Save ticket'}</button>
          {msg && <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{msg}</div>}
        </div>
      </div>

      {/* Ticket list (traceability) */}
      <div>
        <h3 style={{ margin: '0 0 12px', fontSize: '1rem' }}>Tickets ({tickets.length})</h3>
        {tickets.length === 0 ? <div className="card" style={{ color: 'var(--text-secondary)' }}>No tickets yet. Paste one on the left and link a PR + scenarios. When that PR is pushed, the linked scenarios run automatically and this flips ✅/❌.</div>
          : <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {tickets.map(t => (
            <div key={t.id} className="card" style={{ padding: '14px 16px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, justifyContent: 'space-between' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                  {statusChip(t.status)}
                  <strong style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.title}</strong>
                  {t.pr_number && <span style={{ fontSize: '0.75rem', color: 'var(--accent-primary)' }}>PR #{t.pr_number}</span>}
                  <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>{(t.scenario_ids || []).length} scenario(s) · {t.ticket_type}</span>
                </div>
                <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                  {t.last_run_id && <button className="btn" style={btnSm} onClick={() => navigate(`/run/${t.last_run_id}`)}>Report</button>}
                  <button className="btn" style={btnSm} onClick={() => onRun(t.id)} disabled={!(t.scenario_ids || []).length}>Run now</button>
                  <button className="btn" style={{ ...btnSm, color: '#ef4444' }} onClick={() => onDelete(t.id)}>Delete</button>
                </div>
              </div>
            </div>
          ))}
        </div>}
      </div>
    </div>
  );
}
const btnSm: React.CSSProperties = { padding: '4px 10px', fontSize: '0.75rem', background: 'var(--bg-elevated)', border: '1px solid var(--border-color)', color: 'var(--text-primary)' };

export default function ScriptEditorPage() {
  const navigate = useNavigate();

  // Project state
  const [projects, setProjects] = useState<Project[]>([]);
  const [view, setView] = useState<'tickets' | 'editor'>('tickets');
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');

  // File tree state
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [isLoadingFiles, setIsLoadingFiles] = useState(false);

  // Editor state
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [editorContent, setEditorContent] = useState<string>('');
  const [isLoadingContent, setIsLoadingContent] = useState(false);

  // Save state
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');

  // Run / device state
  const [deviceId, setDeviceId] = useState<string>('');
  const [sims, setSims] = useState<SimDevice[]>([]);
  const [showDeviceModal, setShowDeviceModal] = useState(false);
  const [isRunning, setIsRunning] = useState(false);

  // ── Load available simulators; default to a booted one ──────────────────
  useEffect(() => {
    getScenarioDevices()
      .then(({ simulators }) => {
        setSims(simulators);
        setDeviceId(prev => {
          if (prev && simulators.some(s => s.udid === prev)) return prev;
          const booted = simulators.find(s => s.state === 'Booted');
          return (booted || simulators[0])?.udid || '';
        });
      })
      .catch(() => { /* picker shows "no simulators found" */ });
  }, []);

  // AI generation
  const [showGenModal, setShowGenModal] = useState(false);
  const [showScenario, setShowScenario] = useState(false);

  // ── Load project list on mount ──────────────────────────────────────────
  useEffect(() => {
    getProjects()
      .then(list => {
        setProjects(list);
        if (list.length > 0) setSelectedProjectId(list[0].id);
      })
      .catch(err => console.error('Failed to load projects:', err));
  }, []);

  // ── Load file tree when project changes ─────────────────────────────────
  useEffect(() => {
    if (!selectedProjectId) return;
    setIsLoadingFiles(true);
    setFiles([]);
    setSelectedFile(null);
    setEditorContent('');
    setSaveStatus('idle');

    getProjectFiles(selectedProjectId)
      .then(setFiles)
      .catch(err => console.error('Failed to load files:', err))
      .finally(() => setIsLoadingFiles(false));
  }, [selectedProjectId]);

  // ── Load file content when a file is selected ────────────────────────────
  const handleFileSelect = useCallback(
    async (path: string) => {
      if (!selectedProjectId || path === selectedFile) return;
      setSelectedFile(path);
      setIsLoadingContent(true);
      setSaveStatus('idle');
      try {
        const content = await getProjectFileContent(selectedProjectId, path);
        setEditorContent(content);
      } catch (err) {
        console.error('Failed to load file content:', err);
        setEditorContent('');
      } finally {
        setIsLoadingContent(false);
      }
    },
    [selectedProjectId, selectedFile],
  );

  // ── Save ─────────────────────────────────────────────────────────────────
  const handleSave = async () => {
    if (!selectedProjectId || !selectedFile) return;
    setSaveStatus('saving');
    try {
      await saveProjectFile(selectedProjectId, selectedFile, editorContent);
      setSaveStatus('saved');
      setTimeout(() => setSaveStatus('idle'), 3000);
    } catch {
      setSaveStatus('error');
      setTimeout(() => setSaveStatus('idle'), 3000);
    }
  };

  // ── Run (called after device is confirmed) ───────────────────────────────
  const executeRun = async (targetDeviceId: string) => {
    if (!selectedProjectId || !selectedFile) return;
    setIsRunning(true);
    try {
      const { run_id } = await runProjectFile(selectedProjectId, selectedFile, targetDeviceId);
      navigate(`/run/${run_id}`);
    } catch (err) {
      console.error('Failed to start run:', err);
      setIsRunning(false);
    }
  };

  const handleRunClick = () => {
    if (!selectedFile) return;
    if (deviceId.trim()) {
      // Device already selected from a previous run — go directly.
      executeRun(deviceId.trim());
    } else {
      setShowDeviceModal(true);
    }
  };

  const handleDeviceConfirm = () => {
    setShowDeviceModal(false);
    if (deviceId.trim()) executeRun(deviceId.trim());
  };

  // ── AI generation → drop into editor ─────────────────────────────────────
  const handleGenerated = (script: string, filename: string) => {
    setShowGenModal(false);
    // Target a new file so Save has somewhere to write. saveProjectFile creates
    // it (and commits) on first save; the file tree refreshes after that.
    setSelectedFile(filename);
    setEditorContent(script);
    setSaveStatus('idle');
  };

  // ── Keyboard shortcut: Ctrl+S to save ───────────────────────────────────
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        handleSave();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId, selectedFile, editorContent]);

  // ── Derived values ───────────────────────────────────────────────────────
  const editorLanguage = detectLanguage(selectedFile);
  const canSave = Boolean(selectedFile) && saveStatus !== 'saving';
  const canRun = Boolean(selectedFile) && !isRunning;

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div
      className="animate-fade-in"
      style={{ height: 'calc(100vh - 96px)', display: 'flex', flexDirection: 'column' }}
    >
      {/* Page header */}
      <div className="page-header" style={{ marginBottom: '20px', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <FileCode size={28} color="var(--accent-primary)" />
          <div style={{ flex: 1 }}>
            <h1 className="page-title" style={{ margin: 0 }}>Test Authoring</h1>
            <p className="page-subtitle" style={{ marginBottom: 0 }}>
              Link Jira tickets to PRs for auto-testing, or generate & edit test scripts
            </p>
          </div>
          {/* Tickets ⟷ Editor tabs */}
          <div style={{ display: 'flex', gap: 4, background: 'var(--bg-secondary)', borderRadius: 8, padding: 3 }}>
            {(['tickets', 'editor'] as const).map(v => (
              <button key={v} onClick={() => setView(v)}
                style={{ padding: '6px 14px', borderRadius: 6, border: 'none', cursor: 'pointer', fontFamily: 'inherit', fontSize: '0.82rem', fontWeight: 600,
                  background: view === v ? 'var(--accent-primary)' : 'transparent', color: view === v ? '#fff' : 'var(--text-secondary)' }}>
                {v === 'tickets' ? 'Tickets' : 'Script Editor'}
              </button>
            ))}
          </div>
          {view === 'editor' && <>
          <button
            className="btn"
            onClick={() => setShowScenario(true)}
            style={{ padding: '8px 16px', fontSize: '0.85rem', background: 'var(--bg-elevated)', border: '1px solid var(--border-color)', color: 'var(--text-primary)' }}
          >
            <Wand2 size={14} /> Scenario
          </button>
          <button
            className="btn"
            onClick={() => setShowGenModal(true)}
            style={{ padding: '8px 16px', fontSize: '0.85rem' }}
          >
            <Wand2 size={14} /> Generate with AI
          </button>
          </>}
        </div>
      </div>

      {view === 'tickets' && <TicketsSection projects={projects} />}

      {/* Editor shell */}
      <div
        style={{
          flex: 1,
          display: view === 'editor' ? 'flex' : 'none',
          minHeight: 0,
          border: '1px solid var(--border-color)',
          borderRadius: 'var(--radius-lg)',
          overflow: 'hidden',
          background: 'rgba(10,10,15,0.5)',
        }}
      >
        {/* ── Left panel — file tree ───────────────────────────────────── */}
        <div
          style={{
            width: 280,
            flexShrink: 0,
            borderRight: '1px solid var(--border-color)',
            display: 'flex',
            flexDirection: 'column',
            background: 'rgba(8,8,12,0.6)',
          }}
        >
          {/* Project selector */}
          <div
            style={{
              padding: '14px 16px',
              borderBottom: '1px solid var(--border-color)',
              flexShrink: 0,
            }}
          >
            <label
              style={{
                display: 'block',
                fontSize: '0.7rem',
                color: 'var(--text-muted)',
                textTransform: 'uppercase',
                letterSpacing: '0.08em',
                marginBottom: '8px',
                fontWeight: 600,
              }}
            >
              Project
            </label>
            <div style={{ position: 'relative' }}>
              <select
                value={selectedProjectId}
                onChange={e => setSelectedProjectId(e.target.value)}
                style={{
                  width: '100%',
                  background: 'rgba(255,255,255,0.05)',
                  border: '1px solid var(--border-color)',
                  borderRadius: 'var(--radius-sm)',
                  color: 'var(--text-primary)',
                  padding: '8px 32px 8px 12px',
                  fontSize: '0.85rem',
                  appearance: 'none',
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
              >
                {projects.length === 0 && (
                  <option value="">No projects</option>
                )}
                {projects.map(p => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              <ChevronDown
                size={13}
                style={{
                  position: 'absolute',
                  right: 10,
                  top: '50%',
                  transform: 'translateY(-50%)',
                  color: 'var(--text-muted)',
                  pointerEvents: 'none',
                }}
              />
            </div>
          </div>

          {/* File tree */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '8px 4px' }}>
            {isLoadingFiles ? (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                  padding: '16px 12px',
                  color: 'var(--text-muted)',
                  fontSize: '0.85rem',
                }}
              >
                <Loader2 size={14} style={{ animation: 'se-spin 1s linear infinite' }} />
                Loading files…
              </div>
            ) : selectedProjectId && files.length === 0 ? (
              <div style={{ padding: '20px 14px', color: 'var(--text-muted)', fontSize: '0.82rem', lineHeight: 1.6 }}>
                No test scripts in this project yet.<br />
                Use <strong>✨ Generate</strong> to create one, or record a flow in the
                <strong> Scenarios</strong> tab — new scripts are saved under <code>e2e/</code>.
              </div>
            ) : (
              <FileTree
                files={files}
                selected={selectedFile}
                onSelect={handleFileSelect}
              />
            )}
          </div>
        </div>

        {/* ── Right panel — editor + toolbar ──────────────────────────── */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          {/* Tab bar */}
          {selectedFile && (
            <div
              style={{
                height: 36,
                borderBottom: '1px solid var(--border-color)',
                display: 'flex',
                alignItems: 'center',
                padding: '0 16px',
                gap: '8px',
                background: 'rgba(0,0,0,0.3)',
                flexShrink: 0,
              }}
            >
              <FileCode size={13} color="var(--accent-primary)" />
              <span
                style={{
                  fontSize: '0.8rem',
                  color: 'var(--text-secondary)',
                  fontFamily: "'Fira Code', monospace",
                }}
              >
                {selectedFile}
              </span>
            </div>
          )}

          {/* Monaco editor area */}
          <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
            {isLoadingContent && (
              <div
                style={{
                  position: 'absolute',
                  inset: 0,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: 'rgba(10,10,15,0.85)',
                  zIndex: 10,
                }}
              >
                <Loader2
                  size={28}
                  style={{ animation: 'se-spin 1s linear infinite', color: 'var(--accent-primary)' }}
                />
              </div>
            )}

            {selectedFile ? (
              <MonacoEditor
                value={editorContent}
                onChange={setEditorContent}
                language={editorLanguage}
              />
            ) : (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  height: '100%',
                  flexDirection: 'column',
                  gap: '14px',
                  color: 'var(--text-muted)',
                }}
              >
                <FileCode size={52} style={{ opacity: 0.25 }} />
                <p style={{ fontSize: '0.95rem' }}>Select a file to start editing</p>
                <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                  Use the file tree on the left
                </p>
              </div>
            )}
          </div>

          {/* Toolbar */}
          <div
            style={{
              height: 52,
              borderTop: '1px solid var(--border-color)',
              display: 'flex',
              alignItems: 'center',
              gap: '12px',
              padding: '0 16px',
              background: 'rgba(0,0,0,0.4)',
              flexShrink: 0,
            }}
          >
            {/* Save button */}
            <button
              className="btn"
              onClick={handleSave}
              disabled={!canSave}
              title="Save (Ctrl+S)"
              style={{
                padding: '7px 16px',
                fontSize: '0.85rem',
                opacity: canSave ? 1 : 0.4,
                cursor: canSave ? 'pointer' : 'not-allowed',
              }}
            >
              {saveStatus === 'saving' ? (
                <Loader2 size={13} style={{ animation: 'se-spin 1s linear infinite' }} />
              ) : (
                <Save size={13} />
              )}
              {saveStatus === 'saving' ? 'Saving…' : 'Save'}
            </button>

            {/* Run button */}
            <button
              className="btn"
              onClick={handleRunClick}
              disabled={!canRun}
              style={{
                padding: '7px 16px',
                fontSize: '0.85rem',
                background: 'linear-gradient(135deg, #059669, #10b981)',
                boxShadow: '0 4px 12px rgba(16,185,129,0.3)',
                opacity: canRun ? 1 : 0.4,
                cursor: canRun ? 'pointer' : 'not-allowed',
              }}
            >
              {isRunning ? (
                <Loader2 size={13} style={{ animation: 'se-spin 1s linear infinite' }} />
              ) : (
                <Play size={13} />
              )}
              {isRunning ? 'Launching…' : 'Run'}
            </button>

            {/* Device badge (shows current device) */}
            {deviceId && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '4px 10px',
                  background: 'rgba(255,255,255,0.04)',
                  border: '1px solid var(--border-color)',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: '0.78rem',
                  color: 'var(--text-secondary)',
                  cursor: 'pointer',
                }}
                title="Click to change device"
                onClick={() => setShowDeviceModal(true)}
              >
                <MonitorSmartphone size={12} />
                {sims.find(s => s.udid === deviceId)?.name || deviceId || 'Select device'}
              </div>
            )}

            {/* Status messages */}
            {saveStatus === 'saved' && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  color: 'var(--success)',
                  fontSize: '0.82rem',
                }}
              >
                <CheckCircle2 size={13} />
                Saved &amp; committed
              </div>
            )}
            {saveStatus === 'error' && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  color: 'var(--danger)',
                  fontSize: '0.82rem',
                }}
              >
                <AlertCircle size={13} />
                Save failed
              </div>
            )}

            {/* Spacer + keyboard hint */}
            <div style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: '0.75rem' }}>
              Ctrl+S to save
            </div>
          </div>
        </div>
      </div>

      {/* Device selector modal */}
      {showDeviceModal && (
        <DeviceModal
          deviceId={deviceId}
          devices={sims}
          onDeviceChange={setDeviceId}
          onConfirm={handleDeviceConfirm}
          onClose={() => setShowDeviceModal(false)}
        />
      )}

      {/* AI generate modal */}
      {showGenModal && (
        <GenerateModal
          onClose={() => setShowGenModal(false)}
          onGenerated={handleGenerated}
        />
      )}

      {/* Scenario runner modal */}
      {showScenario && (
        <div
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)', backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'flex-start', justifyContent: 'center', zIndex: 999, padding: '40px 20px', overflowY: 'auto' }}
          onClick={() => setShowScenario(false)}
        >
          <div style={{ width: 620, maxWidth: '100%' }} onClick={e => e.stopPropagation()}>
            <ScenarioPanel
              projectId={selectedProjectId}
              onSaved={(path) => {
                if (selectedProjectId) getProjectFiles(selectedProjectId).then(setFiles).catch(() => {});
                handleFileSelect(path);
              }}
            />
          </div>
        </div>
      )}

      {/* Scoped keyframe for spinner */}
      <style>{`
        @keyframes se-spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
