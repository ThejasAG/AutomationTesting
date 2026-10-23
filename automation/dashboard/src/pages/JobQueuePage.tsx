import { useCallback, useEffect, useState } from 'react';
import type { ReactElement } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Loader2, Clock, PlayCircle, CheckCircle2, XCircle, RefreshCw, Smartphone, GitBranch,
  Power, AlertTriangle,
} from 'lucide-react';
import { getRuns, getRunnerState, setRunnerRunning } from '../api';
import type { TestRun, RunnerState } from '../api';
import { parseServerDate } from '../time';

/** Job queue — what is waiting, what is executing right now, and what finished.
 *
 *  The runs list mixed every state together, so "is anything actually running?"
 *  and "did those queued jobs ever get picked up?" could not be answered at a
 *  glance — 10 jobs sat queued for days with a stopped agent and nothing showed it.
 *  Grouped by job_state, newest first, refreshing while work is in flight. */

type Group = { key: string; label: string; states: string[]; color: string; icon: ReactElement };

// job_state values the backend actually emits, folded into the four a person cares about.
const GROUPS: Group[] = [
  { key: 'queued', label: 'Queued', states: ['queued', 'assigned'], color: '#fbbf24',
    icon: <Clock size={16} /> },
  { key: 'running', label: 'Running', states: ['running', 'preparing', 'downloading', 'building', 'collecting_evidence'], color: '#60a5fa',
    icon: <PlayCircle size={16} /> },
  { key: 'passed', label: 'Passed', states: ['passed', 'completed'], color: '#34d399',
    icon: <CheckCircle2 size={16} /> },
  // 'no_tests' belongs here, NOT in Passed: the app deployed but nothing was verified.
  // The chip shows the real job_state, so it stays distinguishable from a real failure.
  { key: 'failed', label: 'Failed / not verified', states: ['failed', 'cancelled', 'no_tests'], color: '#f87171',
    icon: <XCircle size={16} /> },
];

const ago = (iso?: string | null) => {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - parseServerDate(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};

/** The job_state values a run passes through, in order. A queued job is at the
 *  start of this pipeline and a finished one is off the end of it — so the bar
 *  answers "how far along is this?", which a status chip alone never did. */
const STAGES = ['queued', 'assigned', 'downloading', 'preparing', 'running', 'collecting_evidence'];
const STAGE_LABEL: Record<string, string> = {
  queued: 'Waiting for an agent',
  assigned: 'Claimed by agent',
  downloading: 'Fetching the repo',
  preparing: 'Installing dependencies',
  running: 'Executing tests',
  collecting_evidence: 'Collecting evidence',
};

/** A job in flight for longer than this is not progressing — the agent that
 *  claimed it died mid-run. The queue cannot recover it on its own (poll_job
 *  only hands out `queued`), so it would otherwise read as "running" forever. */
const STALLED_AFTER_MS = 60 * 60 * 1000;

function JobProgress({ state, color, since }: { state: string; color: string; since?: string | null }) {
  const i = STAGES.indexOf(state);
  if (i < 0) return null;
  // Half a step past the last completed stage: the current one is in progress,
  // not finished, so the bar must never read 100% while work is still going.
  const pct = ((i + 0.5) / STAGES.length) * 100;
  const age = since ? Date.now() - parseServerDate(since).getTime() : 0;
  const stalled = state !== 'queued' && age > STALLED_AFTER_MS;
  const bar = stalled ? '#fbbf24' : color;
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ height: 4, borderRadius: 4, background: 'rgba(255,255,255,0.08)', overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: bar, borderRadius: 4, transition: 'width .4s ease' }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4, fontSize: '0.68rem', color: stalled ? '#fbbf24' : 'var(--text-muted)' }}>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
          {stalled && <AlertTriangle size={11} />}
          {STAGE_LABEL[state] || state}
          {stalled && ` — stalled, no agent has touched this in ${ago(since)}`}
        </span>
        <span>step {i + 1} of {STAGES.length}</span>
      </div>
    </div>
  );
}

/** On/off for the execution agent — the worker that drains this queue.
 *  Nothing here runs without it, and until now it could only be started from a
 *  terminal, so a stopped agent looked exactly like an idle platform. */
function AgentSwitch({ queued, onChange }: { queued: number; onChange: () => void }) {
  const [state, setState] = useState<RunnerState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try { setState(await getRunnerState()); } catch { /* leave the last known state */ }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    const t = window.setInterval(refresh, 5000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const toggle = async () => {
    if (busy || !state) return;
    setBusy(true);
    setError('');
    try {
      setState(await setRunnerRunning(!state.running));
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not switch the agent');
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const on = state?.running ?? false;
  const color = on ? '#34d399' : 'var(--text-muted)';

  return (
    <div className="card" style={{ padding: '14px 16px', marginBottom: 20, borderLeft: `3px solid ${color}` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <button onClick={toggle} disabled={busy || !state} title={on ? 'Stop the agent' : 'Start the agent'}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 8, cursor: busy ? 'wait' : 'pointer',
            border: 'none', background: 'transparent', padding: 0, color: 'inherit',
          }}>
          <span style={{
            width: 46, height: 26, borderRadius: 20, background: on ? '#34d399' : 'rgba(255,255,255,0.12)',
            position: 'relative', transition: 'background .2s ease', display: 'inline-block', flexShrink: 0,
          }}>
            <span style={{
              position: 'absolute', top: 3, left: on ? 23 : 3, width: 20, height: 20, borderRadius: '50%',
              background: '#fff', transition: 'left .2s ease',
            }} />
          </span>
          <span style={{ fontWeight: 600, fontSize: '0.9rem', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            {busy ? <Loader2 size={14} className="spin" /> : <Power size={14} color={color} />}
            Agent {busy ? '…' : on ? 'on' : 'off'}
          </span>
        </button>

        <div style={{ flex: 1, minWidth: 200, fontSize: '0.76rem', color: 'var(--text-muted)' }}>
          {on
            ? <>Polling for jobs{state?.pid ? ` · pid ${state.pid}` : ''}. {queued > 0 ? `Draining ${queued} queued.` : 'Queue is empty.'}</>
            : <>Stopped — queued jobs will not run until this is on.</>}
          {state?.last_log && (
            <div style={{ marginTop: 3, fontFamily: 'monospace', fontSize: '0.68rem', opacity: 0.75, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {state.last_log.slice(-160)}
            </div>
          )}
        </div>
      </div>

      {!on && queued > 0 && (
        <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 7, fontSize: '0.78rem', color: '#fbbf24' }}>
          <AlertTriangle size={14} />
          {queued} job{queued === 1 ? '' : 's'} waiting with the agent off — nothing is draining them.
        </div>
      )}
      {error && (
        <div style={{ marginTop: 10, fontSize: '0.78rem', color: 'var(--danger)' }}>{error}</div>
      )}
    </div>
  );
}

export default function JobQueuePage() {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<TestRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      setRuns(await getRuns());
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load runs');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const inFlight = runs.some(r => GROUPS[0].states.concat(GROUPS[1].states)
    .includes(r.job_state || ''));

  // Poll only while something is queued or running — a settled board does not need it.
  useEffect(() => {
    if (!inFlight) return;
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [inFlight, load]);

  const bucket = (g: Group) => runs
    .filter(r => g.states.includes(r.job_state || ''))
    .sort((a, b) => (b.started_at || b.created_at || '').localeCompare(a.started_at || a.created_at || ''));

  return (
    <div className="animate-fade-in">
      <header className="page-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <PlayCircle size={28} color="var(--accent-primary)" />
          <div style={{ flex: 1 }}>
            <h1 className="page-title" style={{ margin: 0 }}>Job Queue</h1>
            <p className="page-subtitle" style={{ marginBottom: 0 }}>
              What is waiting, what is executing right now, and what finished.
              {inFlight && ' Refreshing every 5s while work is in flight.'}
            </p>
          </div>
          <button className="btn" onClick={load}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '8px 14px' }}>
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </header>

      {error && (
        <div className="card" style={{ marginBottom: 20, border: '1px solid var(--danger)', background: 'rgba(239,68,68,0.08)', color: 'var(--danger)' }}>
          {error}
        </div>
      )}

      <AgentSwitch queued={bucket(GROUPS[0]).length} onChange={load} />

      {loading ? (
        <div className="card" style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-muted)' }}>
          <Loader2 size={16} className="spin" /> Loading jobs…
        </div>
      ) : (
        <>
          {/* Tally first — the summary a person scans before the detail. */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginBottom: 24 }}>
            {GROUPS.map(g => (
              <div key={g.key} className="card" style={{ padding: '14px 16px', borderTop: `2px solid ${g.color}` }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 7, color: g.color, fontSize: '0.74rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                  {g.icon} {g.label}
                </div>
                <div style={{ fontSize: '2rem', fontWeight: 700, marginTop: 4, fontVariantNumeric: 'tabular-nums' }}>
                  {bucket(g).length}
                </div>
              </div>
            ))}
          </div>

          {GROUPS.map(g => {
            const items = bucket(g);
            return (
              <section key={g.key} style={{ marginBottom: 28 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
                  <span style={{ color: g.color, display: 'flex' }}>{g.icon}</span>
                  <h2 style={{ margin: 0, fontSize: '1rem' }}>{g.label}</h2>
                  <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>({items.length})</span>
                </div>

                {items.length === 0 ? (
                  <div className="card" style={{ color: 'var(--text-muted)', fontSize: '0.85rem', padding: '14px 16px' }}>
                    {g.key === 'queued' ? 'Nothing waiting.'
                      : g.key === 'running' ? 'Nothing executing right now.'
                      : `No ${g.label.toLowerCase()} jobs.`}
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {items.slice(0, 25).map(r => (
                      <div key={r.id} className="card" onClick={() => navigate(`/run/${r.id}`)}
                        style={{ padding: '12px 16px', cursor: 'pointer', borderLeft: `3px solid ${g.color}` }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                          <span style={{ fontWeight: 600, fontSize: '0.88rem', flex: 1, minWidth: 220 }}>
                            {r.test_name}
                          </span>
                          {g.key === 'running' && <Loader2 size={13} className="spin" style={{ color: g.color }} />}
                          <span style={{ fontSize: '0.7rem', padding: '2px 8px', borderRadius: 20, background: 'rgba(255,255,255,0.06)', color: g.color, fontWeight: 600 }}>
                            {r.job_state}
                          </span>
                        </div>
                        <div style={{ display: 'flex', gap: 14, marginTop: 5, flexWrap: 'wrap', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                          <span>{r.test_suite}</span>
                          {r.branch && (
                            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                              <GitBranch size={11} /> {r.branch}
                            </span>
                          )}
                          {r.device_name && (
                            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                              <Smartphone size={11} /> {r.device_name.slice(0, 8)}
                            </span>
                          )}
                          <span>{ago(r.started_at || r.created_at)}</span>
                          {r.attempts && r.attempts > 1 && <span>· {r.attempts} attempts</span>}
                        </div>
                        {(g.key === 'running' || g.key === 'queued') && (
                          <JobProgress state={r.job_state || ''} color={g.color}
                            since={r.started_at || r.created_at} />
                        )}
                        {r.error_message && (
                          <div style={{ marginTop: 6, fontSize: '0.74rem', color: '#f87171' }}>
                            {r.error_message.slice(0, 180)}
                          </div>
                        )}
                      </div>
                    ))}
                    {items.length > 25 && (
                      <div style={{ color: 'var(--text-muted)', fontSize: '0.76rem', padding: '4px 2px' }}>
                        …and {items.length - 25} more
                      </div>
                    )}
                  </div>
                )}
              </section>
            );
          })}
        </>
      )}
    </div>
  );
}
