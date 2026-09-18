import { useEffect, useRef, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { getRun, getRCA, getEvidence, getRunScenarios, triggerAnalysis,
  getVisualRegression, updateVisualBaseline, getRiskPredictions, getRunSummary, getPerformance } from '../api';
import type { TestRun, RCAReport, Evidence, ScenariosResponse, ScenarioResult,
  VisualRegressionItem, RiskPrediction, PerformanceResponse } from '../api';
import { format } from 'date-fns';
import { parseServerDate } from '../time';
import { ArrowLeft, AlertTriangle, CheckCircle2, Zap, GitBranch, GitCommit, FileCode2, Info, Clock, Activity, ChevronDown, ChevronRight, Smartphone, Users, Loader2, Image as ImageIcon, Sparkles, TrendingUp, RefreshCw, Gauge, Cpu, ArrowUp, ArrowDown } from 'lucide-react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip as RTooltip, ResponsiveContainer, Legend } from 'recharts';
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
  const canExpand = (s.reasons && s.reasons.length > 0) || !!s.error || !!s.screenshot;
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
        {/* How far it got. A run that fails at step 7 of 8 is a different problem
            from one that fails at step 1; both used to read just "FAIL". */}
        <td style={{ padding: '10px 12px', fontFamily: 'monospace', fontSize: '0.8rem',
                     color: s.steps_pass_pct == null ? 'var(--text-muted)'
                          : s.steps_pass_pct === 100 ? 'var(--success)'
                          : s.steps_pass_pct >= 50 ? '#fbbf24' : 'var(--danger)' }}>
          {s.steps_total ? `${s.steps_passed}/${s.steps_total} · ${s.steps_pass_pct}%` : '—'}
        </td>
        <td style={{ padding: '10px 12px', fontFamily: 'monospace', color: 'var(--text-secondary)' }}>
          {s.launch_time != null ? `${s.launch_time.toFixed(1)}s` : '—'}
        </td>
      </tr>
      {open && canExpand && (
        <tr style={{ background: 'rgba(0,0,0,0.2)' }}>
          <td colSpan={9} style={{ padding: '10px 40px 14px' }}>
            <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-secondary)', fontSize: '0.82rem', lineHeight: 1.7 }}>
              {(s.reasons && s.reasons.length ? s.reasons : [s.error || '']).filter(Boolean).map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
            {s.screenshot && (
              <div style={{ marginTop: 12 }}>
                <div style={{ color: 'var(--text-muted)', fontSize: '0.72rem', marginBottom: 5 }}>Screen at failure:</div>
                <a href={s.screenshot} target="_blank" rel="noreferrer">
                  <img src={s.screenshot} alt="screen at failure"
                    style={{ maxWidth: 240, maxHeight: 420, border: '1px solid var(--border-color)', borderRadius: 8, boxShadow: '0 2px 10px rgba(0,0,0,0.35)' }} />
                </a>
                <div style={{ color: 'var(--text-muted)', fontSize: '0.68rem', marginTop: 3 }}>click to enlarge</div>
              </div>
            )}
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
              <th style={{ padding: '10px 12px' }}>Steps</th>
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

/** Elapsed seconds → a readable duration. A stage that took four minutes read as
 *  "247.3s", which is why long stages looked wrong at a glance. */
function fmtSecs(secs: number): string {
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const m = Math.floor(secs / 60);
  return `${m}m ${Math.round(secs - m * 60)}s`;
}

interface LiveViewProps {
  runId: string;
  jobState?: string | null;
}

/** Live Steps — replaces the (meaningless) video stream with the actual steps
 *  the run has executed and is executing, polled from the scenarios endpoint.
 *  Each segment shows its step log with ✓/✗; the in-flight segment is highlighted. */
function LiveView({ runId, jobState }: LiveViewProps) {
  const [scenarios, setScenarios] = useState<ScenarioResult[]>([]);
  const [loaded, setLoaded] = useState(false);
  const isActive = !!jobState && ACTIVE_JOB_STATES.has(jobState);

  // Live clock for the in-flight stage. The backend only writes an elapsed value
  // when a STEP completes, so between events (which can be a minute apart on a
  // slow resolve) a poll-driven number sits frozen and under-reports the stage.
  // We keep the last elapsed the backend reported plus the wall-clock moment it
  // CHANGED, and count up from there — re-baselining on every poll instead would
  // make the timer jump backwards each time an unchanged value came back.
  const baseline = useRef<{ num: string; secs: number; at: number } | null>(null);
  const [, tick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const d = await getRunScenarios(runId);
        if (!cancelled) { setScenarios(d.scenarios || []); setLoaded(true); }
      } catch { if (!cancelled) setLoaded(true); }
      if (!cancelled && isActive) timer = setTimeout(poll, 2500);
    };
    poll();
    return () => { cancelled = true; clearTimeout(timer!); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, isActive]);

  // Re-baseline only when the in-flight stage changes, or its reported elapsed does.
  const last = scenarios[scenarios.length - 1];
  const lastNum = last?.scenario_num ?? '';
  const lastSecs = last?.launch_time ?? 0;
  useEffect(() => {
    if (!last) return;
    const b = baseline.current;
    if (!b || b.num !== lastNum || b.secs !== lastSecs) {
      baseline.current = { num: lastNum, secs: lastSecs, at: Date.now() };
    }
  }, [last, lastNum, lastSecs]);

  // Repaint once a second while the run is live, so the ticker actually ticks.
  useEffect(() => {
    if (!isActive) return;
    const t = setInterval(() => tick(n => n + 1), 1000);
    return () => clearInterval(t);
  }, [isActive]);

  // Nothing to show if the run isn't active and never recorded a step.
  if (!isActive && scenarios.length === 0) return null;

  return (
    <div className="card animate-fade-in" style={{ marginBottom: '24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <h3 style={{ display: 'flex', alignItems: 'center', gap: 8, margin: 0 }}>
          <Activity size={18} color="var(--accent-primary)" />
          Live Steps
        </h3>
        <div className="badge" style={{
          background: isActive ? 'rgba(251,191,36,0.15)' : 'rgba(255,255,255,0.06)',
          color: isActive ? 'var(--warning)' : 'var(--text-muted)',
        }}>
          {isActive ? <><Loader2 size={12} className="spin" /> Running</> : <>Finished</>}
        </div>
      </div>

      {!loaded && scenarios.length === 0 ? (
        <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem', display: 'flex', alignItems: 'center', gap: 8 }}>
          <Loader2 size={13} className="spin" /> Waiting for the first step…
        </div>
      ) : scenarios.length === 0 ? (
        <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>No steps recorded yet.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          {scenarios.map((s, i) => {
            // Any status the backend considers terminal. STOPPED and SKIPPED are
            // terminal too: treating only PASS/FAIL as done left a stopped run's
            // last segment looking like it was still in flight.
            const done = s.status === 'PASS' || s.status === 'FAIL'
              || s.status === 'STOPPED' || s.status === 'SKIPPED';
            // Only spin while the RUN itself is still active. A finished run can
            // still carry a segment row stranded at 'running' (it is written before
            // each step and only overwritten when that step returns), and rendering
            // s.status raw showed a RUNNING badge on a run whose header said
            // STOPPED — which reads as "Stop did nothing".
            const running = isActive && i === scenarios.length - 1 && !done;
            const staleRunning = !isActive && !done
              && (s.status || '').toLowerCase() === 'running';
            const isLast = i === scenarios.length - 1;
            const steps = (s.reasons || []).filter(r => !/↳ (screen ids|on screen):/.test(r));
            // Which app/role this stage runs on → node icon + chip.
            const role = /kitchen/i.test(s.scenario_name)
              ? { icon: '🍳', label: 'Kitchen', tint: '#f59e0b' }
              : /waiter|b-app|business/i.test(s.scenario_name)
              ? { icon: '🧑‍🍳', label: 'Waiter', tint: 'var(--accent-primary)' }
              : { icon: '🧑', label: 'Consumer', tint: '#38bdf8' };
            const nodeColor = s.status === 'PASS' ? 'var(--success)'
              : s.status === 'FAIL' ? 'var(--danger)'
              : running ? 'var(--accent-primary)' : 'var(--border-color)';
            const nodeBg = s.status === 'PASS' ? 'rgba(52,211,153,0.15)'
              : s.status === 'FAIL' ? 'rgba(248,113,113,0.15)'
              : running ? 'rgba(129,140,248,0.12)' : 'var(--bg-secondary, rgba(255,255,255,0.03))';
            const badgeBg = s.status === 'PASS' ? 'rgba(52,211,153,0.15)'
              : s.status === 'FAIL' ? 'rgba(248,113,113,0.15)' : 'rgba(129,140,248,0.15)';
            const badgeFg = s.status === 'PASS' ? 'var(--success)'
              : s.status === 'FAIL' ? 'var(--danger)' : 'var(--accent-primary)';
            // The spine below a node is solid+colored once the stage resolves, so the
            // flow reads as "connected" and progress flows top→bottom.
            const spineColor = s.status === 'PASS' ? 'var(--success)'
              : s.status === 'FAIL' ? 'var(--danger)' : 'var(--border-color)';
            return (
              <div key={s.id || i} style={{ display: 'flex', gap: 14, alignItems: 'stretch' }}>
                {/* Left rail: the stage node + the connecting spine to the next stage. */}
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: 32, flexShrink: 0 }}>
                  <div style={{
                    width: 30, height: 30, borderRadius: '50%', flexShrink: 0,
                    display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.85rem',
                    background: nodeBg, border: `2px solid ${nodeColor}`,
                    boxShadow: running ? '0 0 0 4px rgba(129,140,248,0.14)' : 'none',
                    transition: 'all .2s',
                  }}>
                    {s.status === 'PASS' ? <CheckCircle2 size={16} color="var(--success)" />
                      : s.status === 'FAIL' ? <AlertTriangle size={15} color="var(--danger)" />
                      : running ? <Loader2 size={15} className="spin" color="var(--accent-primary)" />
                      : <span>{role.icon}</span>}
                  </div>
                  {!isLast && <div style={{ width: 2, flex: 1, minHeight: 14, background: spineColor, marginTop: 2, borderRadius: 2 }} />}
                </div>

                {/* Stage content: header + its steps branching off a guide line. */}
                <div style={{ flex: 1, minWidth: 0, paddingBottom: isLast ? 4 : 20 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: steps.length ? 10 : 0, flexWrap: 'wrap' }}>
                    <span style={{
                      fontSize: '0.64rem', fontWeight: 700, padding: '2px 8px', borderRadius: 20,
                      background: 'var(--bg-secondary, rgba(255,255,255,0.05))', color: role.tint,
                      border: `1px solid ${role.tint}33`, whiteSpace: 'nowrap',
                    }}>{role.icon} {role.label}</span>
                    <span style={{ fontSize: '0.7rem', fontWeight: 700, color: 'var(--text-muted)' }}>{s.scenario_num}</span>
                    <span style={{ fontSize: '0.88rem', fontWeight: 600 }}>{s.scenario_name}</span>
                    <span className="badge" style={{ background: badgeBg, color: badgeFg, fontSize: '0.62rem' }}>
                      {running ? <><Loader2 size={10} className="spin" /> running</>
                        : staleRunning ? 'INCOMPLETE' : s.status}
                    </span>
                    {(() => {
                      // In flight: count up from the last reported elapsed. Finished:
                      // show exactly what the backend recorded.
                      const b = baseline.current;
                      const secs = running && b && b.num === s.scenario_num
                        ? b.secs + (Date.now() - b.at) / 1000
                        : s.launch_time;
                      if (secs == null) return null;
                      return (
                        <span style={{
                          marginLeft: 'auto', fontSize: '0.72rem',
                          color: running ? 'var(--accent-primary)' : 'var(--text-muted)',
                          fontVariantNumeric: 'tabular-nums',
                        }}>
                          {fmtSecs(secs)}
                        </span>
                      );
                    })()}
                  </div>

                  {(steps.length > 0 || (running && steps.length === 0)) && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 3, borderLeft: '1px dashed var(--border-color)', paddingLeft: 14, marginLeft: 2 }}>
                      {steps.map((line, j) => {
                        const ok = /^\s*\[ok\]/i.test(line);
                        const fail = /^\s*\[FAIL\]/i.test(line);
                        const now = /^\s*▶/.test(line);       // step currently executing
                        const text = line.replace(/^\s*(\[(ok|FAIL)\]|▶)\s*/i, '');
                        return (
                          <div key={j} style={{ position: 'relative', display: 'flex', alignItems: 'flex-start', gap: 7,
                            fontSize: '0.76rem', lineHeight: 1.7,
                            color: fail ? 'var(--danger)' : now ? 'var(--accent-primary)' : 'var(--text-secondary)',
                            fontWeight: now ? 600 : 400,
                          }}>
                            {/* little branch stub from the guide line to this step's marker */}
                            <span style={{ position: 'absolute', left: -14, top: 11, width: 10, height: 1, background: 'var(--border-color)' }} />
                            {ok ? <CheckCircle2 size={13} color="var(--success)" style={{ flexShrink: 0, marginTop: 3 }} />
                              : fail ? <AlertTriangle size={13} color="var(--danger)" style={{ flexShrink: 0, marginTop: 3 }} />
                              : now ? <Loader2 size={13} className="spin" color="var(--accent-primary)" style={{ flexShrink: 0, marginTop: 3 }} />
                              : <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--border-color)', flexShrink: 0, marginTop: 6 }} />}
                            <span style={{ fontFamily: 'monospace' }}>{now ? `running: ${text}` : text}</span>
                          </div>
                        );
                      })}
                      {running && steps.length === 0 && (
                        <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
                          <Loader2 size={12} className="spin" /> executing…
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}


// ── Main RunDetails Page ─────────────────────────────────────────────────────

// ── AI Summary card (PM-friendly, auto-generated) ────────────────────────────
function AISummaryCard({ runId }: { runId: string }) {
  const [summary, setSummary] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancelled = false;
    getRunSummary(runId)
      .then(r => { if (!cancelled) setSummary(r.summary); })
      .catch(() => { if (!cancelled) setSummary(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [runId]);
  if (loading) return (
    <div className="card" style={{ marginBottom: 24, display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-secondary)' }}>
      <Loader2 size={16} className="spin" /> Generating AI summary…
    </div>
  );
  if (!summary) return null;
  return (
    <div className="card" style={{ marginBottom: 24, borderLeft: '4px solid var(--accent-primary)', background: 'rgba(99,102,241,0.06)' }}>
      <h3 style={{ margin: '0 0 8px', display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.95rem' }}>
        <Sparkles size={16} color="var(--accent-primary)" /> AI Summary
      </h3>
      <p style={{ margin: 0, lineHeight: 1.6, color: 'var(--text-primary)' }}>{summary}</p>
    </div>
  );
}

// ── Risk badge (test impact prediction) ──────────────────────────────────────
function riskColor(level: string): { bg: string; fg: string } {
  if (level === 'HIGH') return { bg: 'rgba(239,68,68,0.15)', fg: '#ef4444' };
  if (level === 'MEDIUM') return { bg: 'rgba(245,158,11,0.15)', fg: '#f59e0b' };
  return { bg: 'rgba(34,197,94,0.15)', fg: '#22c55e' };
}

function RiskPredictions({ runId }: { runId: string }) {
  const [preds, setPreds] = useState<RiskPrediction[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    getRiskPredictions(runId)
      .then(r => { if (!cancelled) setPreds(r.predictions); })
      .catch(() => { if (!cancelled) setPreds([]); });
    return () => { cancelled = true; };
  }, [runId]);
  if (!preds || preds.length === 0) return null;
  return (
    <div className="card" style={{ marginBottom: 24 }}>
      <h3 style={{ margin: '0 0 12px', display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.95rem' }}>
        <TrendingUp size={16} color="var(--accent-primary)" /> Failure-Risk Prediction
      </h3>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {preds.slice(0, 8).map(p => {
          const c = riskColor(p.risk_level);
          return (
            <div key={p.test} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
              <span style={{ fontFamily: 'monospace', fontSize: '0.82rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.test_name}</span>
              <span style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
                <span style={{ width: 90, height: 6, background: 'var(--bg-tertiary)', borderRadius: 999, overflow: 'hidden' }}>
                  <span style={{ display: 'block', height: '100%', width: `${p.risk_score}%`, background: c.fg }} />
                </span>
                <span style={{ background: c.bg, color: c.fg, padding: '2px 8px', borderRadius: 999, fontSize: '0.7rem', fontWeight: 700 }}>
                  {p.risk_level} {p.risk_score}
                </span>
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Visual regression tab ────────────────────────────────────────────────────
function VisualTab({ runId, canUpdateBaseline }: { runId: string; canUpdateBaseline: boolean }) {
  const [items, setItems] = useState<VisualRegressionItem[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [updating, setUpdating] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getVisualRegression(runId)
      .then(r => { if (!cancelled) setItems(r.results); })
      .catch(e => { if (!cancelled) setErr(String(e)); });
    return () => { cancelled = true; };
  }, [runId]);

  const onUpdate = async () => {
    setUpdating(true); setMsg(null);
    try { await updateVisualBaseline(runId); setMsg('Baseline updated ✓'); }
    catch (e) { setMsg('Update failed: ' + String(e)); }
    finally { setUpdating(false); }
  };

  const sev = (s: string) => s === 'high' ? { bg: 'rgba(239,68,68,0.15)', fg: '#ef4444' }
    : s === 'medium' ? { bg: 'rgba(245,158,11,0.15)', fg: '#f59e0b' }
    : { bg: 'rgba(148,163,184,0.15)', fg: '#94a3b8' };

  if (err) return <div className="card" style={{ color: 'var(--danger)' }}>Failed to load visual regression: {err}</div>;
  if (!items) return <div className="card" style={{ display: 'flex', gap: 10, alignItems: 'center' }}><Loader2 size={16} className="spin" /> Loading visual comparison…</div>;

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <p style={{ margin: 0, color: 'var(--text-secondary)' }}>
          {items.length === 0
            ? 'No baseline comparison recorded for this run.'
            : `${items.length} screen(s) compared · ${items.filter(i => !i.passed).length} regression(s)`}
        </p>
        {canUpdateBaseline && (
          <button className="btn" onClick={onUpdate} disabled={updating}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <RefreshCw size={14} className={updating ? 'spin' : ''} /> Update Baseline
          </button>
        )}
      </div>
      {msg && <div className="card" style={{ marginBottom: 16 }}>{msg}</div>}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
        {items.map(it => {
          const c = sev(it.severity);
          return (
            <div key={it.id} className="card">
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <ImageIcon size={16} color="var(--accent-primary)" />
                <strong>{it.screen_name}</strong>
                <span style={{ background: c.bg, color: c.fg, padding: '2px 10px', borderRadius: 999, fontSize: '0.72rem', fontWeight: 700 }}>
                  {it.severity.toUpperCase()} · {it.diff_percentage}%
                </span>
                {it.passed
                  ? <span style={{ color: '#22c55e', fontSize: '0.75rem' }}>within threshold</span>
                  : <span style={{ color: '#ef4444', fontSize: '0.75rem' }}>regression</span>}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
                {[['Baseline', it.baseline_image], ['Current', it.current_image], ['Diff', it.diff_image]].map(([label, src]) => (
                  <div key={label as string}>
                    <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: 6 }}>{label}</div>
                    {src
                      ? <img src={src as string} alt={label as string} style={{ width: '100%', borderRadius: 8, border: '1px solid var(--border-color)' }} />
                      : <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-secondary)', background: 'var(--bg-tertiary)', borderRadius: 8 }}>n/a</div>}
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Performance tab ──────────────────────────────────────────────────────────
export function gradeColor(grade: string | null | undefined): string {
  switch ((grade || '').toUpperCase()) {
    case 'A': return '#22c55e';
    case 'B': return '#3b82f6';
    case 'C': return '#eab308';
    case 'D': return '#f97316';
    default:  return '#ef4444';
  }
}

function MetricBar({ label, value, unit, pct, good }: { label: string; value: string; unit: string; pct: number; good: boolean }) {
  return (
    <div className="card" style={{ padding: '14px 16px' }}>
      <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{label}</div>
      <div style={{ fontSize: '1.4rem', fontWeight: 700, margin: '4px 0' }}>{value}<span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginLeft: 4 }}>{unit}</span></div>
      <div style={{ height: 5, background: 'var(--bg-tertiary)', borderRadius: 999, overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${Math.max(4, Math.min(100, pct))}%`, background: good ? '#22c55e' : pct > 66 ? '#ef4444' : '#eab308' }} />
      </div>
    </div>
  );
}

function PerformanceTab({ runId }: { runId: string }) {
  const [perf, setPerf] = useState<PerformanceResponse | null | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    getPerformance(runId)
      .then(p => { if (!cancelled) setPerf(p); })
      .catch(() => { if (!cancelled) setPerf(null); });
    return () => { cancelled = true; };
  }, [runId]);

  if (perf === undefined) return <div className="card" style={{ display: 'flex', gap: 10, alignItems: 'center' }}><Loader2 size={16} className="spin" /> Loading performance…</div>;
  if (perf === null) return <div className="card" style={{ color: 'var(--text-secondary)' }}>No performance data was collected for this run.</div>;

  const s = perf.summary;
  const g = gradeColor(perf.grade);
  const cmp = perf.comparison?.vs_previous_run;
  const sev = (issue: string) => /spike|failed|took|\b[89]\d%/.test(issue) ? '🔴' : /drop|slow|peak/i.test(issue) ? '🟡' : '🟢';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
      {/* 1. Score card */}
      <div className="card" style={{ display: 'flex', alignItems: 'center', gap: 24, borderLeft: `5px solid ${g}` }}>
        <div style={{ textAlign: 'center', minWidth: 120 }}>
          <div style={{ fontSize: '3.5rem', fontWeight: 800, color: g, lineHeight: 1 }}>{perf.grade || '—'}</div>
          <div style={{ fontSize: '1.1rem', fontWeight: 700 }}>{perf.score ?? '—'}<span style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>/100</span></div>
        </div>
        <div style={{ flex: 1 }}>
          <h2 style={{ margin: '0 0 6px', display: 'flex', alignItems: 'center', gap: 8 }}><Gauge size={20} color={g} /> Performance Score</h2>
          {cmp && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: cmp.better ? '#22c55e' : '#ef4444', fontSize: '0.9rem' }}>
              {cmp.better ? <ArrowUp size={15} /> : <ArrowDown size={15} />}
              {cmp.score_change !== null ? `${cmp.score_change > 0 ? '+' : ''}${cmp.score_change} pts` : ''} vs previous run
              {cmp.launch_time_change !== null && <span style={{ color: 'var(--text-secondary)' }}>· launch {cmp.launch_time_change > 0 ? '+' : ''}{cmp.launch_time_change}s</span>}
            </div>
          )}
        </div>
      </div>

      {/* 2. Key metrics row */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12 }}>
        <MetricBar label="Launch Time" value={s.app_launch_time_s != null ? s.app_launch_time_s.toFixed(1) : '—'} unit="s" pct={s.app_launch_time_s ? (s.app_launch_time_s / 3) * 100 : 0} good={(s.app_launch_time_s ?? 99) < 2} />
        <MetricBar label="Avg CPU" value={`${s.avg_cpu_percent?.toFixed(0) ?? '—'}`} unit="%" pct={s.avg_cpu_percent ?? 0} good={(s.avg_cpu_percent ?? 99) < 30} />
        <MetricBar label="Peak Memory" value={`${s.peak_memory_mb?.toFixed(0) ?? '—'}`} unit="MB" pct={s.peak_memory_mb ? (s.peak_memory_mb / 500) * 100 : 0} good={(s.peak_memory_mb ?? 999) < 300} />
        <MetricBar label="Avg FPS" value={s.avg_fps != null ? s.avg_fps.toFixed(0) : 'n/a'} unit="fps" pct={s.avg_fps ? 100 - (s.avg_fps / 60) * 100 : 0} good={(s.avg_fps ?? 0) > 55} />
        <MetricBar label="Avg API" value={s.avg_api_response_ms != null ? s.avg_api_response_ms.toFixed(0) : 'n/a'} unit="ms" pct={s.avg_api_response_ms ? (s.avg_api_response_ms / 500) * 100 : 0} good={(s.avg_api_response_ms ?? 999) < 300} />
      </div>

      {/* 3. CPU + Memory over time */}
      {perf.metrics_over_time.length > 0 && (
        <div className="card">
          <h3 style={{ margin: '0 0 12px', display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.95rem' }}><Cpu size={16} color="var(--accent-primary)" /> CPU &amp; Memory over time</h3>
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={perf.metrics_over_time.map((m, i) => ({ t: i, cpu: m.cpu, memory: m.memory }))}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-color)" />
              <XAxis dataKey="t" stroke="var(--text-secondary)" fontSize={11} />
              <YAxis stroke="var(--text-secondary)" fontSize={11} />
              <RTooltip contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--border-color)' }} />
              <Legend />
              <Line type="monotone" dataKey="cpu" name="CPU %" stroke="#3b82f6" dot={false} strokeWidth={2} />
              <Line type="monotone" dataKey="memory" name="Memory MB" stroke="#22c55e" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* 4. API response times */}
      <div className="card">
        <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem' }}>API Response Times</h3>
        {perf.api_calls.total_calls > 0 ? (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
            <thead><tr style={{ textAlign: 'left', color: 'var(--text-secondary)' }}>
              <th style={{ padding: '6px 8px' }}>Endpoint</th><th>Calls</th><th>Avg</th><th>Max</th><th>Status</th>
            </tr></thead>
            <tbody>
              <tr style={{ borderTop: '1px solid var(--border-color)' }}>
                <td style={{ padding: '6px 8px', fontFamily: 'monospace' }}>{perf.api_calls.slowest.url || 'all endpoints'}</td>
                <td>{perf.api_calls.total_calls}</td>
                <td>{perf.api_calls.avg_ms != null ? `${perf.api_calls.avg_ms}ms` : '—'}</td>
                <td>{perf.api_calls.slowest.ms != null ? `${perf.api_calls.slowest.ms}ms` : '—'}</td>
                <td>{(perf.api_calls.slowest.ms ?? 0) > 500 ? '⚠️ SLOW' : '✅ OK'}</td>
              </tr>
            </tbody>
          </table>
        ) : <div style={{ color: 'var(--text-secondary)' }}>No API calls captured (set <code>METRO_LOG_PATH</code> to enable).</div>}
      </div>

      {/* 5. Issues */}
      <div className="card">
        <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem' }}>Performance Issues</h3>
        {perf.issues.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {perf.issues.map((it, i) => <div key={i}>{sev(it)} {it}</div>)}
          </div>
        ) : <div style={{ color: '#22c55e' }}>🟢 No performance issues detected — all metrics within thresholds.</div>}
      </div>
    </div>
  );
}

export default function RunDetails() {
  const { id } = useParams<{id: string}>();
  const [run, setRun] = useState<TestRun | null>(null);
  const [rca, setRca] = useState<RCAReport | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<'analysis' | 'scenarios' | 'visual' | 'performance'>('analysis');
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
            {(run.flaky_detected || run.is_flaky) && (
              <span className="badge" style={{ background: 'rgba(245,158,11,0.15)', color: '#f59e0b', padding: '4px 10px', fontSize: '0.75rem', fontWeight: 700 }}
                title={run.attempts ? `Passed after ${run.attempts} attempts` : 'Flaky test'}>
                ⚡ FLAKY{run.attempts ? ` ×${run.attempts}` : ''}
              </span>
            )}
            {run.visual_warning && (
              <span className="badge" style={{ background: 'rgba(245,158,11,0.15)', color: '#f59e0b', padding: '4px 10px', fontSize: '0.75rem', fontWeight: 700 }}
                title="Visual regression detected">
                🖼️ VISUAL DIFF
              </span>
            )}
            {run.crash_detected && (
              <span className="badge" style={{ background: 'rgba(239,68,68,0.18)', color: '#ef4444', padding: '4px 10px', fontSize: '0.75rem', fontWeight: 700 }}
                title="The app crashed during this run (app bug, not automation) — crash logs collected">
                💥 APP CRASH
              </span>
            )}
            <h1 className="page-title" style={{ margin: 0 }}>{run.test_name}</h1>
          </div>
          <p className="page-subtitle">
            Suite: {run.test_suite} • Run ID: {run.id.substring(0,8)}... • 
            Time: {format(parseServerDate(run.created_at), "MMM d, yyyy h:mm a")} • 
            Device: {run.device_name}
          </p>
        </div>
      </header>

      {/* ── Tab bar ── */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 24, borderBottom: '1px solid var(--border-color)' }}>
        {(['analysis', 'scenarios', 'visual', 'performance'] as const).map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            style={{
              background: 'transparent', border: 'none', cursor: 'pointer',
              padding: '10px 18px', fontSize: '0.9rem', fontWeight: 600, fontFamily: 'inherit',
              display: 'inline-flex', alignItems: 'center', gap: 6,
              color: tab === t ? 'var(--accent-primary)' : 'var(--text-secondary)',
              borderBottom: tab === t ? '2px solid var(--accent-primary)' : '2px solid transparent',
              marginBottom: -1,
            }}
          >
            {t === 'analysis' ? 'Analysis' : t === 'scenarios' ? 'Scenarios'
              : t === 'visual' ? <><ImageIcon size={14} /> Visual</>
              : <><Gauge size={14} /> Performance</>}
            {t === 'visual' && run.visual_warning && (
              <span style={{ width: 7, height: 7, borderRadius: 999, background: '#f59e0b' }} />
            )}
          </button>
        ))}
      </div>

      {tab === 'performance' && id && <PerformanceTab runId={id} />}

      {tab === 'scenarios' && id && (
        <ScenariosTab runId={id} active={ACTIVE_RUN_STATES.has(run.status)} />
      )}

      {tab === 'visual' && id && (
        <VisualTab runId={id} canUpdateBaseline={run.status === 'passed'} />
      )}

      {tab === 'analysis' && (
      <>
      {/* ── Live View — shown for in-progress runs ── */}
      {id && <LiveView runId={id} jobState={run.job_state} />}

      {id && <AISummaryCard runId={id} />}
      {id && <RiskPredictions runId={id} />}

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
