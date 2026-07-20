import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getRuns, getTrends, getDevices, getRCA, stopRun, cancelAllQueued, runCrossAppSuite } from '../api';
import type { TestRun, Trends, Device } from '../api';
import { Activity, AlertTriangle, CheckCircle2, ChevronRight, Clock, FileText, Smartphone, PlayCircle, Square, Loader2 } from 'lucide-react';
import { formatDistanceToNow, parseISO } from 'date-fns';

// A run that has not reached a terminal state can still be stopped.
const STOPPABLE = new Set(['queued', 'running', 'pending', 'preparing', 'downloading', 'collecting_evidence']);

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
  const [crossAppBusy, setCrossAppBusy] = useState(false);

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
          onClick={async () => {
            if (crossAppBusy) return;
            setCrossAppBusy(true);
            try {
              const r = await runCrossAppSuite();
              navigate(`/run/${r.run_id}`);
            } catch (e: any) {
              alert(e?.message || 'Could not start cross-app run');
            } finally {
              setCrossAppBusy(false);
            }
          }}
          className="btn"
          style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '10px 18px', whiteSpace: 'nowrap' }}
          title="Run the full Consumer + Business scenario across both iOS simulators at once"
        >
          {crossAppBusy ? <Loader2 size={16} style={{ animation: 'spin 1s linear infinite' }} /> : <PlayCircle size={16} />}
          {crossAppBusy ? 'Starting…' : 'Run Cross-App Suite'}
        </button>
      </header>

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

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
        <h2 style={{ fontSize: '1.25rem', margin: 0 }}>Recent Test Runs</h2>
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
        <div className="table-container">
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
                      {run.status === 'failed' ? <AlertTriangle size={12}/> : <CheckCircle2 size={12}/>}
                      {run.status}
                    </span>
                  </td>
                  <td style={{ fontWeight: 500 }}>{run.test_name}</td>
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
                    {formatDistanceToNow(parseISO(run.created_at), { addSuffix: true })}
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
