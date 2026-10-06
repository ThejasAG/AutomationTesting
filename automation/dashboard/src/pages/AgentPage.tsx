import { useCallback, useEffect, useMemo, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Bot, Play, Square, RefreshCw, Loader2, CheckCircle2, XCircle, AlertTriangle, Wrench,
  Sparkles, ShieldCheck, Clock, ChevronDown, ChevronRight, Undo2, GitPullRequest, Mail,
} from 'lucide-react';
import { agentApi } from '../agentApi';
import type {
  AgentAction, AgentBatch, AgentFlow, AgentItem, AgentSettings, AgentStatus, Coverage, Feature, PreflightCheck,
} from '../agentApi';
import { parseServerDate } from '../time';

/** AI Agent — the autonomous test agent.
 *
 *  One place to see what the agent did overnight (every flow, what failed, what
 *  it retried, diagnosed, fixed or proposed), to review what needs a person
 *  (scenario proposals, app patches), and to configure the schedule. */

const STATUS: Record<string, { label: string; color: string }> = {
  passed: { label: 'Passed', color: '#34d399' },
  passed_on_retry: { label: 'Passed on retry', color: '#a3e635' },
  fixed: { label: 'Auto-fixed', color: '#22d3ee' },
  app_bug: { label: 'App bug', color: '#f87171' },
  infra: { label: 'Environment', color: '#fbbf24' },
  failed: { label: 'Needs a person', color: '#f87171' },
  running: { label: 'Running', color: '#60a5fa' },
  pending: { label: 'Waiting', color: '#94a3b8' },
  stopped: { label: 'Stopped', color: '#94a3b8' },
  skipped: { label: 'Skipped', color: '#94a3b8' },
};

const when = (iso?: string | null) => (iso ? parseServerDate(iso).toLocaleString() : '—');
const money = (n?: number) => `$${(n ?? 0).toFixed(2)}`;

const chip = (text: string, color: string): ReactNode => (
  <span style={{ padding: '3px 10px', borderRadius: 99, fontSize: '0.72rem', fontWeight: 600,
                 color, background: `${color}1f`, border: `1px solid ${color}40`, whiteSpace: 'nowrap' }}>
    {text}
  </span>
);

const ghost: CSSProperties = {
  background: 'transparent', border: '1px solid var(--border-highlight)', color: 'var(--text-primary)',
  padding: '8px 14px', borderRadius: 'var(--radius-md)', cursor: 'pointer', display: 'inline-flex',
  alignItems: 'center', gap: 6, font: 'inherit', fontSize: '0.85rem',
};
const input: CSSProperties = {
  background: 'var(--bg-tertiary)', border: '1px solid var(--border-highlight)', color: 'var(--text-primary)',
  padding: '8px 10px', borderRadius: 'var(--radius-sm)', font: 'inherit', fontSize: '0.85rem',
};
const section: CSSProperties = { marginTop: 24 };
const h3: CSSProperties = { fontSize: '1.05rem', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 };

export default function AgentPage() {
  const navigate = useNavigate();
  const [st, setSt] = useState<AgentStatus | null>(null);
  const [batches, setBatches] = useState<AgentBatch[]>([]);
  const [sel, setSel] = useState<string>('');
  const [detail, setDetail] = useState<{ batch: AgentBatch; items: AgentItem[]; actions: AgentAction[] } | null>(null);
  const [review, setReview] = useState<AgentAction[]>([]);
  const [fixes, setFixes] = useState<AgentAction[]>([]);
  const [analysis, setAnalysis] = useState<AgentAction | null>(null);
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [checks, setChecks] = useState<PreflightCheck[] | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [note, setNote] = useState('');
  const [runId, setRunId] = useState('');
  const [focus, setFocus] = useState('');

  const load = useCallback(async () => {
    try {
      const [s, b, r, f, m] = await Promise.all([
        agentApi.status(), agentApi.batches(),
        agentApi.actions('scenario_proposal,app_fix', 'proposed'),
        agentApi.actions('test_fix', 'applied,verified'),
        agentApi.analysis(),
      ]);
      setSt(s);
      setBatches(b.batches);
      setReview(r.actions);
      setFixes(f.actions);
      setAnalysis(m.analysis);
      if (!coverage) agentApi.inventory().then(setCoverage).catch(() => {});
      const pick = sel || s.batch_id || b.batches[0]?.id || '';
      if (pick) {
        setSel(pick);
        setDetail(await agentApi.batch(pick));
      }
      setErr('');
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [sel, coverage]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!st?.running) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [st?.running, load]);

  const act = async (label: string, fn: () => Promise<unknown>, done?: string) => {
    setBusy(label);
    setErr('');
    try {
      await fn();
      if (done) setNote(done);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const itemActions = useMemo(() => {
    const by: Record<string, AgentAction[]> = {};
    for (const a of detail?.actions ?? []) if (a.item_id) (by[a.item_id] ||= []).push(a);
    return by;
  }, [detail]);

  const ready = st?.secrets;
  const running = !!st?.running;

  return (
    <div className="animate-fade-in">
      <div className="page-header" style={{ display: 'flex', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <Bot size={34} color="var(--accent-primary)" /> AI Agent
          </h1>
          <p className="page-subtitle">
            Runs every flow unattended, sorts each failure, retries what is flaky, asks Claude to diagnose the rest,
            applies test fixes only after a re-run proves them, and sends a morning report.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
          {running ? (
            <button className="btn" style={{ background: 'var(--danger)', boxShadow: 'none' }}
                    disabled={!!busy} onClick={() => act('stop', agentApi.stop, 'Stopping after the current step…')}>
              <Square size={16} /> Stop
            </button>
          ) : (
            <button className="btn" disabled={!!busy} onClick={() => act('run', () => agentApi.run(), 'Batch started.')}>
              {busy === 'run' ? <Loader2 size={16} className="spin" /> : <Play size={16} />} Run all flows now
            </button>
          )}
          <button style={ghost} onClick={load}><RefreshCw size={14} /> Refresh</button>
        </div>
      </div>

      {err && <Banner color="#f87171" icon={<XCircle size={16} />}>{err}</Banner>}
      {note && !err && <Banner color="#34d399" icon={<CheckCircle2 size={16} />}>{note}</Banner>}

      {/* ── status ── */}
      <div className="card" style={{ padding: 20 }}>
        <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 260 }}>
            {running ? <Loader2 size={18} className="spin" color="#60a5fa" /> : <Clock size={18} color="var(--text-secondary)" />}
            <div>
              <div style={{ fontWeight: 600 }}>{running ? 'Working' : 'Idle'}</div>
              <div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
                {running ? (st?.phase || st?.busy || 'starting…')
                  : st?.settings.enabled ? `Next nightly run: ${st.next_run ? new Date(st.next_run).toLocaleString() : '—'}`
                  : 'Nightly schedule is off'}
              </div>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {chip(ready?.anthropic ? 'Claude: ready' : 'Claude: no API key', ready?.anthropic ? '#34d399' : '#fbbf24')}
            {chip(ready?.smtp ? 'Email: ready' : 'Email: not set up', ready?.smtp ? '#34d399' : '#94a3b8')}
            {chip(ready?.slack ? 'Slack: ready' : 'Slack: not set up', ready?.slack ? '#34d399' : '#94a3b8')}
            {chip(ready?.github ? 'GitHub: ready' : 'GitHub: no token', ready?.github ? '#34d399' : '#94a3b8')}
          </div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <button style={ghost} disabled={!!busy}
                    onClick={() => act('pre', async () => setChecks((await agentApi.preflight(st?.settings.env || 'staging')).checks))}>
              {busy === 'pre' ? <Loader2 size={14} className="spin" /> : <ShieldCheck size={14} />} Check the rig
            </button>
            {checks?.map(c => <span key={c.name}>{chip(`${c.name}: ${c.detail}`, c.ok ? '#34d399' : '#f87171')}</span>)}
          </div>
        </div>
        {!ready?.anthropic && (
          <p style={{ marginTop: 12, color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
            Without <code>ANTHROPIC_API_KEY</code> in <code>.env</code> the agent still runs, retries and reports — it just
            cannot diagnose or fix. Add the key and restart the backend (<code>./stop.sh && ./start.sh</code>).
          </p>
        )}
      </div>

      {/* ── review queue ── */}
      {review.length > 0 && (
        <div style={section}>
          <h3 style={h3}><AlertTriangle size={18} color="#fbbf24" /> Needs your review ({review.length})</h3>
          <div style={{ display: 'grid', gap: 12 }}>
            {review.map(a => <ReviewCard key={a.id} a={a} busy={busy}
              onApprove={() => act(a.id, () => agentApi.approve(a.id),
                a.kind === 'app_fix' ? 'Draft PR opened.' : 'Scenario added — it runs in the next batch.')}
              onApproveTest={running ? undefined : () => act(a.id, () => agentApi.approveTest(a.id),
                'Scenario added and running now — watch it in the Batch list.')}
              onReject={() => act(a.id, () => agentApi.reject(a.id), 'Rejected.')} />)}
          </div>
        </div>
      )}

      {/* ── batch ── */}
      <div style={section}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
          <h3 style={{ ...h3, marginBottom: 0 }}><Play size={18} /> Batch</h3>
          <select style={input} value={sel} onChange={e => setSel(e.target.value)}>
            {batches.length === 0 && <option value="">No batches yet</option>}
            {batches.map(b => (
              <option key={b.id} value={b.id}>
                {when(b.started_at)} · {b.trigger} · {b.env} · {b.status}
              </option>
            ))}
          </select>
          {detail && (
            <>
              <span style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
                Claude spend {money(detail.batch.spend_usd)} · {detail.items.length} flows
              </span>
              <button style={ghost} disabled={!!busy || detail.batch.status === 'running'}
                      onClick={() => act('mail', () => agentApi.resendReport(detail.batch.id), 'Report sent.')}>
                <Mail size={14} /> Send report
              </button>
            </>
          )}
        </div>
        {detail && (
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
            {Object.entries(detail.items.reduce<Record<string, number>>((m, i) => ({ ...m, [i.status]: (m[i.status] || 0) + 1 }), {}))
              .map(([k, v]) => <span key={k}>{chip(`${STATUS[k]?.label ?? k}: ${v}`, STATUS[k]?.color ?? '#94a3b8')}</span>)}
          </div>
        )}
        <div style={{ display: 'grid', gap: 8 }}>
          {detail?.items.map(it => {
            const s = STATUS[it.status] ?? { label: it.status, color: '#94a3b8' };
            const isOpen = !!open[it.id];
            const acts = itemActions[it.id] ?? [];
            return (
              <div key={it.id} className="card" style={{ padding: 0, transform: 'none' }}>
                <div onClick={() => setOpen(o => ({ ...o, [it.id]: !isOpen }))}
                     style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '14px 18px', cursor: 'pointer' }}>
                  {isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600 }}>{it.flow_name}</div>
                    {it.root_cause && it.status !== 'passed' && (
                      <div style={{ color: 'var(--text-secondary)', fontSize: '0.82rem', overflow: 'hidden',
                                    textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.root_cause}</div>
                    )}
                  </div>
                  {it.category && it.status !== 'passed' && chip(it.category, '#a78bfa')}
                  {it.attempts > 1 && chip(`${it.attempts} attempts`, '#94a3b8')}
                  {it.status === 'running' && <Loader2 size={14} className="spin" color="#60a5fa" />}
                  {chip(s.label, s.color)}
                </div>
                {isOpen && (
                  <div style={{ borderTop: '1px solid var(--border-color)', padding: '14px 18px 18px 46px', fontSize: '0.88rem' }}>
                    <div style={{ display: 'flex', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
                      {it.run_id && <button style={ghost} onClick={() => navigate(`/run/${it.run_id}`)}>First run</button>}
                      {it.final_run_id && it.final_run_id !== it.run_id &&
                        <button style={ghost} onClick={() => navigate(`/run/${it.final_run_id}`)}>Last run</button>}
                    </div>
                    {it.verdict?.root_cause && (
                      <div style={{ marginBottom: 10 }}>
                        <div style={{ fontWeight: 600, marginBottom: 4 }}>
                          Claude: {it.verdict.category} · confidence {it.verdict.confidence} · {it.verdict.recommended_action}
                        </div>
                        <div>{it.verdict.root_cause}</div>
                        {!!it.verdict.evidence?.length && (
                          <ul style={{ margin: '6px 0 0 18px', color: 'var(--text-secondary)' }}>
                            {it.verdict.evidence.map((e, i) => <li key={i}>{e}</li>)}
                          </ul>
                        )}
                      </div>
                    )}
                    {acts.length > 0 && (
                      <div style={{ display: 'grid', gap: 6 }}>
                        {acts.map(a => (
                          <div key={a.id} style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                            {chip(a.kind, '#818cf8')} {chip(a.status, a.status === 'verified' || a.status === 'done' ? '#34d399'
                              : a.status === 'failed' || a.status === 'rolled_back' || a.status === 'rejected' ? '#f87171' : '#fbbf24')}
                            <span>{a.title}</span>
                            {a.cost_usd > 0 && <span style={{ color: 'var(--text-muted)' }}>{money(a.cost_usd)}</span>}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
          {detail && detail.items.length === 0 && <p style={{ color: 'var(--text-secondary)' }}>No flows in this batch.</p>}
        </div>
      </div>

      {/* ── applied test fixes ── */}
      {fixes.length > 0 && (
        <div style={section}>
          <h3 style={h3}><Wrench size={18} /> Test fixes the agent applied</h3>
          <div style={{ display: 'grid', gap: 8 }}>
            {fixes.map(a => (
              <div key={a.id} className="card" style={{ padding: '12px 18px', display: 'flex', gap: 12, alignItems: 'center', transform: 'none' }}>
                {chip(a.status === 'verified' ? 'verified by re-run' : a.status, a.status === 'verified' ? '#34d399' : '#fbbf24')}
                <span style={{ flex: 1 }}>{String(a.detail.flow_id ?? '')} — {a.title}</span>
                <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>{when(a.created_at)}</span>
                <button style={ghost} disabled={!!busy}
                        onClick={() => act(a.id, () => agentApi.rollback(a.id), 'Fix rolled back — the flow is as before.')}>
                  <Undo2 size={14} /> Roll back
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── on demand ── */}
      <div style={section}>
        <h3 style={h3}><Sparkles size={18} /> Ask the agent</h3>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 16 }}>
          <div className="card" style={{ padding: 20, transform: 'none' }}>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>Diagnose a failed run</div>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 10 }}>
              Paste a failed cross-app run id. Claude finds the cause and, if allowed, fixes and re-verifies it.
            </p>
            <div style={{ display: 'flex', gap: 8 }}>
              <input style={{ ...input, flex: 1 }} placeholder="run id" value={runId} onChange={e => setRunId(e.target.value.trim())} />
              <button className="btn" style={{ padding: '8px 14px' }} disabled={!runId || !!busy || running}
                      onClick={() => act('analyze', () => agentApi.analyze(runId), 'Diagnosis started — follow it in the batch list.')}>
                Diagnose
              </button>
            </div>
          </div>
          <div className="card" style={{ padding: 20, transform: 'none' }}>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>Analyze the application</div>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 10 }}>
              Claude studies both apps' code and every screen, rates which features the tests cover, and writes
              new scenarios for the gaps. They run only after you approve them.
            </p>
            <div style={{ display: 'flex', gap: 8 }}>
              <input style={{ ...input, flex: 1 }} placeholder="focus (optional), e.g. refunds, inventory"
                     value={focus} onChange={e => setFocus(e.target.value)} />
              <button className="btn" style={{ padding: '8px 14px' }} disabled={!!busy || running || !st?.claude_ready}
                      title={st?.claude_ready ? '' : 'Needs ANTHROPIC_API_KEY in .env'}
                      onClick={() => act('gen', () => agentApi.generate(focus),
                        'Analysis started — features and new scenarios appear here when Claude finishes (a few minutes).')}>
                {busy === 'gen' ? <Loader2 size={14} className="spin" /> : <Sparkles size={14} />} Analyze
              </button>
            </div>
          </div>
        </div>
      </div>

      <CoverageSection coverage={coverage} analysis={analysis}
        onRefresh={() => act('inv', async () => setCoverage(await agentApi.inventory()))} busy={busy} />

      {st && <SettingsCard s={st.settings} busy={busy}
        onSave={(ch) => act('settings', () => agentApi.saveSettings(ch), 'Settings saved.')} />}
    </div>
  );
}

function Banner({ color, icon, children }: { color: string; icon: ReactNode; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '10px 14px', marginBottom: 16,
                  borderRadius: 'var(--radius-md)', color, background: `${color}14`, border: `1px solid ${color}33` }}>
      {icon}<span>{children}</span>
    </div>
  );
}

function ReviewCard({ a, busy, onApprove, onApproveTest, onReject }: {
  a: AgentAction; busy: string; onApprove: () => void; onApproveTest?: () => void; onReject: () => void;
}) {
  const [show, setShow] = useState(false);
  const isPatch = a.kind === 'app_fix';
  const flow = a.detail.flow as { description?: string; rationale?: string;
    segments?: { name: string; role: string; steps: string[] }[] } | undefined;
  return (
    <div className="card" style={{ padding: 18, transform: 'none' }}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        {chip(isPatch ? 'App patch' : 'New scenario', isPatch ? '#f87171' : '#22d3ee')}
        <span style={{ fontWeight: 600, flex: 1 }}>{a.title}
          {typeof a.detail.feature === 'string' && a.detail.feature &&
            <span style={{ color: 'var(--text-secondary)', fontWeight: 400 }}> · {a.detail.feature}</span>}</span>
        <button style={ghost} onClick={() => setShow(s => !s)}>{show ? 'Hide' : 'Details'}</button>
        <button className="btn" style={{ padding: '8px 14px' }} disabled={!!busy} onClick={onApprove}>
          {busy === a.id ? <Loader2 size={14} className="spin" /> : isPatch ? <GitPullRequest size={14} /> : <CheckCircle2 size={14} />}
          {isPatch ? 'Open draft PR' : 'Approve'}
        </button>
        {!isPatch && onApproveTest && (
          <button style={ghost} disabled={!!busy} onClick={onApproveTest}><Play size={14} /> Approve &amp; test now</button>
        )}
        <button style={ghost} disabled={!!busy} onClick={onReject}><XCircle size={14} /> Reject</button>
      </div>
      {show && (
        <div style={{ marginTop: 12, fontSize: '0.85rem' }}>
          {isPatch ? (
            <>
              <p style={{ marginBottom: 6 }}><b>Cause:</b> {String(a.detail.root_cause ?? '')}</p>
              <p style={{ marginBottom: 6, color: 'var(--text-secondary)' }}>
                {String(a.detail.app ?? '')} app · {String(a.detail.check ?? '')}
                {a.detail.pr_error ? ` · last PR attempt: ${String(a.detail.pr_error)}` : ''}
              </p>
              <pre style={{ background: 'var(--bg-primary)', padding: 12, borderRadius: 8, overflowX: 'auto',
                            fontSize: '0.78rem' }}>{String(a.detail.patch ?? '')}</pre>
            </>
          ) : (
            <>
              {flow?.rationale && <p style={{ marginBottom: 8 }}><b>Why:</b> {flow.rationale}</p>}
              {flow?.segments?.map((s, i) => (
                <div key={i} style={{ marginBottom: 8 }}>
                  <div style={{ fontWeight: 600 }}>{i + 1}. [{s.role}] {s.name}</div>
                  <ol style={{ margin: '4px 0 0 22px', color: 'var(--text-secondary)' }}>
                    {s.steps.map((x, j) => <li key={j}><code>{x}</code></li>)}
                  </ol>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

function SettingsCard({ s, busy, onSave }: {
  s: AgentSettings; busy: string; onSave: (ch: Partial<AgentSettings>) => void;
}) {
  const [f, setF] = useState<AgentSettings>(s);
  const [flows, setFlows] = useState<AgentFlow[]>([]);
  const [emails, setEmails] = useState(s.report_emails.join(', '));
  useEffect(() => { setF(s); setEmails(s.report_emails.join(', ')); }, [s]);
  useEffect(() => { agentApi.flows().then(r => setFlows(r.flows)).catch(() => {}); }, []);
  const set = <K extends keyof AgentSettings>(k: K, v: AgentSettings[K]) => setF(x => ({ ...x, [k]: v }));
  const row: CSSProperties = { display: 'flex', alignItems: 'center', gap: 10, minHeight: 36 };
  const label: CSSProperties = { width: 210, color: 'var(--text-secondary)', fontSize: '0.88rem', flexShrink: 0 };
  const box = (k: 'enabled' | 'include_demos' | 'triage' | 'autofix_tests' | 'report_slack' | 'generator_enabled', text: string) => (
    <label style={row}><input type="checkbox" checked={f[k]} onChange={e => set(k, e.target.checked)} /> {text}</label>
  );
  const chosen = new Set(f.flows);
  return (
    <div style={section}>
      <h3 style={h3}><Wrench size={18} /> Settings</h3>
      <div className="card" style={{ padding: 22, transform: 'none', display: 'grid', gap: 10 }}>
        {box('enabled', 'Run every night')}
        <div style={row}><span style={label}>Nightly start time</span>
          <input type="time" style={input} value={f.nightly_time} onChange={e => set('nightly_time', e.target.value)} /></div>
        <div style={row}><span style={label}>Environment</span>
          <select style={input} value={f.env} onChange={e => set('env', e.target.value)}>
            <option value="staging">Staging</option><option value="prod">Old Vya (prod)</option></select></div>
        <div style={{ ...row, alignItems: 'flex-start' }}><span style={label}>Flows (none ticked = all except demos)</span>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {flows.map(fl => (
              <label key={fl.id} style={{ display: 'inline-flex', gap: 6, alignItems: 'center', fontSize: '0.82rem',
                                          padding: '4px 10px', borderRadius: 99, border: '1px solid var(--border-highlight)' }}>
                <input type="checkbox" checked={chosen.has(fl.id)}
                       onChange={e => set('flows', e.target.checked ? [...f.flows, fl.id] : f.flows.filter(x => x !== fl.id))} />
                {fl.name}{fl.agent ? ' ✦' : ''}
              </label>
            ))}
          </div></div>
        {box('include_demos', 'Include the quick demo flows when none are ticked')}
        {box('triage', 'Ask Claude to diagnose failures')}
        {box('autofix_tests', 'Apply test fixes (kept only if a re-run passes)')}
        <div style={row}><span style={label}>App fixes</span>
          <select style={input} value={f.app_fix_mode} onChange={e => set('app_fix_mode', e.target.value as AgentSettings['app_fix_mode'])}>
            <option value="off">Off</option>
            <option value="propose">Propose a patch for review</option>
            <option value="pr">Open a draft PR automatically</option>
          </select></div>
        <div style={row}><span style={label}>Claude budget per batch (USD)</span>
          <input type="number" min={0} step={1} style={{ ...input, width: 100 }} value={f.budget_usd}
                 onChange={e => set('budget_usd', Number(e.target.value))} /></div>
        <div style={row}><span style={label}>Fix attempts per failure</span>
          <input type="number" min={1} max={5} style={{ ...input, width: 100 }} value={f.max_fix_attempts}
                 onChange={e => set('max_fix_attempts', Number(e.target.value))} /></div>
        <div style={row}><span style={label}>Wait for staging (minutes)</span>
          <input type="number" min={0} max={120} style={{ ...input, width: 100 }} value={f.infra_wait_minutes}
                 onChange={e => set('infra_wait_minutes', Number(e.target.value))} /></div>
        <div style={row}><span style={label}>Report emails</span>
          <input style={{ ...input, flex: 1 }} placeholder="qa@company.com, dev@company.com" value={emails}
                 onChange={e => setEmails(e.target.value)} /></div>
        {box('report_slack', 'Also post the report to Slack')}
        {box('generator_enabled', 'Propose new scenarios every week')}
        <div style={row}><span style={label}>Scenario day</span>
          <select style={input} value={f.generator_day} onChange={e => set('generator_day', e.target.value)}>
            {DAYS.map(d => <option key={d} value={d}>{d}</option>)}</select></div>
        <div style={row}><span style={label}>Model</span><code>{f.model}</code></div>
        <div>
          <button className="btn" disabled={!!busy}
                  onClick={() => onSave({ ...f, report_emails: emails.split(',').map(x => x.trim()).filter(Boolean) })}>
            {busy === 'settings' ? <Loader2 size={16} className="spin" /> : <CheckCircle2 size={16} />} Save settings
          </button>
        </div>
      </div>
    </div>
  );
}

const COV: Record<string, string> = { covered: '#34d399', partial: '#fbbf24', missing: '#f87171' };
const RISK: Record<string, string> = { high: '#f87171', medium: '#fbbf24', low: '#94a3b8' };

function Bar({ value, total }: { value: number; total: number }) {
  const pct = total ? Math.round((value / total) * 100) : 0;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <div style={{ flex: 1, height: 8, borderRadius: 99, background: 'var(--bg-tertiary)', overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: pct >= 70 ? '#34d399' : pct >= 40 ? '#fbbf24' : '#f87171' }} />
      </div>
      <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', minWidth: 90 }}>{value}/{total} ({pct}%)</span>
    </div>
  );
}

function CoverageSection({ coverage, analysis, onRefresh, busy }: {
  coverage: Coverage | null; analysis: AgentAction | null; onRefresh: () => void; busy: string;
}) {
  const [showApp, setShowApp] = useState<string>('');
  const features = (analysis?.detail.features as Feature[] | undefined) ?? [];
  const summary = typeof analysis?.detail.summary === 'string' ? analysis.detail.summary : '';
  const order = { missing: 0, partial: 1, covered: 2 } as Record<string, number>;
  const riskOrder = { high: 0, medium: 1, low: 2 } as Record<string, number>;
  const sorted = [...features].sort((a, b) =>
    (order[a.coverage] - order[b.coverage]) || (riskOrder[a.risk] - riskOrder[b.risk]));
  return (
    <div style={section}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <h3 style={{ ...h3, marginBottom: 0 }}><ShieldCheck size={18} /> Application coverage</h3>
        <button style={ghost} disabled={!!busy} onClick={onRefresh}>
          {busy === 'inv' ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} Recompute
        </button>
      </div>
      <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 12 }}>
        Every screen found in the apps' navigation, and whether any test drives one of its own element ids.
        Computed from the source — no AI.
      </p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 16 }}>
        {coverage ? Object.entries(coverage).map(([app, a]) => (
          <div key={app} className="card" style={{ padding: 20, transform: 'none' }}>
            <div style={{ fontWeight: 600, marginBottom: 10, textTransform: 'capitalize' }}>{app} app</div>
            <div style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>Screens tested</div>
            <Bar value={a.screens_covered} total={a.screens} />
            <div style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginTop: 8 }}>Element ids used by tests</div>
            <Bar value={a.ids_covered} total={a.ids} />
            <button style={{ ...ghost, marginTop: 12 }} onClick={() => setShowApp(showApp === app ? '' : app)}>
              {showApp === app ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Screens
            </button>
            {showApp === app && (
              <div style={{ marginTop: 10, display: 'grid', gap: 4, fontSize: '0.82rem' }}>
                {[...a.routes].sort((x, y) => Number(x.covered) - Number(y.covered)).map((r, i) => (
                  <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                    {chip(r.covered ? 'tested' : 'untested', r.covered ? '#34d399' : '#f87171')}
                    <span style={{ fontWeight: 600 }}>{r.route}</span>
                    <span style={{ color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {r.file ?? ''} · {r.ids} ids{r.covered_ids.length ? ` · uses ${r.covered_ids.slice(0, 3).join(', ')}` : ''}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )) : <div className="card" style={{ padding: 20 }}><Loader2 size={16} className="spin" /> Mapping the apps…</div>}
      </div>

      {analysis && (
        <div className="card" style={{ padding: 20, marginTop: 16, transform: 'none' }}>
          <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap', marginBottom: 10 }}>
            <span style={{ fontWeight: 600 }}>Feature coverage — Claude's analysis</span>
            <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
              {when(analysis.created_at)} · {money(analysis.cost_usd)}{analysis.status === 'failed' ? ` · did not finish (${String(analysis.detail.error || analysis.detail.stop || '')})` : ''}
            </span>
            {features.length > 0 && (['missing', 'partial', 'covered'] as const).map(c =>
              <span key={c}>{chip(`${c}: ${features.filter(f => f.coverage === c).length}`, COV[c])}</span>)}
          </div>
          {sorted.length > 0 && (
            <div style={{ display: 'grid', gap: 6, fontSize: '0.85rem' }}>
              {sorted.map((f, i) => (
                <div key={i} style={{ display: 'grid', gridTemplateColumns: '90px 70px 1fr', gap: 10, alignItems: 'baseline',
                                      padding: '6px 0', borderTop: i ? '1px solid var(--border-color)' : 'none' }}>
                  {chip(f.coverage, COV[f.coverage] ?? '#94a3b8')}
                  {chip(`${f.risk} risk`, RISK[f.risk] ?? '#94a3b8')}
                  <div>
                    <b>{f.name}</b> <span style={{ color: 'var(--text-muted)' }}>({f.app}{f.screens.length ? ` · ${f.screens.join(', ')}` : ''})</span>
                    {f.covered_by.length > 0 && <span style={{ color: 'var(--text-secondary)' }}> — covered by {f.covered_by.join(', ')}</span>}
                    {f.notes && <div style={{ color: 'var(--text-secondary)' }}>{f.notes}</div>}
                  </div>
                </div>
              ))}
            </div>
          )}
          {summary && (
            <details style={{ marginTop: 12 }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)' }}>How the agent understands the app</summary>
              <pre style={{ whiteSpace: 'pre-wrap', marginTop: 8, fontFamily: 'inherit', fontSize: '0.85rem',
                            color: 'var(--text-secondary)' }}>{summary}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
