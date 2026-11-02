import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import {
  AlertTriangle, Bug, CheckCircle2, ChevronDown, ChevronRight, FileText, GitMerge, ListChecks, Loader2,
  RotateCw, ServerCrash, ShieldCheck, Sparkles, UserRound, Wrench, XCircle,
} from 'lucide-react';
import { agentApi, isActiveStatus } from '../../agentApi';
import type { ApproveResult, Session, SessionDetail as Detail, SessionOptions, SessionSummary, RunRow } from '../../agentApi';
import {
  AMBER, GREEN, GREY, KIND, RED, TRIGGER, chip, fmtDur, ghost, h3, mono, ms, muted, runColor, section,
  statusChip, td, th, when, wrapText,
} from './ui';
import type { RestartState } from './ui';
import { Banner, ClampedPre, DiffView, RunLink } from './Bits';
import EventFeed from './EventFeed';

const arr = <T,>(x: unknown): T[] => (Array.isArray(x) ? (x as T[]) : []);
const card = { padding: 20, transform: 'none', marginTop: 12, maxWidth: '100%', minWidth: 0, ...wrapText } as const;

/** One session: what it was asked, what it found, what it changed, and — when it
 *  is waiting for approval — the checks and the diff to decide on. Mount with key={id}. */
export default function SessionDetail({ id, onChanged, restart, onRestart }: {
  id: string; onChanged: () => void; restart: RestartState; onRestart: () => void;
}) {
  const [s, setS] = useState<Detail | null>(null);
  const [err, setErr] = useState('');
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState('');
  const [actErr, setActErr] = useState('');
  const [merged, setMerged] = useState<ApproveResult | null>(null);
  const [activity, setActivity] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const d = await agentApi.session(id);
        if (cancelled) return;
        setS(d);
        setErr('');
        if (isActiveStatus(d.status)) timer = setTimeout(load, 5000);
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      }
    };
    load();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [id, reload]);

  if (err && !s) return <div style={section}><Banner color={RED} icon={<XCircle size={16} />}>{err}</Banner></div>;
  if (!s) {
    return (
      <div style={section}>
        <div className="card" style={{ padding: 20 }}><Loader2 size={16} className="spin" /> Loading the session…</div>
      </div>
    );
  }

  const active = isActiveStatus(s.status);
  const inReview = s.status === 'review' || s.status === 'gates_failed';
  const summary: SessionSummary = s.summary || {};
  const r = s.result || {};
  const end = s.finished_at ? ms(s.finished_at) : Date.now();
  const began = ms(s.started_at || s.created_at);

  const decide = async (what: 'approve' | 'reject') => {
    const q = what === 'approve'
      ? `Merge branch ${s.branch ?? '(none)'} into your current branch?`
      : `Reject this change?${s.branch ? ` Branch ${s.branch} is deleted.` : ''}`;
    if (!window.confirm(q)) return;
    setBusy(what);
    setActErr('');
    try {
      if (what === 'approve') setMerged(await agentApi.approve(s.id));
      else await agentApi.reject(s.id);
      setReload(n => n + 1);
      onChanged();
    } catch (e) {
      setActErr((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  // Size is the one check that depends on a setting: when it is the only one that
  // failed, it can be checked again with the current limit.
  const onlySizeFailed = s.status === 'gates_failed'
    && s.gates.filter(g => !g.ok).map(g => g.name).join() === 'size';
  const recheckSize = async () => {
    setBusy('recheck');
    setActErr('');
    try {
      const out = await agentApi.recheckSize(s.id);
      if (!out.ok) setActErr(`Still too big: ${out.detail}. Raise "Largest change it may propose" in the settings, or reject it.`);
      setReload(n => n + 1);
      onChanged();
    } catch (e) {
      setActErr((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  const resume = async () => {
    if (!window.confirm('Continue this session where it stopped (same worktree, same Claude conversation)?')) return;
    setBusy('continue');
    setActErr('');
    try {
      await agentApi.continueSession(s.id);
      setReload(n => n + 1);
      onChanged();
    } catch (e) {
      setActErr((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  return (
    <div style={section}>
      <h3 style={h3}><FileText size={18} /> {KIND[s.kind] ?? s.kind}
        <span style={{ ...muted, fontWeight: 400 }}>· {when(s.started_at || s.created_at)}</span></h3>

      {/* ── overview ── */}
      <div className="card" style={{ ...card, marginTop: 0 }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', ...muted }}>
          {statusChip(s.status)}
          <span style={wrapText}>{TRIGGER[s.trigger] ?? s.trigger}{s.created_by ? ` · ${s.created_by}` : ''}</span>
          <span>· {fmtDur(end - began)}{active ? ' so far' : ''}</span>
          {s.branch && <span style={wrapText}>· branch <code style={{ ...mono, ...wrapText }}>{s.branch}</code></span>}
          {r.turns != null && <span>· {r.turns} turns</span>}
        </div>
        <Asked o={s.options} />
        {summary.headline && <div style={{ fontWeight: 600, fontSize: '1rem', marginTop: 12, ...wrapText }}>{summary.headline}</div>}
        {active && (
          <p style={{ ...muted, marginTop: 10 }}>
            Working{s.phase ? ` — ${s.phase}` : ''}. Follow it live above; the report appears here when it finishes.
          </p>
        )}
        {(r.timed_out || r.is_error) && (
          <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
            {r.timed_out && chip('Reached its time limit', AMBER)}
            {r.is_error && !r.timed_out && chip('Claude Code stopped early', RED)}
          </div>
        )}
        {s.error && <div style={{ marginTop: 12 }}><Banner color={RED} icon={<AlertTriangle size={16} />}>{s.error}</Banner></div>}
        {s.decided_at && (s.status === 'approved' || s.status === 'rejected') && (
          <p style={{ ...muted, marginTop: 10 }}>
            {s.status === 'approved' ? 'Approved' : 'Rejected'}{s.decision_by ? ` by ${s.decision_by}` : ''} · {when(s.decided_at)}
          </p>
        )}
      </div>

      {/* ── interrupted: pick it up again ── */}
      {s.status === 'interrupted' && (
        <div className="card" style={{ ...card, borderColor: `${AMBER}66` }}>
          <div style={{ fontWeight: 600, display: 'flex', gap: 8, alignItems: 'center', marginBottom: 4 }}>
            <AlertTriangle size={18} color={AMBER} /> This session was interrupted
          </div>
          <p style={{ ...muted, marginBottom: 12 }}>
            Its process ended (the Mac restarted or the platform was stopped). Continue picks it up where
            it stopped: same worktree, same edits and runs, same Claude conversation.
          </p>
          <button className="btn" disabled={!!busy} onClick={resume}>
            {busy === 'continue' ? <Loader2 size={16} className="spin" /> : <RotateCw size={16} />} Continue where it stopped
          </button>
          {actErr && <div style={{ color: RED, fontSize: '0.85rem', marginTop: 10, ...wrapText }}>{actErr}</div>}
        </div>
      )}

      {/* ── review ── */}
      {inReview && (
        <div className="card" style={{ ...card, borderColor: s.status === 'review' ? `${AMBER}66` : `${RED}66` }}>
          <div style={{ fontWeight: 600, display: 'flex', gap: 8, alignItems: 'center', marginBottom: 4 }}>
            <ShieldCheck size={18} color={s.status === 'review' ? AMBER : RED} />
            {s.status === 'review' ? 'Waiting for your approval' : 'A check failed — this change cannot be merged'}
          </div>
          <p style={{ ...muted, marginBottom: 12 }}>
            {s.status === 'review'
              ? 'Every check passed. Approve merges the session\'s branch into the checkout the backend runs from.'
              : onlySizeFailed
                ? 'Only the size check failed. Check it again with the current limit (AI Agent settings; it grows with each new scenario), or reject it to delete the branch.'
                : 'Reject it to delete the branch, or start a new session.'}
          </p>
          <Gates s={s} />
          <Change s={s} />
          <div style={{ display: 'flex', gap: 10, marginTop: 16, flexWrap: 'wrap', alignItems: 'center' }}>
            {s.status === 'review' && (
              <button className="btn" disabled={!!busy} onClick={() => decide('approve')}>
                {busy === 'approve' ? <Loader2 size={16} className="spin" /> : <GitMerge size={16} />} Approve &amp; merge
              </button>
            )}
            {onlySizeFailed && (
              <button className="btn" disabled={!!busy} onClick={recheckSize}
                      title="Only the size failed: check it again with the current limit (it grows with each new scenario)">
                {busy === 'recheck' ? <Loader2 size={16} className="spin" /> : <ShieldCheck size={16} />} Check the size again
              </button>
            )}
            <button style={ghost} disabled={!!busy} onClick={() => decide('reject')}>
              {busy === 'reject' ? <Loader2 size={14} className="spin" /> : <XCircle size={14} />} Reject
            </button>
          </div>
          {actErr && <div style={{ color: RED, fontSize: '0.85rem', marginTop: 10, ...wrapText }}>{actErr}</div>}
        </div>
      )}

      {/* ── merged: load it ── */}
      {(merged || s.status === 'approved') && (
        <div className="card" style={{ ...card, borderColor: `${GREEN}55` }}>
          <div style={{ fontWeight: 600, display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
            <CheckCircle2 size={18} color={GREEN} />
            <span style={wrapText}>
              {merged ? <>Merged <code style={mono}>{merged.branch}</code> into <code style={mono}>{merged.into}</code></> : 'Approved and merged'}
            </span>
          </div>
          {merged?.check_flows && (
            <>
              <div style={{ ...muted, marginTop: 8 }}>Flow check after the merge:</div>
              <ClampedPre text={merged.check_flows} lines={6} />
            </>
          )}
          <p style={{ ...muted, margin: '10px 0' }}>The backend loads the change the next time it restarts.</p>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            <button className="btn" disabled={restart.phase === 'restarting'} onClick={onRestart}>
              {restart.phase === 'restarting' ? <Loader2 size={16} className="spin" /> : <RotateCw size={16} />}
              {restart.phase === 'restarting' ? 'Restarting…' : 'Restart backend to load it'}
            </button>
            {restart.phase === 'done' && <span style={{ color: GREEN, fontSize: '0.85rem' }}>Backend restarted</span>}
            {restart.error && <span style={{ color: RED, fontSize: '0.85rem', ...wrapText }}>{restart.error}</span>}
          </div>
        </div>
      )}

      {/* ── what changed, outside review ── */}
      {!inReview && s.files_changed?.length > 0 && (
        <div className="card" style={card}>
          <div style={{ fontWeight: 600, marginBottom: 8 }}>What it changed</div>
          <Change s={s} />
        </div>
      )}

      {/* ── report ── */}
      {s.report?.trim() && (
        <div className="card" style={card}>
          <div className="markdown-body" style={{ fontSize: '0.88rem', maxWidth: '100%', overflowX: 'auto', ...wrapText }}>
            <ReactMarkdown>{s.report}</ReactMarkdown>
          </div>
        </div>
      )}

      <Summary summary={summary} patches={s.app_patches ?? []} />
      <Runs runs={s.runs ?? []} />

      {!active && (
        <div style={{ marginTop: 12 }}>
          <button style={ghost} onClick={() => setActivity(a => !a)}>
            {activity ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Activity
          </button>
          {activity && <div style={{ marginTop: 10, minWidth: 0 }}><EventFeed key={s.id} sessionId={s.id} live={false} maxHeight={560} /></div>}
        </div>
      )}
    </div>
  );
}

/** What the session was asked to do. */
function Asked({ o }: { o: SessionOptions }) {
  const parts: ReactNode[] = [];
  if (o.run_ids?.length) parts.push(<span key="r">run {o.run_ids.map(r => <RunLink key={r} id={r} />)}</span>);
  if (o.flow_ids?.length) parts.push(<span key="f">flows <code style={mono}>{o.flow_ids.join(', ')}</code></span>);
  if (o.mode) parts.push(<span key="m">{o.mode} analysis</span>);
  if (o.max_new) parts.push(<span key="n">up to {o.max_new} new</span>);
  if (o.focus) parts.push(<span key="o">focus “{o.focus}”</span>);
  if (o.features?.length) {
    parts.push(<span key="x">{o.features.length} feature gap{o.features.length > 1 ? 's' : ''}
      <span title={o.features.join('\n')} style={{ textDecoration: 'underline dotted', marginLeft: 4 }}>(list)</span></span>);
  }
  if (!parts.length) return null;
  return (
    <div style={{ ...muted, marginTop: 8, display: 'flex', gap: 10, flexWrap: 'wrap' }}>
      <span>Asked for:</span>{parts.map((p, i) => <span key={i} style={wrapText}>{i ? '· ' : ''}{p}</span>)}
    </div>
  );
}

function Gates({ s }: { s: Session }) {
  const gates = arr<{ name: string; ok: boolean; detail: string }>(s.gates);
  if (!gates.length) return <p style={muted}>No checks were recorded.</p>;
  return (
    <div style={{ display: 'grid', gap: 8, marginBottom: 12 }}>
      {gates.map((g, i) => (
        <div key={i} style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {g.ok ? <CheckCircle2 size={15} color={GREEN} style={{ flexShrink: 0 }} /> : <XCircle size={15} color={RED} style={{ flexShrink: 0 }} />}
            <span style={{ fontWeight: 600, ...wrapText }}>{g.name}</span>
          </div>
          {g.detail && <div style={{ marginLeft: 23, minWidth: 0 }}><ClampedPre text={g.detail} /></div>}
        </div>
      ))}
    </div>
  );
}

/** Files changed (+/−) and the full diff on demand. */
function Change({ s }: { s: Session }) {
  const [show, setShow] = useState(false);
  const [diff, setDiff] = useState<string | null>(null);
  const [err, setErr] = useState('');
  const files = arr<{ path: string; added: number; removed: number }>(s.files_changed);
  const toggle = async () => {
    const next = !show;
    setShow(next);
    if (next && diff === null) {
      try { setDiff((await agentApi.diff(s.id)).diff); } catch (e) { setErr((e as Error).message); }
    }
  };
  return (
    <div style={{ minWidth: 0, maxWidth: '100%' }}>
      {files.length > 0 && (
        <div style={{ display: 'grid', gap: 3, marginBottom: 10 }}>
          <div style={{ ...muted, marginBottom: 2 }}>{files.length} file{files.length > 1 ? 's' : ''} changed</div>
          {files.map(f => (
            <div key={f.path} style={{ display: 'flex', gap: 10, ...mono }}>
              <span style={{ color: GREEN, minWidth: 44, textAlign: 'right' }}>+{f.added}</span>
              <span style={{ color: RED, minWidth: 44 }}>−{f.removed}</span>
              <span style={{ color: 'var(--text-primary)', ...wrapText }}>{f.path}</span>
            </div>
          ))}
        </div>
      )}
      <button style={ghost} onClick={toggle}>
        {show ? <ChevronDown size={14} /> : <ChevronRight size={14} />} {show ? 'Hide the change' : 'Show the change'}
      </button>
      {show && (
        <div style={{ marginTop: 10 }}>
          {err ? <span style={{ color: RED, fontSize: '0.85rem', ...wrapText }}>{err}</span>
            : diff === null ? <span style={muted}><Loader2 size={13} className="spin" /> Loading…</span>
            : <DiffView diff={diff} />}
        </div>
      )}
    </div>
  );
}

function Block({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <div className="card" style={card}>
      <div style={{ fontWeight: 600, display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10 }}>{icon}{title}</div>
      {children}
    </div>
  );
}

function PatchToggle({ name, patch }: { name: string; patch: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: 6, minWidth: 0 }}>
      <button style={{ ...ghost, padding: '5px 10px', fontSize: '0.8rem', maxWidth: '100%', ...wrapText }} onClick={() => setOpen(o => !o)}>
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />} {name}
      </button>
      {open && <div style={{ marginTop: 8 }}><DiffView diff={patch} maxHeight={400} /></div>}
    </div>
  );
}

function Summary({ summary, patches }: { summary: SessionSummary; patches: { name: string; patch: string }[] }) {
  const flows = arr<NonNullable<SessionSummary['flows']>[number]>(summary.flows);
  const fixes = arr<NonNullable<SessionSummary['fixes']>[number]>(summary.fixes);
  const scen = arr<NonNullable<SessionSummary['new_scenarios']>[number]>(summary.new_scenarios);
  const bugs = arr<NonNullable<SessionSummary['app_bugs']>[number]>(summary.app_bugs);
  const human = arr<string>(summary.needs_human).filter(Boolean);
  const infra = arr<string>(summary.infra).filter(Boolean);
  const findPatch = (file?: string) => {
    if (!file) return undefined;
    const base = file.split('/').pop();
    return patches.find(p => p.name === file || p.name === base);
  };
  const used = new Set(bugs.map(b => findPatch(b.patch_file)?.name).filter(Boolean));
  const loose = patches.filter(p => !used.has(p.name));

  return (
    <>
      {flows.length > 0 && (
        <Block icon={<ListChecks size={16} />} title="Flows">
          <div style={{ overflowX: 'auto', maxWidth: '100%' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.84rem' }}>
              <thead><tr>
                <th style={th}>Flow</th><th style={th}>Result</th><th style={{ ...th, whiteSpace: 'nowrap' }}>Category</th>
                <th style={th}>What happened</th><th style={{ ...th, whiteSpace: 'nowrap' }}>Action</th><th style={th}>Runs</th>
              </tr></thead>
              <tbody>
                {flows.map((f, i) => (
                  <tr key={i}>
                    <td style={{ ...td, ...mono }}>{f.flow_id}</td>
                    <td style={td}>{chip(f.final_status || '—', runColor(f.final_status))}</td>
                    <td style={{ ...td, whiteSpace: 'nowrap' }}>{f.category || '—'}</td>
                    <td style={{ ...td, color: 'var(--text-secondary)' }}>{f.root_cause || '—'}</td>
                    <td style={{ ...td, whiteSpace: 'nowrap' }}>{(f.action || '—').replace(/_/g, ' ')}</td>
                    <td style={td}><span style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {arr<string>(f.runs).filter(Boolean).map(r => <RunLink key={r} id={r} />)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Block>
      )}

      {fixes.length > 0 && (
        <Block icon={<Wrench size={16} />} title="Fixes">
          <div style={{ display: 'grid', gap: 14 }}>
            {fixes.map((f, i) => (
              <div key={i} style={{ borderTop: i ? '1px solid var(--border-color)' : 'none', paddingTop: i ? 12 : 0, minWidth: 0 }}>
                <div style={{ fontWeight: 600, ...wrapText }}>{f.title}</div>
                {f.why && <div style={{ ...muted, marginTop: 4, ...wrapText }}>{f.why}</div>}
                {arr<string>(f.files).length > 0 && (
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 6 }}>
                    {arr<string>(f.files).map(p => <code key={p} style={{ ...mono, color: 'var(--text-secondary)', ...wrapText }}>{p}</code>)}
                  </div>
                )}
                {arr<{ flow_id: string; run_id: string }>(f.verified_runs).length > 0 && (
                  <div style={{ ...muted, marginTop: 6, display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                    Verified by
                    {arr<{ flow_id: string; run_id: string }>(f.verified_runs).map((v, j) => (
                      <span key={j}><span style={mono}>{v.flow_id}</span> <RunLink id={v.run_id} /></span>
                    ))}
                  </div>
                )}
                {f.unit_tests && <div style={{ ...muted, marginTop: 4 }}>Unit tests: {f.unit_tests}</div>}
              </div>
            ))}
          </div>
        </Block>
      )}

      {scen.length > 0 && (
        <Block icon={<Sparkles size={16} />} title="New scenarios">
          <div style={{ display: 'grid', gap: 8 }}>
            {scen.map((n, i) => (
              <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap', minWidth: 0 }}>
                {chip(n.status === 'kept' ? 'Kept' : 'Dropped', n.status === 'kept' ? GREEN : GREY)}
                <code style={{ ...mono, ...wrapText }}>{n.flow_id}</code>
                <span style={wrapText}>{n.covers}</span>
                {n.run_id && <RunLink id={n.run_id} />}
                {n.reason && <span style={{ ...muted, ...wrapText }}>— {n.reason}</span>}
              </div>
            ))}
          </div>
        </Block>
      )}

      {(bugs.length > 0 || loose.length > 0) && (
        <Block icon={<Bug size={16} color={RED} />} title="App & server bugs">
          <div style={{ display: 'grid', gap: 14 }}>
            {bugs.map((b, i) => {
              const p = findPatch(b.patch_file);
              return (
                <div key={i} style={{ borderTop: i ? '1px solid var(--border-color)' : 'none', paddingTop: i ? 12 : 0, minWidth: 0 }}>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
                    {chip(b.app || 'app', b.app === 'server' ? AMBER : RED)}
                    {b.flow_id && <code style={{ ...mono, ...wrapText }}>{b.flow_id}</code>}
                    <span style={{ fontWeight: 600, ...wrapText }}>{b.summary}</span>
                  </div>
                  {b.evidence && <div style={{ ...muted, marginTop: 4, whiteSpace: 'pre-wrap', ...wrapText }}>{b.evidence}</div>}
                  {p ? <PatchToggle name={p.name} patch={p.patch} />
                    : b.patch_file ? <div style={{ ...muted, marginTop: 4 }}>Patch: <code style={mono}>{b.patch_file}</code></div> : null}
                </div>
              );
            })}
            {loose.length > 0 && (
              <div style={{ minWidth: 0 }}>
                {bugs.length > 0 && <div style={{ ...muted, marginBottom: 4 }}>Other proposed app patches</div>}
                {loose.map(p => <PatchToggle key={p.name} name={p.name} patch={p.patch} />)}
              </div>
            )}
          </div>
          <p style={{ ...muted, marginTop: 12 }}>App patches are proposals only — the app checkouts are never changed.</p>
        </Block>
      )}

      {human.length > 0 && (
        <Block icon={<UserRound size={16} color={AMBER} />} title="Needs a person">
          <ul style={{ margin: 0, paddingLeft: 20, display: 'grid', gap: 4, fontSize: '0.88rem', ...wrapText }}>
            {human.map((h, i) => <li key={i}>{h}</li>)}
          </ul>
        </Block>
      )}

      {infra.length > 0 && (
        <Block icon={<ServerCrash size={16} color={AMBER} />} title="Infra">
          <ul style={{ margin: 0, paddingLeft: 20, display: 'grid', gap: 4, fontSize: '0.88rem', color: 'var(--text-secondary)', ...wrapText }}>
            {infra.map((h, i) => <li key={i}>{h}</li>)}
          </ul>
        </Block>
      )}
    </>
  );
}

function Runs({ runs }: { runs: RunRow[] }) {
  if (!runs.length) return null;
  return (
    <Block icon={<ListChecks size={16} />} title={`Runs (${runs.length})`}>
      <div className="scroll-box" style={{ maxWidth: '100%' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.84rem' }}>
          <thead><tr>
            <th style={th}>Flow</th><th style={th}>Attempt</th><th style={th}>Status</th><th style={th}>Time</th>
            <th style={th}>Category</th><th style={th}>Failed at</th><th style={th}>Run</th>
          </tr></thead>
          <tbody>
            {runs.map((r, i) => (
              <tr key={`${r.run_id}-${i}`}>
                <td style={{ ...td, ...mono }}>{r.flow_id}</td>
                <td style={td}>{r.label || '—'}
                  {r.resumed_from && <div style={{ ...muted, fontSize: '0.75rem' }}>resumed from <RunLink id={r.resumed_from} /></div>}</td>
                <td style={td}>{chip(r.status || '—', runColor(r.status))}</td>
                <td style={{ ...td, whiteSpace: 'nowrap' }}>{r.seconds != null ? fmtDur(r.seconds * 1000) : '—'}</td>
                <td style={td}>{r.category || '—'}</td>
                <td style={{ ...td, color: 'var(--text-secondary)', maxWidth: 360 }}>
                  {r.failed_segment || r.failed_step
                    ? <>{r.failed_segment && <span style={{ fontWeight: 600 }}>{r.failed_segment}</span>}
                        {r.failed_step && <div style={{ ...mono, wordBreak: 'break-word' }}>{r.failed_step}</div>}</>
                    : '—'}
                </td>
                <td style={td}><RunLink id={r.run_id} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Block>
  );
}
