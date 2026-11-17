import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Save, Play, FileCode, Loader2, CheckCircle2, AlertCircle } from 'lucide-react';
import FileTree from '../components/FileTree';
import type { FileEntry } from '../components/FileTree';
import MonacoEditor from '../components/MonacoEditor';
import {
  getProjects,
  getScenarios,
  createScenario,
  getTickets,
  createTicket,
  deleteTicket,
  runTicket,
  draftTicketScenarios,
  listCrossAppFlows,
  saveCrossAppFlow,
  runCrossAppFlow,
} from '../api';
import type { Project, SavedScenario, Ticket, DraftScenario, CrossAppFlow, FlowEnv } from '../api';
import { SCRIPT_FOLDERS, SCRIPT_TREE, flowToScript, scriptToSegments } from '../flowScript';

// ── Types ────────────────────────────────────────────────────────────────────

type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

// The script tree (flowScript.ts): ios/ holds the END TO END SCENARIO; android/ is
// empty until Android has scripts.
const TREE: FileEntry[] = Object.keys(SCRIPT_TREE).map(path => ({
  path, name: path.split('/').pop() || path, type: 'file' as const,
}));

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

  const [projects, setProjects] = useState<Project[]>([]);
  const [view, setView] = useState<'editor' | 'tickets'>('editor');

  // Script state
  const [flows, setFlows] = useState<CrossAppFlow[]>([]);
  const [selectedFile, setSelectedFile] = useState<string>('ios/end_to_end_scenario');
  const [editorContent, setEditorContent] = useState<string>('');
  const [loaded, setLoaded] = useState<string>('');          // the script as last loaded/saved
  const [isLoading, setIsLoading] = useState(true);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [message, setMessage] = useState<string>('');
  const [isRunning, setIsRunning] = useState(false);

  // Projects for the Tickets tab
  useEffect(() => {
    getProjects().then(setProjects).catch(err => console.error('Failed to load projects:', err));
  }, []);

  const entry = SCRIPT_TREE[selectedFile];
  const flow = entry ? flows.find(f => f.id === entry.flowId) : undefined;

  const loadFlows = useCallback(async () => {
    setIsLoading(true);
    try {
      setFlows((await listCrossAppFlows()).flows);
    } catch (err) {
      setMessage(`Could not load the scripts: ${err instanceof Error ? err.message : err}`);
    } finally {
      setIsLoading(false);
    }
  }, []);
  useEffect(() => { loadFlows(); }, [loadFlows]);

  // Show the selected file's script.
  useEffect(() => {
    const text = flow ? flowToScript(flow) : '';
    setEditorContent(text);
    setLoaded(text);
    setSaveStatus('idle');
    setMessage('');
  }, [flow, selectedFile]);

  const dirty = editorContent !== loaded;

  const handleSave = async () => {
    if (!flow || !dirty) return;
    const { segments, error } = scriptToSegments(editorContent);
    if (error) {
      setSaveStatus('error');
      setMessage(error);
      return;
    }
    setSaveStatus('saving');
    setMessage('');
    try {
      await saveCrossAppFlow(flow.id, { name: flow.name, description: flow.description, segments });
      setSaveStatus('saved');
      await loadFlows();
      setTimeout(() => setSaveStatus('idle'), 3000);
    } catch (err) {
      setSaveStatus('error');
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const handleRun = async () => {
    if (!flow || dirty) return;
    setIsRunning(true);
    try {
      const env = (localStorage.getItem('flowEnv') as FlowEnv) || 'staging';
      const { run_id } = await runCrossAppFlow(flow.id, env, 'tablet');
      navigate(`/run/${run_id}`);
    } catch (err) {
      setMessage(`Could not start the run: ${err instanceof Error ? err.message : err}`);
      setIsRunning(false);
    }
  };

  // Ctrl/Cmd+S saves
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
  }, [flow, editorContent, loaded]);

  const canSave = Boolean(flow) && dirty && saveStatus !== 'saving';
  const canRun = Boolean(flow) && !dirty && !isRunning;

  return (
    <div className="animate-fade-in" style={{ height: 'calc(100vh - 96px)', display: 'flex', flexDirection: 'column' }}>
      {/* Page header */}
      <div className="page-header" style={{ marginBottom: '20px', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <FileCode size={28} color="var(--accent-primary)" />
          <div style={{ flex: 1 }}>
            <h1 className="page-title" style={{ margin: 0 }}>Scripts</h1>
            <p className="page-subtitle" style={{ marginBottom: 0 }}>
              The test scripts by platform: open, edit and run them. Link Jira tickets under Tickets.
            </p>
          </div>
          <div style={{ display: 'flex', gap: 4, background: 'var(--bg-secondary)', borderRadius: 8, padding: 3 }}>
            {(['editor', 'tickets'] as const).map(v => (
              <button key={v} onClick={() => setView(v)}
                style={{ padding: '6px 14px', borderRadius: 6, border: 'none', cursor: 'pointer', fontFamily: 'inherit', fontSize: '0.82rem', fontWeight: 600,
                  background: view === v ? 'var(--accent-primary)' : 'transparent', color: view === v ? '#fff' : 'var(--text-secondary)' }}>
                {v === 'editor' ? 'Scripts' : 'Tickets'}
              </button>
            ))}
          </div>
        </div>
      </div>

      {view === 'tickets' && <TicketsSection projects={projects} />}

      {/* Scripts: tree on the left, the script on the right */}
      <div style={{ flex: 1, display: view === 'editor' ? 'flex' : 'none', minHeight: 0,
        border: '1px solid var(--border-color)', borderRadius: 'var(--radius-lg)', overflow: 'hidden', background: 'rgba(10,10,15,0.5)' }}>
        <div style={{ width: 260, flexShrink: 0, borderRight: '1px solid var(--border-color)', display: 'flex', flexDirection: 'column', background: 'rgba(8,8,12,0.6)' }}>
          <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--border-color)', fontSize: '0.7rem', color: 'var(--text-muted)',
            textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
            Scripts
          </div>
          <div style={{ flex: 1, overflowY: 'auto', padding: '8px 4px' }}>
            <FileTree files={TREE} selected={selectedFile} onSelect={setSelectedFile}
              dirOrder={SCRIPT_FOLDERS} folders={SCRIPT_FOLDERS} />
          </div>
        </div>

        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          {/* Tab bar: the file and what it is */}
          <div style={{ height: 36, borderBottom: '1px solid var(--border-color)', display: 'flex', alignItems: 'center', padding: '0 16px', gap: '8px', background: 'rgba(0,0,0,0.3)', flexShrink: 0 }}>
            <FileCode size={13} color="var(--accent-primary)" />
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', fontFamily: "'Fira Code', monospace" }}>{selectedFile}</span>
            {flow && <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              · {flow.name} · {flow.segments.length} segments{flow.edited ? ' · edited' : ''}{dirty ? ' · unsaved' : ''}</span>}
          </div>

          <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
            {isLoading && (
              <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(10,10,15,0.85)', zIndex: 10 }}>
                <Loader2 size={28} style={{ animation: 'se-spin 1s linear infinite', color: 'var(--accent-primary)' }} />
              </div>
            )}
            <MonacoEditor value={editorContent} onChange={setEditorContent} language="plaintext" readOnly={!flow} />
          </div>

          {/* Toolbar */}
          <div style={{ height: 52, borderTop: '1px solid var(--border-color)', display: 'flex', alignItems: 'center', gap: '12px', padding: '0 16px', background: 'rgba(0,0,0,0.4)', flexShrink: 0 }}>
            <button className="btn" onClick={handleSave} disabled={!canSave} title="Save (Ctrl+S)"
              style={{ padding: '7px 16px', fontSize: '0.85rem', opacity: canSave ? 1 : 0.4, cursor: canSave ? 'pointer' : 'not-allowed' }}>
              {saveStatus === 'saving' ? <Loader2 size={13} style={{ animation: 'se-spin 1s linear infinite' }} /> : <Save size={13} />}
              {saveStatus === 'saving' ? 'Saving…' : 'Save'}
            </button>
            <button className="btn" onClick={handleRun} disabled={!canRun} title={dirty ? 'Save your changes first' : 'Run on the simulators'}
              style={{ padding: '7px 16px', fontSize: '0.85rem', background: 'linear-gradient(135deg, #059669, #10b981)', boxShadow: '0 4px 12px rgba(16,185,129,0.3)',
                opacity: canRun ? 1 : 0.4, cursor: canRun ? 'pointer' : 'not-allowed' }}>
              {isRunning ? <Loader2 size={13} style={{ animation: 'se-spin 1s linear infinite' }} /> : <Play size={13} />}
              {isRunning ? 'Launching…' : 'Run'}
            </button>
            {saveStatus === 'saved' && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--success)', fontSize: '0.82rem' }}>
                <CheckCircle2 size={13} /> Saved
              </div>
            )}
            {message && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--danger)', fontSize: '0.82rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={message}>
                <AlertCircle size={13} style={{ flexShrink: 0 }} /> {message}
              </div>
            )}
            <div style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: '0.75rem' }}>Ctrl+S to save</div>
          </div>
        </div>
      </div>

      <style>{`
        @keyframes se-spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
