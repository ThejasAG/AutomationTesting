import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { GitPullRequest, Loader2, Play, ChevronDown, ExternalLink, AlertTriangle, Search } from 'lucide-react';
import { getProjects, getPullRequests, testPullRequest, planPullRequest, autotestPullRequest, getScenarioDevices } from '../api';
import type { PRTestPlan } from '../api';
import ModalPortal from '../components/ModalPortal';
import { Sparkles, X, CheckCircle2, ListChecks, Pencil } from 'lucide-react';
import type { Project, PullRequest, SimDevice } from '../api';
import ScenarioPanel from '../components/ScenarioPanel';
import ScenarioEditorModal from '../components/ScenarioEditorModal';
import type { ScenarioInput } from '../api';

export default function PullRequestsPage() {
  const navigate = useNavigate();

  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState('');
  const [repo, setRepo] = useState('');
  const [prs, setPrs] = useState<PullRequest[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState<number | null>(null);
  const [planning, setPlanning] = useState<number | null>(null);
  // Scenario opened for editing from inside the PR plan.
  const [editingScenario, setEditingScenario] = useState<(ScenarioInput & { id?: string }) | null>(null);
  const [plan, setPlan] = useState<PRTestPlan | null>(null);
  const [query, setQuery] = useState('');
  // Which simulator PR tests run on. A run once landed on a brand-new simulator stuck
  // on first-run onboarding and cycled for 10 minutes, so this is an explicit choice.
  const [sims, setSims] = useState<SimDevice[]>([]);
  const [deviceId, setDeviceId] = useState<string>(() => localStorage.getItem('pr_test_device') || '');

  // Search by PR number, title, author, or branch (the branch carries the ticket,
  // e.g. "NEWVYA-1134-1-5"). Space-separated terms must all match (AND).
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const filtered = terms.length === 0 ? prs : prs.filter(pr => {
    const hay = `#${pr.number} ${pr.title} ${pr.author} ${pr.branch} ${pr.base}`.toLowerCase();
    return terms.every(t => hay.includes(t));
  });

  const handlePlan = async (number: number) => {
    setPlanning(number); setError(null);
    try { setPlan(await planPullRequest(projectId, number)); }
    catch (e: any) { setError(e?.message || 'Could not plan tests for this PR'); }
    setPlanning(null);
  };

  const [autoMsg, setAutoMsg] = useState<string | null>(null);
  const handleAutotest = async (number: number) => {
    setAutoMsg('Starting…');
    try { const r = await autotestPullRequest(projectId, number); setAutoMsg(r.message); }
    catch (e: any) { setAutoMsg(e?.message || 'Could not start'); }
  };

  useEffect(() => {
    getScenarioDevices().then(r => setSims(r.simulators)).catch(() => {});
  }, []);

  // Only GitHub projects can list PRs.
  useEffect(() => {
    getProjects()
      .then(list => {
        const gh = list.filter(p => (p.git_url || '').includes('github.com'));
        setProjects(gh);
        if (gh.length) setProjectId(gh[0].id);
      })
      .catch(e => setError(e.message));
  }, []);

  useEffect(() => {
    if (!projectId) return;
    setLoading(true);
    setError(null);
    setPrs([]);
    getPullRequests(projectId)
      .then(data => { setRepo(data.repo); setPrs(data.pull_requests); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [projectId]);

  const handleTest = async (pr: PullRequest) => {
    setTesting(pr.number);
    try {
      const { run_id } = await testPullRequest(projectId, pr.number, deviceId || undefined);
      navigate(`/run/${run_id}`);
    } catch (e: any) {
      setError(e.message);
      setTesting(null);
    }
  };

  return (
    <div className="animate-fade-in">
      <header className="page-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <GitPullRequest size={28} color="var(--accent-primary)" />
          <div>
            <h1 className="page-title" style={{ margin: 0 }}>Pull Requests</h1>
            <p className="page-subtitle" style={{ marginBottom: 0 }}>
              Test an open PR against a device before it merges — no waiting for the merge webhook.
            </p>
          </div>
        </div>
      </header>

      {/* Project picker */}
      <div className="card" style={{ marginBottom: '20px', display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
        <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
          Repository
        </label>
        <div style={{ position: 'relative', minWidth: 260 }}>
          <select
            value={projectId}
            onChange={e => setProjectId(e.target.value)}
            style={{
              width: '100%', background: 'rgba(255,255,255,0.05)', border: '1px solid var(--border-color)',
              borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)', padding: '9px 32px 9px 12px',
              fontSize: '0.9rem', appearance: 'none', cursor: 'pointer', fontFamily: 'inherit',
            }}
          >
            {projects.length === 0 && <option value="">No GitHub projects</option>}
            {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <ChevronDown size={14} style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
        </div>
        {repo && <code style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>{repo}</code>}
        <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600, marginLeft: 'auto' }}>
          Run on
        </label>
        <select
          value={deviceId}
          onChange={e => { setDeviceId(e.target.value); localStorage.setItem('pr_test_device', e.target.value); }}
          title="Which simulator PR tests run on. A freshly-created simulator sits on the first-run onboarding screen and no scenario can pass on it."
          style={{
            background: 'rgba(255,255,255,0.05)', border: '1px solid var(--border-color)',
            borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)',
            padding: '9px 12px', fontSize: '0.85rem', cursor: 'pointer', fontFamily: 'inherit', minWidth: 230,
          }}
        >
          <option value="">Auto (PR_TEST_IOS_DEVICE, else any online)</option>
          {sims.map(s => (
            <option key={s.udid} value={s.udid}>
              {s.name}{s.state === 'Booted' ? ' ● booted' : ' (shut down)'}
            </option>
          ))}
        </select>
      </div>

      {error && (
        <div className="card" style={{ marginBottom: '20px', border: '1px solid var(--danger)', background: 'rgba(239,68,68,0.08)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', color: 'var(--danger)' }}>
            <AlertTriangle size={16} /> {error}
          </div>
        </div>
      )}

      {/* Search / ticket filter */}
      {!loading && prs.length > 0 && (
        <div style={{ position: 'relative', marginBottom: '16px' }}>
          <Search size={16} style={{ position: 'absolute', left: 14, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)', pointerEvents: 'none' }} />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search by ticket, PR #, title, author or branch (e.g. NEWVYA-1134, #540, payment)…"
            style={{
              width: '100%', boxSizing: 'border-box',
              background: 'rgba(255,255,255,0.05)', border: '1px solid var(--border-color)',
              borderRadius: 'var(--radius-sm)', color: 'var(--text-primary)',
              padding: '11px 38px 11px 40px', fontSize: '0.9rem', fontFamily: 'inherit',
            }}
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              title="Clear"
              style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', display: 'flex' }}
            >
              <X size={15} />
            </button>
          )}
          <div style={{ marginTop: 6, fontSize: '0.72rem', color: 'var(--text-muted)' }}>
            {query ? `${filtered.length} of ${prs.length} match` : `${prs.length} pull request${prs.length === 1 ? '' : 's'}`}
          </div>
        </div>
      )}

      {/* PR list */}
      {loading ? (
        <div className="card" style={{ display: 'flex', alignItems: 'center', gap: '10px', color: 'var(--text-muted)' }}>
          <Loader2 size={16} style={{ animation: 'sp 1s linear infinite' }} /> Loading open pull requests…
        </div>
      ) : !error && prs.length === 0 && projectId ? (
        <div className="card" style={{ color: 'var(--text-muted)', textAlign: 'center', padding: '40px' }}>
          <GitPullRequest size={40} style={{ opacity: 0.25, marginBottom: 12 }} />
          <p>No open pull requests on this repository.</p>
        </div>
      ) : !error && filtered.length === 0 && prs.length > 0 ? (
        <div className="card" style={{ color: 'var(--text-muted)', textAlign: 'center', padding: '40px' }}>
          <Search size={40} style={{ opacity: 0.25, marginBottom: 12 }} />
          <p>No pull request matches <strong style={{ color: 'var(--text-secondary)' }}>{query}</strong>.</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {filtered.map(pr => (
            <div key={pr.number} className="card" style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '4px' }}>
                  <span style={{ color: 'var(--text-muted)', fontFamily: "'Fira Code', monospace", fontSize: '0.85rem' }}>#{pr.number}</span>
                  <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{pr.title}</span>
                  {pr.draft && <span className="badge" style={{ background: 'rgba(255,255,255,0.06)', color: 'var(--text-muted)' }}>draft</span>}
                  {pr.state === 'merged'
                    ? <span className="badge" style={{ background: 'rgba(139,92,246,0.18)', color: '#a78bfa' }}>merged</span>
                    : <span className="badge" style={{ background: 'rgba(52,211,153,0.15)', color: 'var(--success, #34d399)' }}>open</span>}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '14px', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                  <span>by {pr.author}</span>
                  <span style={{ fontFamily: "'Fira Code', monospace" }}>{pr.branch} → {pr.base}</span>
                  <a href={pr.url} target="_blank" rel="noreferrer" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', color: 'var(--accent-primary)', textDecoration: 'none' }}>
                    <ExternalLink size={12} /> GitHub
                  </a>
                </div>
                {pr.ticket_key && (
                  <div style={{ marginTop: 8, padding: '8px 10px', borderRadius: 6,
                                background: 'rgba(59,130,246,0.06)', border: '1px solid rgba(59,130,246,0.18)' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.78rem', flexWrap: 'wrap' }}>
                      <span style={{ color: '#60a5fa', fontWeight: 600, fontFamily: "'Fira Code', monospace" }}>📋 {pr.ticket_key}</span>
                      {pr.ticket_status && <span className="badge" style={{ background: 'rgba(255,255,255,0.06)', color: 'var(--text-muted)' }}>{pr.ticket_status}</span>}
                      {pr.ticket_source === 'jira' && <span style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>live from Jira</span>}
                      {pr.ticket_title && <span style={{ color: 'var(--text-primary)' }}>{pr.ticket_title}</span>}
                      {pr.ticket_url && <a href={pr.ticket_url} target="_blank" rel="noreferrer" style={{ color: 'var(--accent-primary)', textDecoration: 'none', display: 'inline-flex', alignItems: 'center', gap: 3 }}><ExternalLink size={11} /> open</a>}
                    </div>
                    {pr.ticket_description && (
                      <details style={{ marginTop: 6 }}>
                        <summary style={{ cursor: 'pointer', fontSize: '0.72rem', color: 'var(--text-muted)' }}>Description</summary>
                        <div style={{ marginTop: 4, fontSize: '0.76rem', color: 'var(--text-secondary)', whiteSpace: 'pre-wrap', maxHeight: 220, overflow: 'auto' }}>{pr.ticket_description}</div>
                      </details>
                    )}
                  </div>
                )}
              </div>
              <button
                onClick={() => handlePlan(pr.number)}
                disabled={planning !== null}
                title="Use AI to decide what to test and how to reach it"
                style={{
                  padding: '8px 14px', fontSize: '0.85rem', flexShrink: 0, cursor: 'pointer',
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                  background: 'transparent', border: '1px solid var(--accent-primary)',
                  borderRadius: 'var(--radius-sm)', color: 'var(--accent-primary)',
                  opacity: planning !== null && planning !== pr.number ? 0.4 : 1,
                }}
              >
                {planning === pr.number
                  ? <><Loader2 size={13} style={{ animation: 'sp 1s linear infinite' }} /> Planning…</>
                  : <><Sparkles size={13} /> Plan tests</>}
              </button>
              <button
                className="btn"
                onClick={() => handleTest(pr)}
                disabled={testing !== null}
                style={{
                  padding: '8px 16px', fontSize: '0.85rem', flexShrink: 0,
                  background: 'linear-gradient(135deg, #059669, #10b981)',
                  opacity: testing !== null && testing !== pr.number ? 0.4 : 1,
                }}
              >
                {testing === pr.number
                  ? <><Loader2 size={13} style={{ animation: 'sp 1s linear infinite' }} /> Queuing…</>
                  : <><Play size={13} /> {pr.state === 'merged' ? 'Test merge' : 'Test this PR'}</>}
              </button>
            </div>
          ))}
        </div>
      )}

      {plan && (
        <ModalPortal onClose={() => setPlan(null)}>
          <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)', backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 999, padding: 16 }} onClick={() => setPlan(null)}>
            <div className="card modal-pop" style={{ width: 640, maxWidth: '95vw', maxHeight: '90vh', overflowY: 'auto', padding: 26, position: 'relative' }} onClick={e => e.stopPropagation()}>
              <button onClick={() => setPlan(null)} style={{ position: 'absolute', top: 16, right: 16, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex' }}><X size={18} /></button>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                <Sparkles size={18} color="var(--accent-primary)" />
                <h3 style={{ margin: 0 }}>Test plan — PR #{plan.pr_number}</h3>
              </div>
              <p style={{ color: 'var(--text-secondary)', fontSize: '0.86rem', marginTop: 4 }}>{plan.summary || plan.title}</p>

              {plan.affected_areas.length > 0 && (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', margin: '10px 0' }}>
                  {plan.affected_areas.map((a, i) => <span key={i} className="badge" style={{ background: 'rgba(99,102,241,0.14)', color: 'var(--accent-primary)' }}>{a}</span>)}
                </div>
              )}

              {plan.graph_driven && (
                <div style={{ border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: 12, margin: '12px 0', fontSize: '0.83rem' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                    <span style={{ fontWeight: 600 }}>Smart selection (from dependency graph)</span>
                    {typeof plan.reduction_pct === 'number' && (
                      <span className="badge" style={{ background: 'var(--success-bg, rgba(52,211,153,0.15))', color: 'var(--success)' }}>
                        {plan.reduction_pct}% fewer tests
                      </span>
                    )}
                  </div>
                  <div style={{ color: 'var(--text-secondary)', marginBottom: 6 }}>
                    Affected modules: {(plan.affected_modules || []).slice(0, 10).map(m => (
                      <code key={m} style={{ background: 'rgba(255,255,255,0.05)', padding: '1px 5px', borderRadius: 4, marginRight: 4 }}>{m}</code>
                    ))}
                  </div>
                  {plan.skipped_scenarios && plan.skipped_scenarios.length > 0 && (
                    <div style={{ color: 'var(--text-muted)', fontSize: '0.78rem' }}>
                      Skipped (not affected): {plan.skipped_scenarios.map(s => s.name).join(', ')}
                    </div>
                  )}
                </div>
              )}

              {plan.path_explanation && (
                <div style={{ border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: 12, margin: '12px 0', fontSize: '0.85rem', lineHeight: 1.5 }}>
                  <div style={{ fontWeight: 600, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}><ListChecks size={14} /> How to reach it</div>
                  {plan.path_explanation}
                </div>
              )}

              <div style={{ fontWeight: 600, fontSize: '0.88rem', margin: '14px 0 8px' }}>Scenarios to run ({plan.selected_scenarios.length})</div>
              {plan.selected_scenarios.length === 0 ? (
                <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', background: 'rgba(251,191,36,0.1)', border: '1px solid rgba(251,191,36,0.3)', borderRadius: 'var(--radius-sm)', padding: 12, fontSize: '0.85rem' }}>
                  <AlertTriangle size={15} color="#fbbf24" style={{ flexShrink: 0, marginTop: 2 }} />
                  <div><strong>No saved scenario covers this yet.</strong><br />{plan.missing_coverage || 'Record a scenario that reaches this feature, then re-plan.'}</div>
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  {/* Cross-app verification split into the three roles — each runs on its own app. */}
                  {([
                    { key: 'consumer', label: '🧑 Consumer', app: 'diner app', color: '#34d399' },
                    { key: 'waiter', label: '🧑‍🍳 Waiter', app: 'Business iPad', color: '#60a5fa' },
                    { key: 'kitchen', label: '🍳 Kitchen', app: 'Business iPad', color: '#fbbf24' },
                  ] as const).map(role => {
                    const items = plan.by_role
                      ? plan.by_role[role.key]
                      : (role.key === 'consumer' ? plan.selected_scenarios : []);
                    if (!items || items.length === 0) return null;
                    return (
                      <div key={role.key}>
                        <div style={{ fontSize: '0.78rem', fontWeight: 700, color: role.color, marginBottom: 6, letterSpacing: 0.3 }}>
                          {role.label} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>· {role.app} · {items.length} path{items.length > 1 ? 's' : ''}</span>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, borderLeft: `2px solid ${role.color}`, paddingLeft: 10 }}>
                          {items.map(s => (
                            <div key={s.id} style={{ border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: 10 }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: '0.86rem' }}>
                                <CheckCircle2 size={13} color="var(--success)" />
                                <span style={{ flex: 1 }}>{s.name}</span>
                                {/* Edit the scenario right where the PR plan shows it — no
                                    detour to the Scenarios page and back. */}
                                <button onClick={() => setEditingScenario({
                                  id: s.id, name: s.name, description: '', project_id: projectId,
                                  device_id: '', steps: s.steps || [], covers: [],
                                })}
                                  title="Edit this scenario's steps"
                                  style={{ display: 'inline-flex', alignItems: 'center', gap: 4, background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 6, color: 'var(--text-secondary)', cursor: 'pointer', padding: '2px 8px', fontSize: '0.72rem' }}>
                                  <Pencil size={11} /> Edit
                                </button>
                              </div>
                              <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginTop: 3 }}>{s.reason}</div>
                              {!!(s.steps && s.steps.length) && (
                                <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', fontFamily: 'monospace', marginTop: 4, lineHeight: 1.5 }}>
                                  {s.steps.join(' → ')}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                  <button className="btn" onClick={() => handleAutotest(plan.pr_number)}
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '9px 18px', marginTop: 4 }}>
                    <Play size={14} /> Run this plan (build → test → comment on PR)
                  </button>
                  {autoMsg && <div style={{ fontSize: '0.8rem', color: 'var(--accent-primary)' }}>{autoMsg}</div>}
                  <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>Builds the PR branch, runs these scenarios, and posts the verdict as a PR comment.</div>
                </div>
              )}

              {plan.changed_files.length > 0 && (
                <div style={{ marginTop: 16, fontSize: '0.76rem', color: 'var(--text-muted)' }}>
                  Changed files: {plan.changed_files.slice(0, 6).map(f => <code key={f} style={{ background: 'rgba(255,255,255,0.05)', padding: '1px 5px', borderRadius: 4, marginRight: 4 }}>{f}</code>)}
                </div>
              )}
            </div>
          </div>
        </ModalPortal>
      )}

      {projectId && (
        <div style={{ marginTop: 24 }}>
          <ScenarioPanel projectId={projectId} />
        </div>
      )}

      {editingScenario && (
        <ScenarioEditorModal
          initial={editingScenario}
          isNew={false}
          onClose={() => setEditingScenario(null)}
          onSaved={() => { setEditingScenario(null); if (plan) handlePlan(plan.pr_number); }}
        />
      )}

      <style>{`@keyframes sp { from { transform: rotate(0) } to { transform: rotate(360deg) } }`}</style>
    </div>
  );
}
