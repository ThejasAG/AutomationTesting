import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  getProjects, addProject, updateProject, deleteProject,
  cloneProject, pullProject, getProjectBranches, getBranchesForUrl,
  startPreparation, getPreparationStatus,
  getGroups, addGroup, deleteGroup,
  getDevices, startRun,
} from '../api';
import type {
  Project, ProjectInput, CloneStatus, Device, PreparationResult, ApplicationGroup,
  PreparationTask,
} from '../api';
import {
  Plus, Pencil, Trash2, GitBranch, DownloadCloud, RefreshCw, FolderOpen,
  Play, AlertTriangle, CheckCircle2, XCircle, Loader2, X, Smartphone,
  Layers, HeartPulse,
} from 'lucide-react';
import ModalPortal from '../components/ModalPortal';
import { parseServerDate } from '../time';

// ── Presentation helpers ─────────────────────────────────────────────────────

const CLONE_STATUS_META: Record<CloneStatus, { label: string; color: string }> = {
  not_cloned:   { label: 'Not Cloned',   color: 'var(--text-muted)' },
  syncing:      { label: 'Syncing',      color: 'var(--warning)' },
  cloned:       { label: 'Cloned',       color: 'var(--primary)' },
  ready:        { label: 'Ready',        color: 'var(--success)' },
  outdated:     { label: 'Outdated',     color: 'var(--warning)' },
  clone_failed: { label: 'Clone Failed', color: 'var(--danger)' },
};

const EMPTY_FORM: ProjectInput = {
  name: '', description: '', git_url: '', default_branch: 'main',
  platform: 'ios', repo_type: 'github', group_id: null,
};

const UNGROUPED = '__ungrouped__';

const fmtDate = (iso: string | null) =>
  iso ? parseServerDate(iso).toLocaleString() : '—';

const inputStyle: React.CSSProperties = {
  width: '100%', padding: '10px', borderRadius: '6px',
  backgroundColor: '#0d1117', border: '1px solid rgba(255,255,255,0.1)', color: '#fff',
};

const btn = (bg: string, disabled = false): React.CSSProperties => ({
  padding: '8px 12px', borderRadius: '6px', backgroundColor: bg, color: '#fff',
  border: 'none', display: 'flex', alignItems: 'center', gap: '6px',
  cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.5 : 1,
  fontSize: '0.85rem',
});

function StatusPill({ status }: { status: CloneStatus }) {
  const meta = CLONE_STATUS_META[status] ?? CLONE_STATUS_META.not_cloned;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '2px 10px',
      borderRadius: '999px', fontSize: '0.75rem', fontWeight: 600,
      color: meta.color, border: `1px solid ${meta.color}`,
      backgroundColor: 'rgba(255,255,255,0.03)',
    }}>
      {status === 'syncing' && <Loader2 size={11} className="spin" />}
      {meta.label}
    </span>
  );
}

/** One labelled cell in the Project Health panel. */
function HealthCell({ label, value, ok }: { label: string; value: React.ReactNode; ok?: boolean }) {
  return (
    <div>
      <div style={{ color: 'var(--text-muted)', marginBottom: 2, fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.03em' }}>
        {label}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 5, color: 'var(--text-secondary)' }}>
        {ok === true && <CheckCircle2 size={12} color="var(--success)" />}
        {ok === false && <XCircle size={12} color="var(--danger)" />}
        {value}
      </div>
    </div>
  );
}

/** Full Project Health panel: repo, toolchain, agent and run state at a glance. */
function ProjectHealth({ p }: { p: Project }) {
  const h = p.health;
  const lastRun = h.last_run;

  return (
    <div style={{
      display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
      gap: 12, padding: '12px 14px', marginBottom: 12, borderRadius: 8,
      background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)',
      fontSize: '0.8rem',
    }}>
      <div style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-secondary)', fontWeight: 600, marginBottom: 2 }}>
        <HeartPulse size={14} /> Project Health
      </div>

      <HealthCell label="Clone Status" value={<StatusPill status={p.clone_status} />} />
      <HealthCell label="Current Branch" value={<><GitBranch size={12} /> {p.current_branch ?? p.default_branch}</>} />
      <HealthCell label="Last Pull" value={fmtDate(p.last_pull_at)} />
      <HealthCell
        label="Project Type"
        value={p.project_type_label ?? 'Unknown'}
        ok={p.project_type !== 'unknown' ? true : undefined}
      />
      <HealthCell
        label="automation.yaml"
        value={h.automation_yaml ? 'Present' : 'Missing'}
        ok={h.automation_yaml}
      />
      <HealthCell
        label="Dependencies"
        value={h.dependencies_ok ? 'Installed' : 'Not installed'}
        ok={h.dependencies_ok}
      />
      <HealthCell
        label="Last Run"
        value={lastRun ? `${lastRun.status} · ${fmtDate(lastRun.created_at)}` : 'Never run'}
        ok={lastRun ? lastRun.status === 'passed' : undefined}
      />
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function ProjectsPage() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [groups, setGroups] = useState<ApplicationGroup[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showGroupForm, setShowGroupForm] = useState(false);
  const [groupForm, setGroupForm] = useState({ name: '', description: '' });

  // Which project id currently has an operation in flight.
  const [busy, setBusy] = useState<Record<string, string>>({});

  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ProjectInput>(EMPTY_FORM);

  const [confirmDelete, setConfirmDelete] = useState<Project | null>(null);
  const [deleteLocal, setDeleteLocal] = useState(false);

  // automation.yaml missing prompt: { project, result }
  const [yamlPrompt, setYamlPrompt] = useState<Project | null>(null);
  const [prepLog, setPrepLog] = useState<{ project: Project; result: PreparationResult } | null>(null);

  // Live progress of the background preparation (clone → deps → build → install).
  const [progress, setProgress] = useState<
    {
      project: Project;
      steps: string[];
      status: PreparationTask['status'];
      percent?: number;
      phaseLabel?: string;
      phase?: number;
      phaseCount?: number;
      elapsed?: number;
      phaseElapsed?: number;
      phaseIsLong?: boolean;
    } | null
  >(null);

  const refresh = async () => {
    try {
      const [p, g] = await Promise.all([getProjects(), getGroups()]);
      setProjects(p);
      setGroups(g);
      setError(null);
    } catch (e: any) {
      setError(e.message ?? 'Failed to load projects.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    getDevices().then(r => setDevices(r.devices)).catch(() => {});
  }, []);

  const submitGroup = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await addGroup(groupForm);
      setShowGroupForm(false);
      setGroupForm({ name: '', description: '' });
      await refresh();
    } catch (e: any) {
      alert(`Failed to create group: ${e.message}`);
    }
  };

  const removeGroup = async (g: ApplicationGroup) => {
    if (!confirm(`Delete the group "${g.name}"? Its ${g.project_count} project(s) will be kept and simply ungrouped.`)) return;
    try {
      await deleteGroup(g.id);
      await refresh();
    } catch (e: any) {
      alert(`Failed to delete group: ${e.message}`);
    }
  };

  /** Projects bucketed by group, ungrouped last. */
  const sections = (() => {
    const out: { key: string; group: ApplicationGroup | null; items: Project[] }[] = [];
    for (const g of groups) {
      out.push({ key: g.id, group: g, items: projects.filter(p => p.group_id === g.id) });
    }
    const ungrouped = projects.filter(p => !p.group_id);
    if (ungrouped.length) out.push({ key: UNGROUPED, group: null, items: ungrouped });
    return out;
  })();

  const setBusyFor = (id: string, action: string | null) =>
    setBusy(prev => {
      const next = { ...prev };
      if (action) next[id] = action; else delete next[id];
      return next;
    });

  // ── Actions ────────────────────────────────────────────────────────────────

  const openCreate = () => { setEditingId(null); setForm(EMPTY_FORM); setShowForm(true); };

  const openEdit = (p: Project) => {
    setEditingId(p.id);
    setForm({
      name: p.name, description: p.description ?? '', git_url: p.git_url,
      default_branch: p.default_branch, platform: p.platform, repo_type: p.repo_type,
      group_id: p.group_id,
    });
    setShowForm(true);
  };

  const submitForm = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingId) await updateProject(editingId, form);
      else await addProject(form);
      setShowForm(false);
      setEditingId(null);
      setForm(EMPTY_FORM);
      await refresh();
    } catch (e: any) {
      alert(`Failed to save project: ${e.message}`);
    }
  };

  const doDelete = async () => {
    if (!confirmDelete) return;
    try {
      await deleteProject(confirmDelete.id, deleteLocal);
      setConfirmDelete(null);
      setDeleteLocal(false);
      await refresh();
    } catch (e: any) {
      alert(`Failed to delete: ${e.message}`);
    }
  };

  // Branches per project, fetched on demand: this consumer repo has 489 of them,
  // so they are read when a card's picker is first opened, not for every card on
  // page load.
  const [branches, setBranches] = useState<Record<string, string[]>>({});
  const [pickedBranch, setPickedBranch] = useState<Record<string, string>>({});
  const [loadingBranches, setLoadingBranches] = useState<string | null>(null);
  const [pickedDevice, setPickedDevice] = useState<Record<string, string>>({});
  const [formBranches, setFormBranches] = useState<string[]>([]);
  const [loadingFormBranches, setLoadingFormBranches] = useState(false);

  const loadFormBranches = async () => {
    const url = form.git_url.trim();
    if (!url || loadingFormBranches) return;
    setLoadingFormBranches(true);
    try {
      const res = await getBranchesForUrl(url);
      setFormBranches(res.branches);
    } catch {
      setFormBranches([]);   // a bad/unreachable URL just means no suggestions
    } finally {
      setLoadingFormBranches(false);
    }
  };

  const loadBranches = async (p: Project) => {
    if (branches[p.id]) return;
    setLoadingBranches(p.id);
    try {
      const res = await getProjectBranches(p.id);
      setBranches(b => ({ ...b, [p.id]: res.branches }));
      setPickedBranch(s => ({ ...s, [p.id]: s[p.id] ?? (res.current || res.default) }));
    } catch (e: any) {
      alert(`Could not list branches: ${e.message}`);
    } finally {
      setLoadingBranches(null);
    }
  };

  const doClone = async (p: Project, force = false) => {
    setBusyFor(p.id, force ? 're-cloning' : 'cloning');
    try {
      await cloneProject(p.id, force, pickedBranch[p.id]);
      await refresh();
    } catch (e: any) {
      alert(`Clone failed: ${e.message}`);
      await refresh();
    } finally {
      setBusyFor(p.id, null);
    }
  };

  const doPull = async (p: Project) => {
    setBusyFor(p.id, 'pulling');
    try {
      await pullProject(p.id);
      await refresh();
    } catch (e: any) {
      alert(`Pull failed: ${e.message}`);
      await refresh();
    } finally {
      setBusyFor(p.id, null);
    }
  };

  /** Execute = prepare in the background (clone → validate → deps → BUILD the
   *  app → install it on the device → launch), streaming progress, then run.
   *
   *  The build can take minutes, so this polls a background task rather than
   *  blocking on one long request.
   */
  const doExecute = async (p: Project, generateYaml = false, runAfter = true) => {
    // The card's Device picker wins; "Auto" falls back to the first device on the
    // project's platform, which is what this always did.
    const chosen = pickedDevice[p.id];
    const device = (chosen && devices.find(d => d.id === chosen))
      ?? devices.find(d => d.platform?.toLowerCase() === p.platform)
      ?? devices[0];
    if (!device) return alert('No device connected. Connect a device or boot a simulator first.');

    setBusyFor(p.id, 'preparing');
    setProgress({ project: p, steps: [], status: 'running', percent: 0,
                  phaseLabel: 'Starting', phase: 0, phaseCount: 9, elapsed: 0 });

    try {
      // The card's Branch picker decides what gets built. Without this the
      // pipeline prepared whatever was already checked out, so choosing a branch
      // and pressing Execute silently rebuilt the previous one.
      await startPreparation(p.id, {
        device_id: device.id,
        generate_yaml: generateYaml,
        branch: pickedBranch[p.id],
      });

      // Poll until the pipeline finishes, streaming its steps into the modal.
      const task = await new Promise<PreparationTask>((resolve, reject) => {
        const timer = setInterval(async () => {
          try {
            const t = await getPreparationStatus(p.id);
            setProgress({
              project: p, steps: t.steps, status: t.status,
              percent: t.percent, phaseLabel: t.phase_label,
              phase: t.phase, phaseCount: t.phase_count,
              elapsed: t.elapsed_seconds, phaseElapsed: t.phase_elapsed_seconds,
              phaseIsLong: t.phase_is_long,
            });
            if (t.status === 'completed' || t.status === 'failed') {
              clearInterval(timer);
              resolve(t);
            }
          } catch (e) {
            clearInterval(timer);
            reject(e);
          }
        }, 1500);
      });

      const result = task.result;

      // automation.yaml missing → ask once, then re-run with generation enabled.
      if (result && !result.ok && result.needs_automation_yaml) {
        setProgress(null);
        setYamlPrompt(p);
        return;
      }
      if (!result || !result.ok) {
        setProgress(null);
        if (result) setPrepLog({ project: p, result });
        else alert('Preparation failed.');
        return;
      }

      setProgress(null);
      if (!runAfter) {
        alert(`${p.name}: pulled latest + rebuilt + installed. Ready to test.`);
        return;
      }
      const runId = await startRun(p.id, device.id);
      navigate(`/run/${runId}`);
    } catch (e: any) {
      setProgress(null);
      alert(`Execution failed: ${e.message}`);
    } finally {
      setBusyFor(p.id, null);
      refresh();
    }
  };

  /** Confirmed the prompt: generate automation.yaml and carry on into the run. */
  const doGenerateYaml = async () => {
    if (!yamlPrompt) return;
    const p = yamlPrompt;
    setYamlPrompt(null);
    await doExecute(p, true);
  };

  const openLocalFolder = (p: Project) => {
    // The browser cannot open a native file manager; show the path to copy.
    navigator.clipboard?.writeText(p.local_path).catch(() => {});
    alert(`Local repository path (copied to clipboard):\n\n${p.local_path}`);
  };

  // ── Render ─────────────────────────────────────────────────────────────────

  if (loading) {
    return <div className="page-header"><h1 className="page-title">Loading Projects…</h1></div>;
  }

  return (
    <div className="animate-fade-in">
      <style>{`@keyframes spin{to{transform:rotate(360deg)}} .spin{animation:spin 1s linear infinite}`}</style>

      <header className="page-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 className="page-title">Projects</h1>
          <p className="page-subtitle">Register repositories, manage clones, and execute iOS &amp; Android regression suites.</p>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <button onClick={() => setShowGroupForm(v => !v)} style={{ ...btn('rgba(255,255,255,0.08)'), padding: '10px 16px' }}>
            <Layers size={18} /> New Group
          </button>
          <button onClick={openCreate} style={{ ...btn('var(--primary)'), padding: '10px 16px' }}>
            <Plus size={18} /> Register Project
          </button>
        </div>
      </header>

      {showGroupForm && (
        <div className="card" style={{ marginBottom: 24 }}>
          <h2 style={{ fontSize: '1.1rem', marginTop: 0, marginBottom: 12 }}>New Application Group</h2>
          <form onSubmit={submitGroup} style={{ display: 'flex', gap: 12, alignItems: 'flex-end' }}>
            <div style={{ flex: 1 }}>
              <label style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>Group Name</label>
              <input required value={groupForm.name} onChange={e => setGroupForm({ ...groupForm, name: e.target.value })}
                style={inputStyle} placeholder="e.g. Food Delivery" />
            </div>
            <div style={{ flex: 2 }}>
              <label style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>Description</label>
              <input value={groupForm.description} onChange={e => setGroupForm({ ...groupForm, description: e.target.value })}
                style={inputStyle} placeholder="Optional" />
            </div>
            <button type="submit" style={{ ...btn('var(--primary)'), padding: '10px 16px' }}>Create</button>
          </form>
        </div>
      )}

      {error && (
        <div className="card" style={{ marginBottom: 24, border: '1px solid var(--danger)', backgroundColor: 'rgba(239,68,68,0.1)' }}>
          <div style={{ color: 'var(--danger)', display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertTriangle size={18} /> {error}
          </div>
        </div>
      )}

      {/* ── Create / Edit form ── */}
      {showForm && (
        <div className="card" style={{ marginBottom: 24, backgroundColor: 'rgba(59,130,246,0.05)', border: '1px solid rgba(59,130,246,0.2)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <h2 style={{ fontSize: '1.25rem', margin: 0 }}>{editingId ? 'Edit Project' : 'Register Project'}</h2>
            <button onClick={() => setShowForm(false)} style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer' }}>
              <X size={18} />
            </button>
          </div>

          <form onSubmit={submitForm} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div style={{ display: 'flex', gap: 16 }}>
              <div style={{ flex: 1 }}>
                <label style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>Project Name</label>
                <input required value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
                  style={inputStyle} placeholder="e.g. Banking iOS Regression" />
              </div>
              <div style={{ flex: 2 }}>
                <label style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>Repository URL</label>
                {/* Branches load when the URL is finished, so the Default Branch
                    field below is already a list by the time it is reached. */}
                <input required value={form.git_url}
                  onChange={e => setForm({ ...form, git_url: e.target.value })}
                  onBlur={loadFormBranches}
                  style={inputStyle} placeholder="https://github.com/org/repo.git" />
              </div>
            </div>

            <div style={{ display: 'flex', gap: 16 }}>
              <div style={{ flex: 1 }}>
                <label style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>
                  Default Branch
                  {formBranches.length > 0 && (
                    <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}> · {formBranches.length} on remote</span>
                  )}
                </label>
                {/* Branches load once a repo URL is present — before the project
                    exists there is no id to ask by, so this reads the URL itself. */}
                {formBranches.length > 0 ? (
                  <select required value={form.default_branch}
                    onChange={e => setForm({ ...form, default_branch: e.target.value })}
                    style={inputStyle}>
                    {formBranches.map(b => <option key={b} value={b}>{b}</option>)}
                  </select>
                ) : (
                  // Before a URL is entered there is nothing to list, so this stays
                  // a plain text field — and stays usable for a repo the server
                  // cannot reach.
                  <input required value={form.default_branch}
                    onChange={e => setForm({ ...form, default_branch: e.target.value })}
                    onBlur={loadFormBranches}
                    style={inputStyle}
                    placeholder={loadingFormBranches ? 'Loading branches…' : 'main'} />
                )}
              </div>
              <div style={{ flex: 1 }}>
                <label style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>Platform</label>
                <select value={form.platform} onChange={e => setForm({ ...form, platform: e.target.value as any })} style={inputStyle}>
                  <option value="ios">iOS</option>
                  <option value="android">Android</option>
                </select>
              </div>
              <div style={{ flex: 1 }}>
                <label style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>Repository Type</label>
                <select value={form.repo_type} onChange={e => setForm({ ...form, repo_type: e.target.value as any })} style={inputStyle}>
                  <option value="github">GitHub</option>
                  <option value="gitlab">GitLab</option>
                  <option value="local">Local</option>
                </select>
              </div>
            </div>

            <div style={{ display: 'flex', gap: 16 }}>
              <div style={{ flex: 1 }}>
                <label style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>Application Group</label>
                <select
                  value={form.group_id ?? ''}
                  onChange={e => setForm({ ...form, group_id: e.target.value || null })}
                  style={inputStyle}
                >
                  <option value="">No group</option>
                  {groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
                </select>
              </div>
              <div style={{ flex: 2 }}>
                <label style={{ display: 'block', marginBottom: 8, color: 'var(--text-secondary)' }}>Description</label>
                <input value={form.description} onChange={e => setForm({ ...form, description: e.target.value })}
                  style={inputStyle} placeholder="Optional" />
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
              <button type="button" onClick={() => setShowForm(false)}
                style={{ padding: '10px 16px', borderRadius: 6, background: 'transparent', color: 'var(--text-secondary)', border: '1px solid rgba(255,255,255,0.1)', cursor: 'pointer' }}>
                Cancel
              </button>
              <button type="submit" style={{ ...btn('var(--primary)'), padding: '10px 16px' }}>
                {editingId ? 'Save Changes' : 'Register & Clone'}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* ── Project cards ── */}
      {projects.length === 0 ? (
        <div className="card" style={{ textAlign: 'center', padding: 48, color: 'var(--text-muted)' }}>
          No projects registered yet. Click <strong>Register Project</strong> to add your first repository.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 28 }}>
          {sections.map(section => (
            <div key={section.key}>
              {/* Group header */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <Layers size={16} color={section.group ? 'var(--primary)' : 'var(--text-muted)'} />
                <h2 style={{ margin: 0, fontSize: '1rem' }}>
                  {section.group ? section.group.name : 'Ungrouped Projects'}
                </h2>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                  {section.items.length} app{section.items.length === 1 ? '' : 's'}
                </span>
                {section.group?.description && (
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>· {section.group.description}</span>
                )}
                {section.group && (
                  <button onClick={() => removeGroup(section.group!)}
                    title="Delete group (projects are kept)"
                    style={{ ...btn('transparent'), color: 'var(--text-muted)', padding: '2px 6px' }}>
                    <Trash2 size={13} />
                  </button>
                )}
              </div>

              {section.items.length === 0 ? (
                <div className="card" style={{ padding: 20, color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                  No projects in this group yet. Assign one by editing a project and choosing this group.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                  {section.items.map(p => {
            const action = busy[p.id];
            const isBusy = Boolean(action);
            const cloned = p.clone_status !== 'not_cloned' && p.clone_status !== 'clone_failed';

            return (
              <div key={p.id} className="card">
                {/* Header row */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
                  <div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
                      <h3 style={{ margin: 0, fontSize: '1.1rem' }}>{p.name}</h3>
                      <StatusPill status={p.clone_status} />
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', textTransform: 'uppercase' }}>
                        {p.platform}
                      </span>
                      {p.project_type_label && p.project_type !== 'unknown' && (
                        <span style={{ fontSize: '0.75rem', padding: '2px 8px', borderRadius: 4, background: 'rgba(255,255,255,0.06)', color: 'var(--text-secondary)' }}>
                          {p.project_type_label}
                        </span>
                      )}
                    </div>
                    {p.description && (
                      <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>{p.description}</div>
                    )}
                  </div>

                  <div style={{ display: 'flex', gap: 8 }}>
                    <button onClick={() => openEdit(p)} disabled={isBusy} style={btn('rgba(255,255,255,0.08)', isBusy)}>
                      <Pencil size={14} /> Edit
                    </button>
                    <button onClick={() => { setConfirmDelete(p); setDeleteLocal(false); }} disabled={isBusy}
                      style={btn('rgba(239,68,68,0.15)', isBusy)}>
                      <Trash2 size={14} /> Delete
                    </button>
                  </div>
                </div>

                {/* Repository line */}
                <div style={{ fontSize: '0.8rem', marginBottom: 12 }}>
                  <span style={{ color: 'var(--text-muted)' }}>Repository: </span>
                  <span style={{ fontFamily: 'monospace', wordBreak: 'break-all', color: 'var(--text-secondary)' }}>{p.git_url}</span>
                </div>

                {/* Project Health */}
                <ProjectHealth p={p} />

                {p.clone_status === 'clone_failed' && p.clone_error && (
                  <div style={{ marginBottom: 12, padding: 10, borderRadius: 6, background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)', color: 'var(--danger)', fontSize: '0.8rem', fontFamily: 'monospace' }}>
                    {p.clone_error}
                  </div>
                )}

                {/* Branch + device pickers. Both feed the buttons below: clone
                    checks out the chosen branch, and a run installs onto the
                    chosen device instead of whichever simulator happens to be up. */}
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 10 }}>
                  <label style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-secondary)', fontSize: 13 }}>
                    <GitBranch size={13} /> Branch
                  </label>
                  {/* A real <select>, not a datalist. A datalist renders as a text
                      box that AUTOCOMPLETES: it silently replaced the branch with
                      whichever suggestion matched, so a clone could check out a
                      branch nobody picked. A select shows every branch, and its
                      value can only be one of them. Browsers scroll and
                      type-to-jump long lists natively. */}
                  <select
                    value={pickedBranch[p.id] ?? p.current_branch ?? p.default_branch ?? ''}
                    onFocus={() => loadBranches(p)}
                    onChange={e => setPickedBranch(s => ({ ...s, [p.id]: e.target.value }))}
                    style={{ ...inputStyle, width: 260, padding: '6px 10px', fontSize: 13 }}
                  >
                    {/* The current branch is always an option, so the select has
                        something valid to show before the list has loaded. */}
                    {!branches[p.id] && (
                      <option value={pickedBranch[p.id] ?? p.current_branch ?? p.default_branch ?? ''}>
                        {loadingBranches === p.id ? 'Loading branches…'
                          : (p.current_branch ?? p.default_branch ?? 'main')}
                      </option>
                    )}
                    {(branches[p.id] ?? []).map(b => <option key={b} value={b}>{b}</option>)}
                  </select>
                  {branches[p.id] && (
                    <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                      {branches[p.id].length} branches
                    </span>
                  )}

                  <label style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-secondary)', fontSize: 13, marginLeft: 8 }}>
                    <Smartphone size={13} /> Device
                  </label>
                  <select
                    value={pickedDevice[p.id] ?? ''}
                    onChange={e => setPickedDevice(s => ({ ...s, [p.id]: e.target.value }))}
                    style={{ ...inputStyle, width: 240, padding: '6px 10px', fontSize: 13 }}
                  >
                    <option value="">Auto (first available)</option>
                    {devices
                      .filter(d => d.platform?.toLowerCase() === p.platform?.toLowerCase())
                      .map(d => (
                        <option key={d.id} value={d.id}>
                          {d.name}{d.platform_version ? ` · ${d.platform_version}` : ''}
                          {d.status ? ` · ${d.status}` : ''}
                        </option>
                      ))}
                  </select>
                </div>

                {/* Action buttons */}
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                  <button onClick={() => doClone(p)} disabled={isBusy || cloned} style={btn('rgba(255,255,255,0.08)', isBusy || cloned)}>
                    <DownloadCloud size={14} /> Clone
                  </button>
                  <button onClick={() => doPull(p)} disabled={isBusy || !cloned} style={btn('rgba(255,255,255,0.08)', isBusy || !cloned)}>
                    <RefreshCw size={14} /> Pull Latest
                  </button>
                  <button onClick={() => doClone(p, true)} disabled={isBusy} style={btn('rgba(255,255,255,0.08)', isBusy)}>
                    <RefreshCw size={14} /> Re-clone
                  </button>
                  <button onClick={() => openLocalFolder(p)} disabled={!cloned} style={btn('rgba(255,255,255,0.08)', !cloned)}>
                    <FolderOpen size={14} /> Open Local Folder
                  </button>

                  <div style={{ flex: 1 }} />

                  <button
                    onClick={() => doExecute(p, false, false)}
                    disabled={isBusy || !cloned}
                    title="Pull the latest merged code, force a rebuild, and install the fresh app — then run the flows to test it."
                    style={btn('rgba(139, 92, 246, 0.25)', isBusy || !cloned)}
                  >
                    <RefreshCw size={14} /> Pull latest &amp; rebuild
                  </button>

                  <button onClick={() => doExecute(p)} disabled={isBusy} style={btn('var(--primary)', isBusy)}>
                    {isBusy ? <Loader2 size={14} className="spin" /> : <Play size={14} />}
                    {isBusy ? `${action}…` : 'Execute'}
                  </button>
                </div>
              </div>
            );
                  })}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* ── Delete confirmation ── */}
      {confirmDelete && (
        <Modal title="Delete Project" onClose={() => setConfirmDelete(null)}>
          <p style={{ color: 'var(--text-secondary)', marginBottom: 16 }}>
            Delete <strong>{confirmDelete.name}</strong>? This removes it from the database.
          </p>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 20, color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
            <input type="checkbox" checked={deleteLocal} onChange={e => setDeleteLocal(e.target.checked)} />
            Also delete the local cloned repository
            <code style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{confirmDelete.local_path}</code>
          </label>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
            <button onClick={() => setConfirmDelete(null)}
              style={{ padding: '10px 16px', borderRadius: 6, background: 'transparent', color: 'var(--text-secondary)', border: '1px solid rgba(255,255,255,0.1)', cursor: 'pointer' }}>
              Cancel
            </button>
            <button onClick={doDelete} style={{ ...btn('var(--danger)'), padding: '10px 16px' }}>
              <Trash2 size={14} /> Delete
            </button>
          </div>
        </Modal>
      )}

      {/* ── Live preparation progress (clone → deps → build → install app) ── */}
      {progress && (
        <Modal title={`Preparing ${progress.project.name}`} onClose={() => setProgress(null)}>
          {/* Stage progress. Deliberately NOT an ETA: a cold install or Xcode
              build runs anywhere from seconds to twenty minutes depending on
              what is cached, so a predicted time would be a guess presented as
              a fact. The bar shows how far through the pipeline's stages the
              run has got, and names the stage it is in. */}
          <div style={{ marginBottom: 14 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
                {progress.status === 'running' && <Loader2 size={14} className="spin" />}
                <span>{progress.phaseLabel ?? 'Starting'}</span>
                {progress.phase != null && progress.phaseCount != null && (
                  <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
                    step {Math.min(progress.phase + 1, progress.phaseCount)} of {progress.phaseCount}
                  </span>
                )}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                {progress.elapsed != null && progress.elapsed > 0 && (
                  <span style={{ color: 'var(--text-muted)', fontSize: '0.78rem', fontVariantNumeric: 'tabular-nums' }}>
                    {Math.floor(progress.elapsed / 60)}m {progress.elapsed % 60}s
                  </span>
                )}
                <span style={{
                  color: progress.status === 'failed' ? '#f85149' : 'var(--text-secondary)',
                  fontSize: '0.85rem', fontVariantNumeric: 'tabular-nums', minWidth: 38, textAlign: 'right',
                }}>
                  {progress.percent ?? 0}%
                </span>
              </div>
            </div>

            <div style={{ height: 6, background: 'rgba(255,255,255,0.08)', borderRadius: 999, overflow: 'hidden' }}>
              <div style={{
                height: '100%',
                width: `${Math.max(2, Math.min(100, progress.percent ?? 0))}%`,
                borderRadius: 999,
                background: progress.status === 'failed' ? '#f85149'
                          : progress.status === 'completed' ? '#3fb950'
                          : 'linear-gradient(90deg, #388bfd, #58a6ff)',
                transition: 'width 400ms ease',
              }} />
            </div>

            {/* A slow stage must read as "expected", not "hung". */}
            <div style={{ marginTop: 6, color: 'var(--text-muted)', fontSize: '0.78rem', minHeight: 16 }}>
              {progress.status === 'failed'
                ? 'Stopped here — see the log below.'
                : progress.phaseIsLong
                  ? `This stage normally takes several minutes${
                      progress.phaseElapsed && progress.phaseElapsed > 30
                        ? ` — ${Math.floor(progress.phaseElapsed / 60)}m ${progress.phaseElapsed % 60}s so far`
                        : ''}.`
                  : progress.status === 'completed' ? 'Finished.' : '\u00a0'}
            </div>
          </div>
          <div style={{ background: '#0d1117', borderRadius: 6, padding: 12, fontFamily: 'monospace', fontSize: '0.75rem', color: '#c9d1d9', maxHeight: 320, overflowY: 'auto' }}>
            {progress.steps.length === 0
              ? <div style={{ color: 'var(--text-muted)' }}>Starting…</div>
              : progress.steps.map((s, i) => <div key={i}>{s}</div>)}
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
            <button onClick={() => setProgress(null)}
              style={{ padding: '10px 16px', borderRadius: 6, background: 'transparent', color: 'var(--text-secondary)', border: '1px solid rgba(255,255,255,0.1)', cursor: 'pointer' }}>
              Hide (keeps running)
            </button>
          </div>
        </Modal>
      )}

      {/* ── automation.yaml missing prompt ── */}
      {yamlPrompt && (
        <Modal title="automation.yaml not found" onClose={() => setYamlPrompt(null)}>
          <p style={{ color: 'var(--text-secondary)', marginBottom: 8 }}>
            <strong>{yamlPrompt.name}</strong> has no <code>automation.yaml</code> in its repository root.
            Generate one based on the detected project type?
          </p>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 20 }}>
            The file will be written to the repository root and execution will continue automatically.
          </p>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
            <button onClick={() => setYamlPrompt(null)}
              style={{ padding: '10px 16px', borderRadius: 6, background: 'transparent', color: 'var(--text-secondary)', border: '1px solid rgba(255,255,255,0.1)', cursor: 'pointer' }}>
              Cancel
            </button>
            <button onClick={doGenerateYaml} style={{ ...btn('var(--primary)'), padding: '10px 16px' }}>
              Generate &amp; Execute
            </button>
          </div>
        </Modal>
      )}

      {/* ── Preparation / validation failure detail ── */}
      {prepLog && (
        <Modal title="Preparation failed" onClose={() => setPrepLog(null)}>
          <div style={{ color: 'var(--danger)', marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertTriangle size={16} /> {prepLog.result.error}
          </div>

          {prepLog.result.validation?.issues?.length ? (
            <div style={{ marginBottom: 16 }}>
              {prepLog.result.validation.issues.map((i, idx) => (
                <div key={idx} style={{ marginBottom: 10, padding: 10, borderRadius: 6, background: 'rgba(239,68,68,0.08)' }}>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>{i.check}</div>
                  <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>{i.problem}</div>
                  <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: 4 }}>Fix: {i.resolution}</div>
                </div>
              ))}
            </div>
          ) : null}

          <div style={{ background: '#0d1117', borderRadius: 6, padding: 12, fontFamily: 'monospace', fontSize: '0.75rem', color: '#c9d1d9', maxHeight: 220, overflowY: 'auto' }}>
            {prepLog.result.steps.map((s, i) => <div key={i}>{s}</div>)}
          </div>

          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 20 }}>
            <button onClick={() => setPrepLog(null)} style={{ ...btn('var(--primary)'), padding: '10px 16px' }}>Close</button>
          </div>
        </Modal>
      )}

      {devices.length === 0 && (
        <div style={{ marginTop: 24, display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-muted)', fontSize: '0.85rem' }}>
          <Smartphone size={14} /> No devices connected — Execute will be unavailable until a device or simulator is online.
        </div>
      )}
    </div>
  );
}

// ── Small modal shell ────────────────────────────────────────────────────────

function Modal({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  return (
    <ModalPortal onClose={onClose}>
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(4px)', display: 'flex',
        alignItems: 'center', justifyContent: 'center', zIndex: 1000, padding: 16,
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        className="card modal-pop"
        style={{ maxWidth: 560, width: '100%', maxHeight: '85vh', overflowY: 'auto' }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <h2 style={{ margin: 0, fontSize: '1.15rem' }}>{title}</h2>
          <button onClick={onClose} style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer' }}>
            <X size={18} />
          </button>
        </div>
        {children}
      </div>
    </div>
    </ModalPortal>
  );
}
