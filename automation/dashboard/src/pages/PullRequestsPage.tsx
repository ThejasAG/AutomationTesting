import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { GitPullRequest, Loader2, Play, ChevronDown, ExternalLink, AlertTriangle } from 'lucide-react';
import { getProjects, getPullRequests, testPullRequest, planPullRequest, autotestPullRequest } from '../api';
import type { PRTestPlan } from '../api';
import ModalPortal from '../components/ModalPortal';
import { Sparkles, X, CheckCircle2, ListChecks } from 'lucide-react';
import type { Project, PullRequest } from '../api';
import ScenarioPanel from '../components/ScenarioPanel';

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
  const [plan, setPlan] = useState<PRTestPlan | null>(null);

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
      const { run_id } = await testPullRequest(projectId, pr.number);
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
      </div>

      {error && (
        <div className="card" style={{ marginBottom: '20px', border: '1px solid var(--danger)', background: 'rgba(239,68,68,0.08)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', color: 'var(--danger)' }}>
            <AlertTriangle size={16} /> {error}
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
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {prs.map(pr => (
            <div key={pr.number} className="card" style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '4px' }}>
                  <span style={{ color: 'var(--text-muted)', fontFamily: "'Fira Code', monospace", fontSize: '0.85rem' }}>#{pr.number}</span>
                  <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{pr.title}</span>
                  {pr.draft && <span className="badge" style={{ background: 'rgba(255,255,255,0.06)', color: 'var(--text-muted)' }}>draft</span>}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '14px', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                  <span>by {pr.author}</span>
                  <span style={{ fontFamily: "'Fira Code', monospace" }}>{pr.branch} → {pr.base}</span>
                  <a href={pr.url} target="_blank" rel="noreferrer" style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', color: 'var(--accent-primary)', textDecoration: 'none' }}>
                    <ExternalLink size={12} /> GitHub
                  </a>
                </div>
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
                  : <><Play size={13} /> Test this PR</>}
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
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {plan.selected_scenarios.map(s => (
                    <div key={s.id} style={{ border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: 12 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: '0.88rem' }}><CheckCircle2 size={14} color="var(--success)" /> {s.name}</div>
                      <div style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginTop: 4 }}>{s.reason}</div>
                    </div>
                  ))}
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

      <style>{`@keyframes sp { from { transform: rotate(0) } to { transform: rotate(360deg) } }`}</style>
    </div>
  );
}
