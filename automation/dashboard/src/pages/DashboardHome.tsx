import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getRuns, getTrends, getDevices } from '../api';
import type { TestRun, Trends, Device } from '../api';
import { Activity, AlertTriangle, CheckCircle2, ChevronRight, Clock, FileText, Smartphone, PlayCircle } from 'lucide-react';
import { formatDistanceToNow, parseISO } from 'date-fns';

export default function DashboardHome() {
  const [runs, setRuns] = useState<TestRun[]>([]);
  const [trends, setTrends] = useState<Trends | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [trendsError, setTrendsError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    async function loadData() {
      try { setRuns(await getRuns()); } catch (e) { setRunsError("Failed to load Runs."); }
      try { setTrends(await getTrends()); } catch (e) { setTrendsError("Failed to load Trends."); }
      setLoading(false);
    }
    loadData();
  }, []);

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

  return (
    <div className="animate-fade-in">
      <header className="page-header">
        <h1 className="page-title">Mobile Test Operations</h1>
        <p className="page-subtitle">AI-powered insights, real device execution, and root cause analysis across your mobile test suites.</p>
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
              {trends.common_root_causes.length > 0 ? trends.common_root_causes[0].failure_category : "None"}
            </div>
          </div>
        </div>
      )}

      <h2 style={{ marginBottom: '16px', fontSize: '1.25rem' }}>Recent Test Runs</h2>
      
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
                    <ChevronRight size={18} color="var(--text-muted)" />
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
