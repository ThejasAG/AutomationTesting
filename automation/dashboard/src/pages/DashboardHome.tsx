import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getRuns, getTrends, getDevices, getRCA, stopRun, cancelAllQueued, getTrendSummary } from '../api';
import CrossAppRunModal from '../components/CrossAppRunModal';
import type { TestRun, Trends, Device } from '../api';
import { Activity, AlertTriangle, CheckCircle2, ChevronRight, Clock, FileText, Smartphone, PlayCircle, Square, Loader2 } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { parseServerDate } from '../time';

// A run that has not reached a terminal state can still be stopped.
const STOPPABLE = new Set(['queued', 'running', 'pending', 'preparing', 'downloading', 'collecting_evidence']);

// Status → the little icon shown inside its badge.
function statusIcon(status: string) {
  if (status === 'failed' || status === 'error') return <AlertTriangle size={12} />;
  if (status === 'passed' || status === 'completed') return <CheckCircle2 size={12} />;
  if (status === 'running') return <Loader2 size={12} className="spin" />;
  if (STOPPABLE.has(status)) return <Clock size={12} />;
  return <Square size={12} />;
}

export default function DashboardHome() {
  const [runs, setRuns] = useState<TestRun[]>([]);
  const [trends, setTrends] = useState<Trends | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [trendsError, setTrendsError] = useState<string | null>(null);
  const [topRootCause, setTopRootCause] = useState<string>("No failures yet ✓");
  const [stopping, setStopping] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const navigate = useNavigate();
  const [crossAppModal, setCrossAppModal] = useState(false);
  const [trendSummary, setTrendSummary] = useState<string>('');

  const flakyCount = runs.filter(r => r.flaky_detected || r.is_flaky).length;

  useEffect(() => {
    const projectId = runs.find(r => r.project_id)?.project_id;
    if (!projectId) return;
    let cancelled = false;
    getTrendSummary(projectId, 7)
      .then(r => { if (!cancelled) setTrendSummary(r.summary); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [runs]);

  async function refreshRuns() {
    try { setRuns(await getRuns()); setRunsError(null); } catch { setRunsError("Failed to load Runs."); }
  }

  useEffect(() => {
    async function loadData() {
      await refreshRuns();
      try { setTrends(await getTrends()); } catch (e) { setTrendsError("Failed to load Trends."); }
      setLoading(false);
    }
    loadData();
  }, []);

  const handleStop = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();  // don't navigate into the run
    setStopping(id);
    try { await stopRun(id); await refreshRuns(); }
    catch { /* surfaced by the runs error banner on next refresh */ }
    finally { setStopping(null); }
  };

  const handleCancelAll = async () => {
    setCancelling(true);
    try { await cancelAllQueued(); await refreshRuns(); }
    finally { setCancelling(false); }
  };

  // Top AI Root Cause: find the latest failed run and pull its RCA root_cause.
  useEffect(() => {
    async function loadTopRootCause() {
      const latestFailed = runs.slice(0, 10).find(r => r.status === 'failed');
      if (!latestFailed) {
        setTopRootCause("No failures yet ✓");
        return;
      }
      try {
        const rca = await getRCA(latestFailed.id);
        if (!rca) {
          // Failed run exists but RCA has not been generated at all.
          setTopRootCause("No failures yet ✓");
        } else if (!rca.root_cause || !rca.root_cause.trim()) {
          // RCA row exists but the LLM has not filled in a root cause yet.
          setTopRootCause("Analysis pending");
        } else {
          setTopRootCause(rca.root_cause);
        }
      } catch (e) {
        setTopRootCause("Analysis pending");
      }
    }
    if (runs.length > 0) loadTopRootCause();
  }, [runs]);

  useEffect(() => {
    let mounted = true;
    async function fetchDevices() {
      try {
        const res = await getDevices();
        if (mounted) {
          setDevices(res.devices);
        }
      } catch (e) {
        console.error("Failed to load devices", e);
      }
    }
    fetchDevices();
    const interval = setInterval(fetchDevices, 5000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  if (loading) return <div className="page-header"><h1 className="page-title animate-fade-in">Loading Dashboard...</h1></div>;

  const runningCount = runs.filter(r => r.status === 'running').length;
  const queuedCount = runs.filter(r => r.status === 'queued').length;

  return (
    <div className="animate-fade-in">
      <header className="page-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title">Mobile Test Operations</h1>
          <p className="page-subtitle">AI-powered insights, real device execution, and root cause analysis across your mobile test suites.</p>
        </div>
        <button
          onClick={() => setCrossAppModal(true)}
          className="btn"
          style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '10px 18px', whiteSpace: 'nowrap' }}
          title="Assign simulators + logins, then run the Consumer + Business scenario"
        >
          <PlayCircle size={16} /> Run Cross-App Suite
        </button>
      </header>

      {crossAppModal && (
        <CrossAppRunModal
          onClose={() => setCrossAppModal(false)}
          onStarted={(runId) => { setCrossAppModal(false); navigate(`/run/${runId}`); }}
        />
      )}

      <div className="grid-3" style={{ marginBottom: '24px' }}>
          <div className="card">
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)' }}>
              <Smartphone size={18} /> Connected Devices
            </div>
            <div className="stat-value">{devices.length}</div>
          </div>
          <div className="card">
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)' }}>
              <PlayCircle size={18} color="var(--primary)" /> Running Tests
            </div>
            <div className="stat-value">{runningCount}</div>
          </div>
      </div>

      {trendsError && (
        <div className="card" style={{ marginBottom: '40px', border: '1px solid var(--danger)', backgroundColor: 'rgba(239, 68, 68, 0.1)' }}>
          <div style={{ color: 'var(--danger)', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <AlertTriangle size={18} /> {trendsError}
          </div>
        </div>
      )}

      {trends && !trendsError && (
        <div className="grid-3" style={{ marginBottom: '40px' }}>
          <div className="card">
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)' }}>
              <Activity size={18} /> Total Failure Rate
            </div>
            <div className="stat-value">{Number(trends.failure_rate).toFixed(1)}%</div>
            <div style={{ marginTop: '8px', fontSize: '0.875rem', color: 'var(--text-muted)' }}>
              Out of {trends.total_executions} total executions
            </div>
          </div>
          
          <div className="card">
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)' }}>
              <AlertTriangle size={18} color="var(--warning)" /> Top Flaky Test
            </div>
            <div className="stat-value" style={{ fontSize: '1.5rem', marginTop: '16px' }}>
              {trends.flaky_tests.length > 0 ? trends.flaky_tests[0].test_name : "None"}
            </div>
            {trends.flaky_tests.length > 0 && (
                <div style={{ marginTop: '8px', fontSize: '0.875rem', color: 'var(--text-muted)' }}>
                Failed {trends.flaky_tests[0].fail_count} times recently
                </div>
            )}
          </div>

          <div className="card">
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)' }}>
              <FileText size={18} /> Top AI Root Cause
            </div>
            <div className="stat-value" style={{ fontSize: '1.25rem', marginTop: '16px', lineHeight: '1.4' }}>
              {topRootCause}
            </div>
          </div>
        </div>
      )}

      {/* ── Flaky KPI + Weekly Trend ── */}
      <div className="grid-3" style={{ marginBottom: '40px' }}>
        <div className="card">
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)' }}>
            <span style={{ fontSize: 16 }}>⚡</span> Flaky Tests
          </div>
          <div className="stat-value" style={{ color: flakyCount > 0 ? '#f59e0b' : undefined }}>{flakyCount}</div>
          <div style={{ marginTop: '8px', fontSize: '0.875rem', color: 'var(--text-muted)' }}>
            Passed only after a retry (recent runs)
          </div>
        </div>

        <div className="card" style={{ gridColumn: 'span 2' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)' }}>
            <Activity size={18} color="var(--accent-primary)" /> Weekly Trend
          </div>
          <p style={{ marginTop: 12, marginBottom: 0, lineHeight: 1.6 }}>
            {trendSummary || 'Generating weekly trend summary…'}
          </p>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px', gap: 12, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <h2 style={{ fontSize: '1.25rem', margin: 0 }}>Recent Test Runs</h2>
          {runs.length > 0 && (
            <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
              {runs.length} total{runningCount > 0 ? ` · ${runningCount} running` : ''}{queuedCount > 0 ? ` · ${queuedCount} queued` : ''}
            </span>
          )}
        </div>
        {queuedCount > 0 && (
          <button
            onClick={handleCancelAll}
            disabled={cancelling}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              background: 'transparent', border: '1px solid var(--danger)',
              borderRadius: 'var(--radius-sm)', color: 'var(--danger)',
              padding: '7px 14px', cursor: cancelling ? 'not-allowed' : 'pointer',
              fontSize: '0.82rem', fontFamily: 'inherit', opacity: cancelling ? 0.5 : 1,
            }}
          >
            {cancelling ? <Loader2 size={13} className="spin" /> : <Square size={13} />}
            Cancel all queued ({queuedCount})
          </button>
        )}
      </div>

      {runsError ? (
        <div className="card" style={{ border: '1px solid var(--danger)', backgroundColor: 'rgba(239, 68, 68, 0.1)' }}>
          <div style={{ color: 'var(--danger)', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <AlertTriangle size={18} /> {runsError}
          </div>
        </div>
      ) : (
        <div className="table-container table-scroll">
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th>Test Name</th>
                <th>Suite</th>
                <th>Device</th>
                <th>Duration</th>
                <th>Time</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {runs.map(run => (
                <tr key={run.id} className="row-link" onClick={() => navigate(`/run/${run.id}`)}>
                  <td>
                    <span className={`badge ${run.status}`}>
                      {statusIcon(run.status)}
                      {run.status}
                    </span>
                  </td>
                  <td style={{ fontWeight: 500 }}>
                    {run.test_name}
                    {(run.flaky_detected || run.is_flaky) && (
                      <span title={run.attempts ? `Passed after ${run.attempts} attempts` : 'Flaky'}
                        style={{ marginLeft: 8, background: 'rgba(245,158,11,0.15)', color: '#f59e0b', padding: '1px 7px', borderRadius: 999, fontSize: '0.68rem', fontWeight: 700 }}>
                        ⚡ FLAKY
                      </span>
                    )}
                    {run.visual_warning && (
                      <span title="Visual regression detected"
                        style={{ marginLeft: 6, background: 'rgba(245,158,11,0.15)', color: '#f59e0b', padding: '1px 7px', borderRadius: 999, fontSize: '0.68rem', fontWeight: 700 }}>
                        🖼️
                      </span>
                    )}
                    {run.crash_detected && (
                      <span title="App crashed during this run (app bug)"
                        style={{ marginLeft: 6, background: 'rgba(239,68,68,0.18)', color: '#ef4444', padding: '1px 7px', borderRadius: 999, fontSize: '0.68rem', fontWeight: 700 }}>
                        💥
                      </span>
                    )}
                  </td>
                  <td><span style={{ color: 'var(--text-secondary)', fontFamily: 'monospace', fontSize: '0.8rem', background: 'rgba(255,255,255,0.05)', padding: '2px 6px', borderRadius: '4px' }}>{run.test_suite}</span></td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
                      <Smartphone size={14} /> {run.device_name || 'N/A'} {run.os_version || ''}
                    </div>
                  </td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--text-secondary)' }}>
                      <Clock size={14}/> {(run.duration_ms / 1000).toFixed(1)}s
                    </div>
                  </td>
                  <td style={{ color: 'var(--text-secondary)' }}>
                    {formatDistanceToNow(parseServerDate(run.created_at), { addSuffix: true })}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: '8px' }}>
                      {STOPPABLE.has(run.status) && (
                        <button
                          onClick={e => handleStop(e, run.id)}
                          disabled={stopping === run.id}
                          title="Stop this run"
                          style={{
                            display: 'flex', alignItems: 'center', gap: '5px',
                            background: 'transparent', border: '1px solid var(--danger)',
                            borderRadius: 'var(--radius-sm)', color: 'var(--danger)',
                            padding: '4px 10px', cursor: 'pointer', fontSize: '0.75rem',
                            fontFamily: 'inherit', opacity: stopping === run.id ? 0.5 : 1,
                          }}
                        >
                          {stopping === run.id ? <Loader2 size={11} className="spin" /> : <Square size={11} />}
                          Stop
                        </button>
                      )}
                      <ChevronRight size={18} color="var(--text-muted)" />
                    </div>
                  </td>
                </tr>
              ))}
              {runs.length === 0 && (
                <tr>
                  <td colSpan={7} style={{ textAlign: 'center', padding: '40px', color: 'var(--text-muted)' }}>
                    No test runs found. Start the automation runner to populate data!
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
