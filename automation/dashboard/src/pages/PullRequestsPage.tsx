import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { GitPullRequest, Loader2, Play, ChevronDown, ExternalLink, AlertTriangle } from 'lucide-react';
import { getProjects, getPullRequests, testPullRequest } from '../api';
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

      {projectId && (
        <div style={{ marginTop: 24 }}>
          <ScenarioPanel projectId={projectId} />
        </div>
      )}

      <style>{`@keyframes sp { from { transform: rotate(0) } to { transform: rotate(360deg) } }`}</style>
    </div>
  );
}
