import { useEffect, useRef, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { getRun, getRCA, getEvidence, getAuthToken, getRunScenarios, triggerAnalysis } from '../api';
import type { TestRun, RCAReport, Evidence, ScenariosResponse, ScenarioResult } from '../api';
import { format } from 'date-fns';
import { ArrowLeft, AlertTriangle, CheckCircle2, Zap, GitBranch, GitCommit, FileCode2, Info, Clock, Activity, Monitor, Wifi, WifiOff, ChevronDown, ChevronRight, Smartphone, Users, Loader2 } from 'lucide-react';
import ReactMarkdown from 'react-markdown';

// ── Scenarios Tab (Android cross-app: Consumer + Business) ───────────────────

const ACTIVE_RUN_STATES = new Set([
  'queued', 'running', 'collecting_evidence', 'downloading', 'preparing', 'assigned',
]);

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { bg: string; fg: string; label: string }> = {
    PASS: { bg: 'rgba(34,197,94,0.15)', fg: '#22c55e', label: 'PASS' },
    FAIL: { bg: 'rgba(239,68,68,0.15)', fg: '#ef4444', label: 'FAIL' },
    'N/A': { bg: 'rgba(148,163,184,0.15)', fg: '#94a3b8', label: 'N/A' },
  };
  const s = map[status] || map['N/A'];
  return (
    <span style={{ background: s.bg, color: s.fg, padding: '3px 10px', borderRadius: 999, fontSize: '0.72rem', fontWeight: 700, letterSpacing: '0.04em' }}>
      {s.label}
    </span>
  );
}

function ScenarioRow({ s }: { s: ScenarioResult }) {
  const [open, setOpen] = useState(false);
  const canExpand = (s.reasons && s.reasons.length > 0) || !!s.error;
  return (
    <>
      <tr
        onClick={() => canExpand && setOpen(o => !o)}
        style={{ cursor: canExpand ? 'pointer' : 'default', borderTop: '1px solid var(--border-color)' }}
      >
        <td style={{ padding: '10px 12px', width: 28 }}>
          {canExpand ? (open ? <ChevronDown size={14} /> : <ChevronRight size={14} />) : null}
        </td>
        <td style={{ padding: '10px 12px', fontFamily: 'monospace', color: 'var(--text-secondary)' }}>{s.scenario_num}</td>
        <td style={{ padding: '10px 12px' }}>{s.scenario_name}</td>
        <td style={{ padding: '10px 12px' }}><StatusBadge status={s.status} /></td>
        <td style={{ padding: '10px 12px' }}><StatusBadge status={s.consumer_status} /></td>
        <td style={{ padding: '10px 12px' }}><StatusBadge status={s.business_status} /></td>
        <td style={{ padding: '10px 12px', color: 'var(--danger)', fontSize: '0.8rem', maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {s.error || '—'}
        </td>
        <td style={{ padding: '10px 12px', fontFamily: 'monospace', color: 'var(--text-secondary)' }}>
          {s.launch_time != null ? `${s.launch_time.toFixed(1)}s` : '—'}
        </td>
      </tr>
      {open && canExpand && (
        <tr style={{ background: 'rgba(0,0,0,0.2)' }}>
          <td colSpan={8} style={{ padding: '10px 40px 14px' }}>
            <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-secondary)', fontSize: '0.82rem', lineHeight: 1.7 }}>
              {(s.reasons && s.reasons.length ? s.reasons : [s.error || '']).filter(Boolean).map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

function SummaryCard({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="card" style={{ padding: '14px 18px', textAlign: 'center', minWidth: 120 }}>
      <div style={{ fontSize: '1.7rem', fontWeight: 700, color }}>{value}</div>
      <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.06em', marginTop: 4 }}>{label}</div>
    </div>
  );
}

function ScenariosTab({ runId, active }: { runId: string; active: boolean }) {
  const [data, setData] = useState<ScenariosResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const d = await getRunScenarios(runId);
        if (cancelled) return;
        setData(d); setErr(null);
      } catch (e: any) {
        if (!cancelled) setErr(e?.message || 'Failed to load scenarios');
      } finally {
        // Auto-refresh every 5s while the run is still active.
        if (!cancelled && active) timer = setTimeout(load, 5000);
      }
    }
    load();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [runId, active]);

  if (err) return <div className="card" style={{ color: 'var(--danger)' }}>{err}</div>;
  if (!data) return <div className="card">Loading scenarios…</div>;
  if (data.total === 0) {
    return (
      <div className="card" style={{ textAlign: 'center', color: 'var(--text-secondary)', padding: '32px' }}>
        No scenario results yet{active ? ' — waiting for the Android bot…' : '.'}
      </div>
    );
  }

  return (
    <div>
      <div className="card" style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16, background: 'rgba(99,102,241,0.08)', borderColor: 'rgba(99,102,241,0.3)' }}>
        <Users size={18} color="var(--accent-primary)" />
        <strong style={{ color: 'var(--text-primary)' }}>{data.rule}</strong>
      </div>

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 20 }}>
        <SummaryCard label="Total" value={data.total} color="var(--text-primary)" />
        <SummaryCard label="Passed" value={data.passed} color="#22c55e" />
        <SummaryCard label="Failed" value={data.failed} color="#ef4444" />
        <SummaryCard label="Consumer ✓" value={data.consumer_passed} color="#22c55e" />
        <SummaryCard label="Business ✓" value={data.business_passed} color="#22c55e" />
      </div>

      <div className="card" style={{ padding: 0, overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--text-secondary)', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              <th style={{ padding: '10px 12px' }}></th>
              <th style={{ padding: '10px 12px' }}>TC#</th>
              <th style={{ padding: '10px 12px' }}>Scenario</th>
              <th style={{ padding: '10px 12px' }}>Overall</th>
              <th style={{ padding: '10px 12px' }}>Consumer</th>
              <th style={{ padding: '10px 12px' }}>Business</th>
              <th style={{ padding: '10px 12px' }}>Error</th>
              <th style={{ padding: '10px 12px' }}>Launch</th>
            </tr>
          </thead>
          <tbody>
            {data.scenarios.map(s => <ScenarioRow key={s.id} s={s} />)}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BotBadge({ botType }: { botType?: string }) {
  const android = botType === 'android';
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5, padding: '4px 10px', borderRadius: 999,
      fontSize: '0.72rem', fontWeight: 600,
      background: android ? 'rgba(34,197,94,0.12)' : 'rgba(99,102,241,0.12)',
      color: android ? '#22c55e' : 'var(--accent-primary)',
    }}>
      {android ? <Users size={12} /> : <Smartphone size={12} />}
      {android ? 'Android Bot' : 'iOS Appium'}
    </span>
  );
}

// ── Live View Component ──────────────────────────────────────────────────────

const ACTIVE_JOB_STATES = new Set([
  'queued',
  'running',
  'collecting_evidence',
  'downloading',
  'preparing',
]);

type StreamState = 'waiting' | 'streaming' | 'ended';

interface LiveViewProps {
  runId: string;
  jobState?: string | null;
}

function LiveView({ runId, jobState }: LiveViewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [streamState, setStreamState] = useState<StreamState>('waiting');

  const isActive = jobState && ACTIVE_JOB_STATES.has(jobState);

  useEffect(() => {
    if (!isActive) return;

    const token = getAuthToken();
    if (!token) return;

    const wsUrl = `ws://localhost:8000/ws/stream/${runId}?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
    ws.binaryType = 'blob';

    ws.onmessage = async (event: MessageEvent) => {
      // Binary frame → draw on canvas
      if (event.data instanceof Blob) {
        setStreamState('streaming');
        const canvas = canvasRef.current;
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        try {
          const blob = new Blob([event.data]);
          const bitmap = await createImageBitmap(blob);
          ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
          bitmap.close();
        } catch {
          // Corrupt frame — skip silently
        }
        return;
      }
      // Text/JSON frame → check for end signal
      if (typeof event.data === 'string') {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'ended') {
            setStreamState('ended');
            ws.close(1000, 'stream ended');
          }
        } catch {
          // Ignore non-JSON text frames
        }
      }
    };

    ws.onerror = () => {
      setStreamState(prev => (prev === 'waiting' ? 'ended' : prev));
    };

    ws.onclose = () => {
      setStreamState(prev => (prev === 'streaming' || prev === 'waiting' ? 'ended' : prev));
    };

    return () => {
      // Detach first: a socket torn down by StrictMode's double-mount must not
      // flip streamState to 'ended' on the instance that replaced it.
      ws.onmessage = ws.onerror = ws.onclose = null;
      if (ws.readyState === WebSocket.CONNECTING) {
        ws.addEventListener('open', () => ws.close(), { once: true });
      } else {
        ws.close();
      }
      wsRef.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, isActive]);

  if (!isActive) return null;

  return (
    <div className="card animate-fade-in" style={{ marginBottom: '24px' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
        <h3 style={{ display: 'flex', alignItems: 'center', gap: '8px', margin: 0 }}>
          <Monitor size={18} color="var(--accent-primary)" />
          Live Device View
        </h3>
        <div
          className="badge"
          style={{
            background: streamState === 'streaming'
              ? 'rgba(52, 211, 153, 0.15)'
              : streamState === 'ended'
              ? 'rgba(255,255,255,0.06)'
              : 'rgba(251, 191, 36, 0.15)',
            color: streamState === 'streaming'
              ? 'var(--success)'
              : streamState === 'ended'
              ? 'var(--text-muted)'
              : 'var(--warning)',
          }}
        >
          {streamState === 'streaming'
            ? <><Wifi size={12} /> Live</>
            : streamState === 'ended'
            ? <><WifiOff size={12} /> Ended</>
            : <><Wifi size={12} /> Connecting…</>}
        </div>
      </div>

      {/* Canvas wrapper */}
      <div
        style={{
          position: 'relative',
          width: 360,
          height: 640,
          background: '#000',
          borderRadius: 'var(--radius-md)',
          overflow: 'hidden',
          margin: '0 auto',
          border: '1px solid var(--border-color)',
          boxShadow: '0 0 40px rgba(0,0,0,0.6)',
        }}
      >
        <canvas
          ref={canvasRef}
          width={360}
          height={640}
          style={{ display: 'block', width: '100%', height: '100%' }}
        />

        {/* Waiting overlay */}
        {streamState === 'waiting' && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexDirection: 'column',
              gap: '16px',
              background: 'rgba(10,10,15,0.92)',
            }}
          >
            <div style={{ position: 'relative', width: 56, height: 56 }}>
              <div style={{
                position: 'absolute', inset: 0,
                borderRadius: '50%',
                border: '3px solid rgba(129,140,248,0.2)',
              }} />
              <div style={{
                position: 'absolute', inset: 0,
                borderRadius: '50%',
                border: '3px solid transparent',
                borderTopColor: 'var(--accent-primary)',
                animation: 'lv-spin 1s linear infinite',
              }} />
            </div>
            <div style={{ textAlign: 'center' }}>
              <p style={{ color: 'var(--text-primary)', fontWeight: 500, margin: 0 }}>
                Waiting for stream…
              </p>
              <p style={{ color: 'var(--text-muted)', fontSize: '0.8rem', marginTop: '4px' }}>
                Frames will appear once the test starts
              </p>
            </div>
          </div>
        )}

        {/* Stream ended overlay */}
        {streamState === 'ended' && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexDirection: 'column',
              gap: '8px',
              background: 'rgba(0,0,0,0.55)',
              backdropFilter: 'blur(4px)',
            }}
          >
            <WifiOff size={32} color="var(--text-muted)" />
            <p style={{ color: 'var(--text-secondary)', fontSize: '1rem', margin: 0 }}>
              Stream ended
            </p>
          </div>
        )}
      </div>

      <style>{`
        @keyframes lv-spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}


// ── Main RunDetails Page ─────────────────────────────────────────────────────

export default function RunDetails() {
  const { id } = useParams<{id: string}>();
  const [run, setRun] = useState<TestRun | null>(null);
  const [rca, setRca] = useState<RCAReport | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<'analysis' | 'scenarios'>('analysis');
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    const TERMINAL = ['passed', 'failed', 'completed', 'stopped', 'cancelled', 'error'];
    let timer: ReturnType<typeof setTimeout> | undefined;
    let cancelled = false;

    async function poll() {
      try {
        const runData = await getRun(id!);
        if (cancelled) return;
        setRun(runData);
        if (TERMINAL.includes(runData.status)) {
          // Run is done — fetch RCA/evidence once and stop polling.
          const [rcaData, evidenceData] = await Promise.all([
            getRCA(id!).catch(() => null),
            getEvidence(id!).catch(() => null),
          ]);
          if (!cancelled) { setRca(rcaData); setEvidence(evidenceData); }
        } else {
          // Still queued/running — keep refreshing so the badge moves live.
          timer = setTimeout(poll, 3000);
        }
      } catch (e) {
        console.error('Failed to load run details', e);
        if (!cancelled) timer = setTimeout(poll, 5000);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [id]);

  if (loading) return <div className="page-header"><h1 className="page-title animate-fade-in">Loading Analysis...</h1></div>;
  if (!run) return <div>Run not found</div>;

  const timeline = run.timeline ? JSON.parse(run.timeline) : [];

  return (
    <div className="animate-fade-in">
      <Link to="/" style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', color: 'var(--text-secondary)', textDecoration: 'none', marginBottom: '24px', fontWeight: 500 }}>
        <ArrowLeft size={16} /> Back to Dashboard
      </Link>

      <header className="page-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
            <span className={`badge ${run.status}`} style={{ padding: '6px 12px', fontSize: '0.85rem' }}>
              {run.status === 'failed' ? <AlertTriangle size={14}/> : <CheckCircle2 size={14}/>}
              {run.status}
            </span>
            <BotBadge botType={run.bot_type} />
            <h1 className="page-title" style={{ margin: 0 }}>{run.test_name}</h1>
          </div>
          <p className="page-subtitle">
            Suite: {run.test_suite} • Run ID: {run.id.substring(0,8)}... • 
            Time: {format(new Date(run.created_at), "MMM d, yyyy h:mm a")} • 
            Device: {run.device_name}
          </p>
        </div>
      </header>

      {/* ── Tab bar ── */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 24, borderBottom: '1px solid var(--border-color)' }}>
        {(['analysis', 'scenarios'] as const).map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            style={{
              background: 'transparent', border: 'none', cursor: 'pointer',
              padding: '10px 18px', fontSize: '0.9rem', fontWeight: 600, fontFamily: 'inherit',
              color: tab === t ? 'var(--accent-primary)' : 'var(--text-secondary)',
              borderBottom: tab === t ? '2px solid var(--accent-primary)' : '2px solid transparent',
              marginBottom: -1,
            }}
          >
            {t === 'analysis' ? 'Analysis' : 'Scenarios'}
          </button>
        ))}
      </div>

      {tab === 'scenarios' && id && (
        <ScenariosTab runId={id} active={ACTIVE_RUN_STATES.has(run.status)} />
      )}

      {tab === 'analysis' && (
      <>
      {/* ── Live View — shown for in-progress runs ── */}
      {id && <LiveView runId={id} jobState={run.job_state} />}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: '24px', alignItems: 'start' }}>
        <div>
          {evidence && evidence.git_commit && (
        <div className="card" style={{ marginBottom: '24px', padding: '16px 24px', display: 'flex', gap: '24px', background: 'var(--bg-tertiary)' }}>
           <div style={{display: 'flex', alignItems: 'center', gap: '8px'}}>
               <GitCommit size={16} color="var(--accent-primary)" />
               <span style={{color: 'var(--text-secondary)'}}>Commit:</span>
               <span style={{fontFamily: 'monospace'}}>{evidence.git_commit.substring(0, 7)}</span>
           </div>
           <div style={{display: 'flex', alignItems: 'center', gap: '8px'}}>
               <GitBranch size={16} color="var(--accent-primary)" />
               <span style={{color: 'var(--text-secondary)'}}>Branch:</span>
               <span>{evidence.git_branch}</span>
           </div>
           <div style={{display: 'flex', alignItems: 'center', gap: '8px'}}>
               <FileCode2 size={16} color="var(--accent-primary)" />
               <span style={{color: 'var(--text-secondary)'}}>Changed Files:</span>
               <span>{evidence.changed_files?.length || 0}</span>
           </div>
        </div>
      )}

      {run.error_message && (
        <div className="card" style={{ marginBottom: '32px', borderColor: 'rgba(239, 68, 68, 0.3)' }}>
          <h3 style={{ marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--danger)' }}>
            <AlertTriangle size={18} /> Raw Error
          </h3>
          <pre style={{ margin: 0, padding: '16px', background: 'rgba(0,0,0,0.4)', borderRadius: '8px', overflowX: 'auto', fontSize: '0.85rem', color: '#f87171' }}>
            <code>{run.error_message}</code>
          </pre>
        </div>
      )}

      {rca ? (
        <div className="card" style={{ borderTop: '4px solid var(--accent-primary)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px', paddingBottom: '16px', borderBottom: '1px solid var(--border-color)' }}>
            <h2 style={{ display: 'flex', alignItems: 'center', gap: '8px', margin: 0 }}>
              <Zap size={24} color="var(--accent-primary)" /> AI Root Cause Analysis
            </h2>
            <div style={{display: 'flex', gap: '12px'}}>
                <div className="badge" style={{ background: 'rgba(99, 102, 241, 0.1)', color: 'var(--accent-primary)' }}>
                  Confidence: {(rca.confidence * 100).toFixed(0)}%
                </div>
                <div className="badge" style={{ background: rca.priority === 'High' ? 'var(--danger-bg)' : 'rgba(255,255,255,0.1)', color: rca.priority === 'High' ? 'var(--danger)' : '#fff' }}>
                  {rca.priority} Priority
                </div>
            </div>
          </div>
          
          <div className="grid-3" style={{ marginBottom: '24px' }}>
              <div style={{background: 'rgba(0,0,0,0.2)', padding: '16px', borderRadius: '8px'}}>
                  <div style={{color: 'var(--text-secondary)', fontSize: '0.8rem', textTransform: 'uppercase', marginBottom: '4px'}}>Failure Category</div>
                  <div style={{fontWeight: 600, fontSize: '1.1rem'}}>{rca.failure_category}</div>
              </div>
              <div style={{background: 'rgba(0,0,0,0.2)', padding: '16px', borderRadius: '8px'}}>
                  <div style={{color: 'var(--text-secondary)', fontSize: '0.8rem', textTransform: 'uppercase', marginBottom: '4px'}}>Severity</div>
                  <div style={{fontWeight: 600, fontSize: '1.1rem'}}>{rca.severity}</div>
              </div>
              <div style={{background: 'rgba(0,0,0,0.2)', padding: '16px', borderRadius: '8px'}}>
                  <div style={{color: 'var(--text-secondary)', fontSize: '0.8rem', textTransform: 'uppercase', marginBottom: '4px'}}>Responsible Module</div>
                  <div style={{fontWeight: 600, fontSize: '1.1rem'}}>{rca.responsible_module}</div>
              </div>
          </div>

          <div className="markdown-body">
            <h3 style={{display: 'flex', alignItems: 'center', gap: '8px'}}><Info size={18} color="var(--accent-primary)"/> Root Cause</h3>
            <p>{rca.root_cause}</p>
            
            <h3 style={{display: 'flex', alignItems: 'center', gap: '8px'}}><Info size={18} color="var(--accent-primary)"/> Summary</h3>
            <p>{rca.summary}</p>
            
            <h3 style={{display: 'flex', alignItems: 'center', gap: '8px'}}><Info size={18} color="var(--accent-primary)"/> Possible Reason</h3>
            <p>{rca.possible_reason}</p>

            <h3 style={{display: 'flex', alignItems: 'center', gap: '8px'}}><Info size={18} color="var(--accent-primary)"/> Impact</h3>
            <p>{rca.impact}</p>
            
            <h3>Affected Modules</h3>
            {/* Older RCA rows predate the affected_modules column, so this can
                legitimately be null — never assume the array exists. */}
            {rca.affected_modules?.length ? (
              <ul>
                {rca.affected_modules.map((mod, i) => (
                  <li key={i}><code>{mod}</code></li>
                ))}
              </ul>
            ) : (
              <p style={{ color: 'var(--text-muted)' }}>None identified.</p>
            )}
            
            <div style={{ background: 'rgba(0,0,0,0.3)', padding: '24px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
              <ReactMarkdown>{rca.suggested_fix}</ReactMarkdown>
            </div>
          </div>
        </div>
      ) : (
        run.status === 'failed' && (
          <div className="card" style={{ textAlign: 'center', padding: '48px' }}>
            <Activity size={48} color="var(--text-muted)" style={{ margin: '0 auto 16px' }} />
            <h3 style={{ marginBottom: '8px' }}>No RCA Available</h3>
            <p style={{ color: 'var(--text-secondary)', marginBottom: '24px' }}>An RCA report has not been generated for this failed run yet.</p>
            {analyzeError && (
              <p style={{ color: 'var(--danger)', marginBottom: '16px', fontSize: '0.85rem' }}>{analyzeError}</p>
            )}
            <button
              className="btn"
              disabled={analyzing}
              onClick={async () => {
                if (!id || analyzing) return;
                setAnalyzing(true); setAnalyzeError(null);
                try {
                  const result = await triggerAnalysis(id);
                  if (result) setRca(result);
                  else setAnalyzeError('Analysis returned no report — check the backend logs.');
                } catch (e: any) {
                  setAnalyzeError(e?.message || 'Analysis failed. Is Ollama running?');
                } finally {
                  setAnalyzing(false);
                }
              }}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}
            >
              {analyzing && <Loader2 size={15} style={{ animation: 'spin 1s linear infinite' }} />}
              {analyzing ? 'Analyzing…' : 'Trigger Analysis'}
            </button>
          </div>
        )
      )}
      </div>
      
      {/* Sidebar Timeline */}
      <div className="card" style={{ position: 'sticky', top: '24px' }}>
        <h3 style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '24px', borderBottom: '1px solid var(--border-color)', paddingBottom: '12px' }}>
          <Clock size={18} color="var(--primary)" /> Execution Timeline
        </h3>
        
        {run.timeline ? (
          <div className="timeline">
            {timeline.map((event: any, idx: number) => (
              <div key={idx} style={{ display: 'flex', gap: '16px', marginBottom: '16px', position: 'relative' }}>
                <div style={{ minWidth: '65px', fontSize: '0.8rem', color: 'var(--text-secondary)', paddingTop: '2px' }}>
                  {event.time}
                </div>
                <div style={{ 
                  position: 'absolute', left: '76px', top: '6px', bottom: '-16px', width: '2px', 
                  background: idx === timeline.length - 1 ? 'transparent' : 'var(--border-color)' 
                }}></div>
                <div style={{ 
                  width: '10px', height: '10px', borderRadius: '50%', background: 'var(--primary)', 
                  position: 'absolute', left: '72px', top: '6px', zIndex: 1
                }}></div>
                <div style={{ marginLeft: '24px', fontSize: '0.9rem', color: event.event.includes('Failed') ? 'var(--danger)' : 'var(--text-primary)' }}>
                  {event.event}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', textAlign: 'center', padding: '24px 0' }}>
            No timeline data available for this run.
          </p>
        )}
      </div>
    </div>
      </>
      )}
  </div>
  );
}
