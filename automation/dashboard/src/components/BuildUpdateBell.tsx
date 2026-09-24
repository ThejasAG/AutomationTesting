import { useCallback, useEffect, useRef, useState } from 'react';
import { Bell, Loader2, CheckCircle2, AlertTriangle, Smartphone, Download } from 'lucide-react';
import {
  getBuildUpdates, deployLatestBuild, getBuildDeployStatus, getScenarioDevices,
} from '../api';
import type { BuildUpdateProject, BuildDeployStatus, SimDevice } from '../api';
import ModalPortal from './ModalPortal';

/** Bell in the sidebar: shows how many projects have commits waiting on the remote,
 *  and deploys the latest build to EVERY simulator in one click.
 *
 *  You choose WHICH simulators to install onto. It used to install on every
 *  simulator found, which is almost never wanted — a build meant for one device
 *  would overwrite the app on the others mid-run. */
/** '3m 20s' / '45s'. Anything under a minute stays in seconds — "0m 45s" reads worse. */
function fmtDuration(secs: number): string {
  const s = Math.max(0, Math.round(secs));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

function phaseLabel(phase?: string): string {
  return phase === 'pull' ? 'Pulling'
    : phase === 'build' ? 'Building'
      : phase === 'install' ? 'Installing'
        : phase === 'done' ? 'Finishing'
          : 'Working';
}

/** The ETA, or an honest admission that there isn't one yet.
 *
 *  eta_s is null until a work unit completes — during the very first build there is
 *  nothing to extrapolate from, and printing a made-up "about 5 minutes" would be worse
 *  than saying so. The phase timer next to it still shows the deploy is alive. */
function etaText(p?: { eta_s: number | null; phase_elapsed_s: number }): string {
  if (!p) return '';
  if (p.eta_s === null) return `estimating… (${fmtDuration(p.phase_elapsed_s)} in this step)`;
  return p.eta_s <= 0 ? 'almost done' : `~${fmtDuration(p.eta_s)} left`;
}

export default function BuildUpdateBell() {
  const [projects, setProjects] = useState<BuildUpdateProject[]>([]);
  const [count, setCount] = useState(0);
  const [open, setOpen] = useState(false);
  const [checking, setChecking] = useState(false);
  const [deploy, setDeploy] = useState<BuildDeployStatus | null>(null);
  const [sims, setSims] = useState<SimDevice[]>([]);
  const [chosen, setChosen] = useState<string[]>(
    () => JSON.parse(localStorage.getItem('deploy_devices') || '[]'));
  // Which APPS to build+install. Sent explicitly, so "install this app on this device"
  // works even when there are no new commits — the local checkout is often already
  // ahead of what is installed on the simulator.
  const [apps, setApps] = useState<string[]>(
    () => JSON.parse(localStorage.getItem('deploy_apps') || '[]'));
  const [error, setError] = useState('');
  const poll = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    setChecking(true);
    try {
      const r = await getBuildUpdates();
      setProjects(r.projects);
      setCount(r.count);
      // Drop remembered apps the server no longer knows. The selection is restored
      // from localStorage, so a deleted/un-cloned project stayed selected forever and
      // every deploy 404'd on the whole batch ("Unknown or un-cloned project(s)") --
      // one dead id blocked the valid ones, with no way to clear it from the UI.
      const live = new Set(r.projects.map((p) => p.project_id));
      setApps((prev) => {
        const kept = prev.filter((id) => live.has(id));
        if (kept.length !== prev.length) {
          localStorage.setItem('deploy_apps', JSON.stringify(kept));
        }
        return kept.length === prev.length ? prev : kept;
      });
      setError('');
    } catch {
      /* offline / not logged in — leave the bell quiet rather than shouting */
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    getScenarioDevices().then(r => setSims(r.simulators)).catch(() => {});
  }, []);

  // A fetch per project runs server-side on every check, so keep this slow.
  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 60_000);
    return () => window.clearInterval(t);
  }, [refresh]);

  // While a deploy runs, poll its progress.
  const startPolling = useCallback(() => {
    if (poll.current) window.clearInterval(poll.current);
    poll.current = window.setInterval(async () => {
      try {
        const s = await getBuildDeployStatus();
        setDeploy(s);
        if (s.status !== 'running') {
          if (poll.current) window.clearInterval(poll.current);
          poll.current = null;
          refresh();
        }
      } catch { /* transient */ }
    }, 2000);
  }, [refresh]);

  useEffect(() => () => { if (poll.current) window.clearInterval(poll.current); }, []);

  /** Read the server's deploy state and, if one is still running, START WATCHING IT.
   *
   *  This used to only setDeploy() once on mount and never poll. A deploy started in
   *  another tab — or simply still running after a page reload — therefore rendered a
   *  frozen snapshot: the button said "Deploying…" forever and the log never advanced,
   *  so there was no way to tell whether it was installing or had silently died. The
   *  polling is what makes the progress live, so it has to be resumed here too, not
   *  only in onDeploy(). */
  const syncDeploy = useCallback(async () => {
    try {
      const s = await getBuildDeployStatus();
      if (s.status !== 'idle') setDeploy(s);
      if (s.status === 'running' && !poll.current) startPolling();
    } catch { /* offline — leave the panel as it is */ }
  }, [startPolling]);

  useEffect(() => { syncDeploy(); }, [syncDeploy]);

  const onDeploy = async () => {
    setError('');
    try {
      if (!apps.length) { setError('Pick at least one app to build.'); return; }
      if (!chosen.length) { setError('Pick at least one simulator to install onto.'); return; }
      await deployLatestBuild(apps, chosen);
      setDeploy({ status: 'running', steps: [], results: [] });
      startPolling();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start the deploy');
    }
  };

  const running = deploy?.status === 'running';
  const withUpdates = projects.filter((p) => p.has_updates);
  // The newest log line IS the answer to "is it installing?" — surface it instead of
  // making people scroll a 200px-tall <pre> at the bottom of a modal to find out.
  const lastStep = deploy?.steps?.length ? deploy.steps[deploy.steps.length - 1].message : '';
  const prog = deploy?.progress;
  const devicesTouched = new Set(
    (deploy?.results ?? []).filter((r) => r.device_id).map((r) => r.device_id),
  ).size;

  return (
    <>
      <button
        onClick={() => { setOpen(true); refresh(); syncDeploy(); }}
        className="nav-link"
        title="Latest build — pull and install on every device"
        style={{
          background: count ? 'rgba(251,191,36,0.14)' : 'transparent',
          color: count ? '#fbbf24' : undefined,
          border: count ? '1px solid rgba(251,191,36,0.35)' : '1px solid transparent',
          cursor: 'pointer', width: '100%', textAlign: 'left', font: 'inherit',
          position: 'relative',
        }}
      >
        {running ? <Loader2 size={20} className="spin" /> : <Bell size={20} />}
        Latest build
        {count > 0 && (
          <span style={{
            marginLeft: 'auto', background: '#fbbf24', color: '#1f2937',
            borderRadius: 999, padding: '0 7px', fontSize: 12, fontWeight: 700,
          }}>{count}</span>
        )}
      </button>

      {open && (
        <ModalPortal>
          <div
            onClick={() => setOpen(false)}
            style={{
              position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
            }}
          >
            <div
              onClick={(e) => e.stopPropagation()}
              style={{
                background: 'var(--card-bg,#111827)', color: 'var(--text,#e5e7eb)',
                borderRadius: 12, padding: 24, width: 'min(640px, 92vw)',
                maxHeight: '84vh', overflowY: 'auto',
                border: '1px solid rgba(255,255,255,0.08)',
              }}
            >
              <h2 style={{ margin: '0 0 4px', display: 'flex', alignItems: 'center', gap: 8 }}>
                <Download size={20} /> Latest build
              </h2>
              <p style={{ margin: '0 0 18px', opacity: 0.7, fontSize: 14 }}>
                Pulls the newest commits, builds each chosen app once, and installs it
                on the simulators you pick. Works with no new commits too — the local
                checkout is often already ahead of what is on the device.
              </p>

              {error && (
                <div style={{
                  background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)',
                  borderRadius: 8, padding: 10, marginBottom: 14, fontSize: 13,
                }}>{error}</div>
              )}

              {/* Pinned progress. The Deploy button and the detailed log live BELOW the
                  app + simulator lists, which on a full list are off the bottom of the
                  modal — so clicking Deploy appeared to do nothing at all. This stays put
                  at the top and says what is happening right now. */}
              {deploy && deploy.status !== 'idle' && (
                <div style={{
                  position: 'sticky', top: 0, zIndex: 1,
                  background: running ? 'rgba(124,58,237,0.16)' : 'rgba(255,255,255,0.06)',
                  border: `1px solid ${running ? 'rgba(124,58,237,0.45)' : 'rgba(255,255,255,0.12)'}`,
                  borderRadius: 8, padding: '9px 11px', marginBottom: 14,
                  display: 'flex', alignItems: 'center', gap: 9,
                }}>
                  {running
                    ? <Loader2 size={15} className="spin" style={{ flexShrink: 0 }} />
                    : deploy.status === 'completed'
                      ? <CheckCircle2 size={15} style={{ color: '#34d399', flexShrink: 0 }} />
                      : <AlertTriangle size={15} style={{ color: '#fbbf24', flexShrink: 0 }} />}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, display: 'flex', gap: 8 }}>
                      <span style={{ flex: 1, minWidth: 0 }}>
                        {running
                          ? `${phaseLabel(prog?.phase)}${prog?.detail ? ` — ${prog.detail}` : ''}`
                          : deploy.status.replace(/_/g, ' ')}
                        {devicesTouched > 0 && (
                          <span style={{ opacity: 0.6, fontWeight: 400 }}> · {devicesTouched} device(s)</span>
                        )}
                      </span>
                      {!!prog?.total && (
                        <span style={{ opacity: 0.85, fontVariantNumeric: 'tabular-nums' }}>
                          {prog.percent}%
                        </span>
                      )}
                    </div>

                    {!!prog?.total && (
                      <div style={{
                        height: 5, borderRadius: 3, marginTop: 6,
                        background: 'rgba(255,255,255,0.12)', overflow: 'hidden',
                      }}>
                        <div style={{
                          width: `${prog.percent}%`, height: '100%',
                          background: running ? '#7c3aed'
                            : deploy.status === 'completed' ? '#34d399' : '#fbbf24',
                          transition: 'width 400ms ease',
                        }} />
                      </div>
                    )}

                    <div style={{
                      fontSize: 11, opacity: 0.7, marginTop: 4, display: 'flex', gap: 10,
                      fontVariantNumeric: 'tabular-nums',
                    }}>
                      {running && <span>{etaText(prog)}</span>}
                      {!!prog && <span>elapsed {fmtDuration(prog.elapsed_s)}</span>}
                    </div>

                    {!!lastStep && (
                      <div style={{
                        fontSize: 11, opacity: 0.55, marginTop: 3,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}>{lastStep}</div>
                    )}
                  </div>
                </div>
              )}

              <div style={{ marginBottom: 18 }}>
                {checking && !projects.length && <div style={{ opacity: 0.6 }}>Checking remotes…</div>}
                {!checking && !withUpdates.length && projects.length > 0 && (
                  <div style={{ opacity: 0.7, fontSize: 13, marginBottom: 10 }}>
                    No new commits — but you can still rebuild and reinstall any app below.
                  </div>
                )}
                <div style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: '0.06em', opacity: 0.6, margin: '4px 0 7px' }}>
                  Build &amp; install
                </div>
                {projects.map((p) => (
                  <label key={p.project_id} style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '7px 0',
                    borderBottom: '1px solid rgba(255,255,255,0.06)', cursor: 'pointer',
                  }}>
                    <input
                      type="checkbox"
                      checked={apps.includes(p.project_id)}
                      onChange={(e) => {
                        const next = e.target.checked
                          ? [...apps, p.project_id]
                          : apps.filter((x) => x !== p.project_id);
                        setApps(next);
                        localStorage.setItem('deploy_apps', JSON.stringify(next));
                      }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontWeight: 600 }}>{p.name}</div>
                      <div style={{ opacity: 0.6, fontSize: 12 }}>
                        {p.branch} · at {p.current_commit || '?'}
                        {p.has_updates ? ' · new commits waiting' : ' · up to date'}
                      </div>
                      {!!p.installed?.length && (
                        <div style={{ opacity: 0.55, fontSize: 11, marginTop: 2 }}>
                          on device: {p.installed.map(i =>
                            `${i.device} v${i.version} (${i.build})`).join(' · ')}
                        </div>
                      )}
                    </div>
                    {p.has_updates && <AlertTriangle size={15} style={{ color: '#fbbf24', flexShrink: 0 }} />}
                  </label>
                ))}
              </div>

              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: '0.06em', opacity: 0.6, marginBottom: 7 }}>
                  Install on
                </div>
                {sims.length === 0 && <div style={{ opacity: 0.6, fontSize: 13 }}>No simulators found.</div>}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                  {sims.map((s) => (
                    <label key={s.udid} style={{ display: 'flex', alignItems: 'center', gap: 9, fontSize: 13, cursor: 'pointer' }}>
                      <input
                        type="checkbox"
                        checked={chosen.includes(s.udid)}
                        onChange={(e) => {
                          const next = e.target.checked
                            ? [...chosen, s.udid]
                            : chosen.filter((x) => x !== s.udid);
                          setChosen(next);
                          localStorage.setItem('deploy_devices', JSON.stringify(next));
                        }}
                      />
                      <span>{s.name}</span>
                      <span style={{ opacity: 0.5, fontSize: 11 }}>
                        {s.state === 'Booted' ? '● booted' : 'shut down (will be booted)'}
                      </span>
                    </label>
                  ))}
                </div>
                {!chosen.length && sims.length > 0 && (
                  <div style={{ marginTop: 6, fontSize: 12, color: '#fbbf24' }}>
                    Nothing selected — pick the simulators you want this build on.
                  </div>
                )}
              </div>

              <button
                onClick={onDeploy}
                disabled={running || !apps.length || !chosen.length}
                style={{
                  width: '100%', padding: '11px 14px', borderRadius: 8, fontWeight: 600,
                  border: 'none',
                  // Must match `disabled` exactly. It used to test !withUpdates.length, so with
                  // no new commits the cursor said not-allowed on a button that was enabled and
                  // worked — this panel deliberately supports rebuilding an up-to-date checkout.
                  cursor: running || !apps.length || !chosen.length ? 'not-allowed' : 'pointer',
                  background: running || !apps.length || !chosen.length ? 'rgba(255,255,255,0.10)' : '#7c3aed',
                  color: '#fff', display: 'flex', alignItems: 'center',
                  justifyContent: 'center', gap: 8,
                }}
              >
                {running
                  ? <><Loader2 size={16} className="spin" /> Deploying…</>
                  : <><Smartphone size={16} /> Pull &amp; install on {chosen.length || 'no'} device{chosen.length === 1 ? '' : 's'}</>}
              </button>

              {deploy && (
                <div style={{ marginTop: 18 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                    {deploy.status === 'completed' && <CheckCircle2 size={16} style={{ color: '#34d399' }} />}
                    {deploy.status === 'completed_with_errors' && <AlertTriangle size={16} style={{ color: '#fbbf24' }} />}
                    {deploy.status === 'failed' && <AlertTriangle size={16} style={{ color: '#ef4444' }} />}
                    <strong style={{ fontSize: 14 }}>{deploy.status.replace(/_/g, ' ')}</strong>
                    {devicesTouched > 0 && (
                      <span style={{ opacity: 0.6, fontSize: 12 }}>· {devicesTouched} device(s)</span>
                    )}
                  </div>

                  {!!deploy.results.length && (
                    <div style={{ marginBottom: 10 }}>
                      {deploy.results.map((r, i) => (
                        <div key={i} style={{
                          display: 'flex', gap: 8, fontSize: 12, padding: '3px 0',
                          color: r.ok ? 'inherit' : '#fca5a5',
                        }}>
                          <span style={{ width: 14 }}>{r.ok ? '✓' : '✕'}</span>
                          <span style={{ flex: 1 }}>
                            {r.project}{r.device ? ` → ${r.device}` : ''} ({r.stage})
                            {r.version && (
                              <> · <strong>v{r.version}</strong>
                                {r.build ? ` (${r.build})` : ''}
                                {r.commit ? ` @ ${r.commit}` : ''}
                                {r.previous_version && r.previous_version !== r.version
                                  ? ` — was v${r.previous_version}` : ''}
                              </>
                            )}
                            {r.ok && r.version_matches === false && (
                              <strong style={{ color: '#fbbf24' }}> · VERSION MISMATCH — the device
                                is not running the build we just made</strong>
                            )}
                            {!r.ok && r.detail ? ` — ${r.detail}` : ''}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  <pre style={{
                    background: 'rgba(0,0,0,0.35)', borderRadius: 8, padding: 10,
                    maxHeight: 200, overflowY: 'auto', fontSize: 11, margin: 0,
                    whiteSpace: 'pre-wrap',
                  }}>
                    {deploy.steps.map((s) => s.message).join('\n') || 'starting…'}
                  </pre>
                </div>
              )}
            </div>
          </div>
        </ModalPortal>
      )}
    </>
  );
}
