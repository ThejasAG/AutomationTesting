import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  AlertTriangle, Bot, CheckCircle2, History, Loader2, RefreshCw, ShieldCheck, Square, XCircle,
} from 'lucide-react';
import { agentApi, isActiveStatus } from '../agentApi';
import type { AgentStatus, Coverage, FeatureMap, PreflightCheck, Session, StartSession } from '../agentApi';
import {
  AMBER, BLUE, GREEN, GREY, KIND, RED, TRIGGER, chip, fmtDur, ghost, h3, mono, ms, muted, section, statusChip,
  td, th, when, wrapText,
} from './agent/ui';
import type { RestartState } from './agent/ui';
import { Banner } from './agent/Bits';
import EventFeed from './agent/EventFeed';
import SessionDetail from './agent/SessionDetail';
import StartPanel from './agent/StartPanel';
import SettingsCard from './agent/SettingsCard';
import { CoverageSection, FeatureMapSection } from './agent/FeatureMap';

/** AI Agent — Claude Code sessions.
 *
 *  Start a session (run & heal, fix a run, analyze the apps, create scenarios),
 *  follow it live, and review what it changed: every change sits on its own branch
 *  until a person approves and merges it. */

const sleep = (t: number) => new Promise(r => setTimeout(r, t));

export default function AgentPage() {
  const [params, setParams] = useSearchParams();
  const selId = params.get('session') || '';
  const [st, setSt] = useState<AgentStatus | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [fmap, setFmap] = useState<FeatureMap | null>(null);
  const [fmapLoaded, setFmapLoaded] = useState(false);
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [checks, setChecks] = useState<PreflightCheck[] | null>(null);
  const [busy, setBusy] = useState('');
  const [covBusy, setCovBusy] = useState(false);
  const [err, setErr] = useState('');          // from an action the user took
  const [loadErr, setLoadErr] = useState('');  // from background loading
  const [note, setNote] = useState('');
  const [restart, setRestart] = useState<RestartState>({ phase: 'idle' });
  const restarting = useRef(false);

  const load = useCallback(async () => {
    if (restarting.current) return;   // the backend is down on purpose
    try {
      const [s, l] = await Promise.all([agentApi.status(), agentApi.sessions(30)]);
      setSt(s);
      setSessions(l.sessions);
      setLoadErr('');
    } catch (e) {
      setLoadErr((e as Error).message);
    }
  }, []);

  const loadFmap = useCallback(async () => {
    try {
      setFmap((await agentApi.featureMap()).feature_map);
    } catch (e) {
      setLoadErr((e as Error).message);
    } finally {
      setFmapLoaded(true);
    }
  }, []);

  const loadCoverage = useCallback(async () => {
    setCovBusy(true);
    try {
      setCoverage(await agentApi.inventory());
    } catch (e) {
      setLoadErr(`Coverage: ${(e as Error).message}`);
      setCoverage(c => c ?? {});
    } finally {
      setCovBusy(false);
    }
  }, []);

  useEffect(() => { load(); loadFmap(); loadCoverage(); }, [load, loadFmap, loadCoverage]);

  // Poll the status: every 5 s while a session works, every 30 s otherwise.
  const activeId = st?.active?.id ?? '';
  useEffect(() => {
    const t = setInterval(load, activeId ? 5000 : 30000);
    return () => clearInterval(t);
  }, [activeId, load]);

  // A session just ended: it may have written a new feature map.
  const prevActive = useRef('');
  useEffect(() => {
    if (prevActive.current && prevActive.current !== activeId) loadFmap();
    prevActive.current = activeId;
  }, [activeId, loadFmap]);

  const select = useCallback((id: string) => {
    setParams(id ? { session: id } : {});
  }, [setParams]);

  const act = async (label: string, fn: () => Promise<unknown>, done?: string) => {
    setBusy(label);
    setErr('');
    setNote('');
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

  const start = (label: string, body: StartSession) => act(label, async () => {
    const r = await agentApi.start(body);
    select(r.id);
  }, `${KIND[body.kind] ?? body.kind} started — follow it below.`);

  // Approved changes load when the backend restarts. It is down for a few seconds:
  // wait until it has gone away and answers again.
  const restartBackend = async () => {
    setRestart({ phase: 'restarting' });
    try {
      await agentApi.restartBackend();
    } catch (e) {
      setRestart({ phase: 'idle', error: (e as Error).message });
      return;
    }
    restarting.current = true;
    const t0 = Date.now();
    let wentDown = false;
    let back = false;
    while (Date.now() - t0 < 120_000) {
      await sleep(1500);
      try {
        await agentApi.status();
        if (wentDown || Date.now() - t0 > 20_000) { back = true; break; }
      } catch {
        wentDown = true;
      }
    }
    restarting.current = false;
    if (back) {
      setRestart({ phase: 'done' });
      load();
    } else {
      setRestart({ phase: 'idle', error: 'The backend did not come back after 2 minutes — check ./status.sh on the test Mac.' });
    }
  };

  const selRow = sessions.find(s => s.id === selId);
  const live = selRow && isActiveStatus(selRow.status) ? selRow : st?.active ?? null;
  const waiting = sessions.filter(s => s.status === 'review');
  const claude = st?.claude;
  const settings = st?.settings;

  return (
    <div className="animate-fade-in" style={{ minWidth: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>
      <div className="page-header" style={{ display: 'flex', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <Bot size={34} color="var(--accent-primary)" /> AI Agent
          </h1>
          <p className="page-subtitle">
            Claude Code analyzes the Vyapy apps, writes new scenarios and fixes the automation when a flow fails.
            Every change waits for your approval.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
          <button style={ghost} onClick={() => { load(); loadFmap(); }}><RefreshCw size={14} /> Refresh</button>
        </div>
      </div>

      {/* ── status ── */}
      <div className="card" style={{ padding: 20, transform: 'none' }}>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          {!claude ? chip('Checking Claude Code…', GREY)
            : !claude.installed ? chip('Claude Code is not installed on the test Mac', RED)
            : !claude.logged_in
              ? chip(<>Claude Code not logged in — run <code>claude</code> in a terminal on the test Mac</>, RED, true)
              : chip(['Claude Code logged in', claude.plan, claude.version && `v${claude.version}`].filter(Boolean).join(' · '), GREEN)}
          {settings && st && (
            settings.nightly_enabled && st.schedule?.installed
              ? chip(`Nightly at ${st.schedule.time || settings.nightly_time}`, GREEN)
              : settings.nightly_enabled
                ? chip(`Nightly on, but not scheduled${st.schedule?.error ? ` — ${st.schedule.error}` : ''}`, AMBER, true)
                : chip('Nightly off', GREY)
          )}
          {live && chip('Working', BLUE)}
          <button style={{ ...ghost, marginLeft: 'auto' }} disabled={busy === 'pre'}
                  onClick={() => act('pre', async () => setChecks((await agentApi.preflight(settings?.env || 'staging')).checks))}>
            {busy === 'pre' ? <Loader2 size={14} className="spin" /> : <ShieldCheck size={14} />} Preflight check
          </button>
        </div>
        {checks && (
          <div style={{ display: 'grid', gap: 4, marginTop: 14, fontSize: '0.85rem' }}>
            {checks.map(c => (
              <div key={c.name} style={{ display: 'flex', gap: 8, alignItems: 'baseline', minWidth: 0 }}>
                {c.ok ? <CheckCircle2 size={14} color={GREEN} style={{ alignSelf: 'center' }} />
                  : <XCircle size={14} color={RED} style={{ alignSelf: 'center' }} />}
                <span style={{ fontWeight: 600, flexShrink: 0 }}>{c.name}</span>
                <span style={{ color: 'var(--text-secondary)', ...wrapText }}>{c.detail}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={{ marginTop: 16 }}>
        {err && <Banner color={RED} icon={<XCircle size={16} />}>{err}</Banner>}
        {loadErr && !err && <Banner color={RED} icon={<AlertTriangle size={16} />}>{loadErr}</Banner>}
        {note && !err && <Banner color={GREEN} icon={<CheckCircle2 size={16} />}>{note}</Banner>}
        {waiting.length > 0 && !(waiting.length === 1 && waiting[0].id === selId) && (
          <Banner color={AMBER} icon={<AlertTriangle size={16} />}>
            {waiting.length === 1 ? 'A session is' : `${waiting.length} sessions are`} waiting for your approval.{' '}
            <button onClick={() => select(waiting[0].id)}
                    style={{ background: 'none', border: 'none', padding: 0, color: 'inherit', cursor: 'pointer',
                             font: 'inherit', textDecoration: 'underline', fontWeight: 600 }}>
              Review {waiting.length > 1 ? 'the latest' : 'it'}
            </button>
          </Banner>
        )}
      </div>

      <StartPanel disabled={!!st?.active} busy={busy} defaultMaxNew={settings?.max_new_scenarios ?? 3} onStart={start} />

      {/* ── live ── */}
      {live && (
        <div style={section}>
          <h3 style={h3}><Loader2 size={18} className="spin" color={BLUE} /> Working now</h3>
          <ActivePanel s={live} busy={busy} selected={live.id === selId} onOpen={() => select(live.id)}
                       onStop={() => act('stop', () => agentApi.stop(live.id),
                         'Stopping — Claude Code finishes its current step first.')} />
        </div>
      )}

      {/* ── sessions ── */}
      <div style={section}>
        <h3 style={h3}><History size={18} /> Sessions</h3>
        <div className="card" style={{ padding: 0, transform: 'none', overflowX: 'auto', maxWidth: '100%' }}>
          {sessions.length === 0 ? (
            <p style={{ ...muted, padding: 20 }}>{st ? 'No sessions yet — start one above.' : 'Loading…'}</p>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
              <thead><tr>
                <th style={th}>Started</th><th style={th}>Kind</th><th style={th}>Trigger</th>
                <th style={th}>Status</th><th style={th}>Duration</th><th style={th}>Branch</th>
              </tr></thead>
              <tbody>
                {sessions.map(s => {
                  const on = s.id === selId;
                  const began = ms(s.started_at || s.created_at);
                  const end = s.finished_at ? ms(s.finished_at) : isActiveStatus(s.status) ? Date.now() : NaN;
                  return (
                    <tr key={s.id} onClick={() => select(on ? '' : s.id)}
                        style={{ cursor: 'pointer', background: on ? 'rgba(129,140,248,0.10)' : 'transparent' }}>
                      <td style={{ ...td, whiteSpace: 'nowrap', borderLeft: `3px solid ${on ? 'var(--accent-primary)' : 'transparent'}` }}>
                        {when(s.started_at || s.created_at)}
                      </td>
                      <td style={{ ...td, fontWeight: 600 }}>{KIND[s.kind] ?? s.kind}</td>
                      <td style={td}>{TRIGGER[s.trigger] ?? s.trigger}</td>
                      <td style={td}>{statusChip(s.status)}</td>
                      <td style={{ ...td, whiteSpace: 'nowrap' }}>{fmtDur(end - began)}</td>
                      <td style={{ ...td, ...mono, color: 'var(--text-secondary)', maxWidth: 280 }}>{s.branch || '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {selId && <SessionDetail key={selId} id={selId} onChanged={load} restart={restart} onRestart={restartBackend} />}

      <FeatureMapSection fmap={fmap} loaded={fmapLoaded} canStart={!st?.active} starting={busy === 'fmap'}
                         onOpenSession={select}
                         onCreate={features => start('fmap', { kind: 'generate', features })} />

      <CoverageSection coverage={coverage} onRefresh={loadCoverage} busy={covBusy} />

      {st && <SettingsCard s={st.settings} schedule={st.schedule} secrets={st.secrets} onSaved={load} />}
    </div>
  );
}

/** The session that is working right now: what, how long, and its live steps. */
function ActivePanel({ s, busy, selected, onOpen, onStop }: {
  s: Session; busy: string; selected: boolean; onOpen: () => void; onStop: () => void;
}) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick(n => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const began = ms(s.started_at || s.created_at);
  return (
    <div className="card" style={{ padding: 20, transform: 'none', borderColor: `${BLUE}55`, minWidth: 0, maxWidth: '100%' }}>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
        <span style={{ fontWeight: 600 }}>{KIND[s.kind] ?? s.kind}</span>
        {statusChip(s.status)}
        <span style={muted}>{TRIGGER[s.trigger] ?? s.trigger}</span>
        {s.phase && <span style={{ ...muted, ...wrapText }}>· {s.phase}</span>}
        <span style={{ ...muted, fontVariantNumeric: 'tabular-nums' }}>· {fmtDur(Date.now() - began)}</span>
        {s.stop_requested && chip('Stopping…', AMBER)}
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
          {!selected && <button style={ghost} onClick={onOpen}>Open</button>}
          <button className="btn" style={{ background: 'var(--danger)', boxShadow: 'none', padding: '8px 14px' }}
                  disabled={!!busy || s.stop_requested} onClick={onStop}>
            {busy === 'stop' ? <Loader2 size={14} className="spin" /> : <Square size={14} />} Stop
          </button>
        </span>
      </div>
      <EventFeed key={s.id} sessionId={s.id} live />
    </div>
  );
}
