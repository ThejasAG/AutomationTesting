import { useState } from 'react';
import { ChevronDown, ChevronRight, Loader2, Map as MapIcon, RefreshCw, ShieldCheck, Sparkles } from 'lucide-react';
import type { Coverage, Feature, FeatureMap, FeatureTotals } from '../../agentApi';
import { AMBER, GREEN, GREY, RED, chip, ghost, h3, mono, muted, section, td, th, when, wrapText } from './ui';

const COV: Record<string, string> = { covered: GREEN, partial: AMBER, missing: RED };
const RISK: Record<string, string> = { high: RED, medium: AMBER, low: GREY };
const ORDER: Record<string, number> = { missing: 0, partial: 1, covered: 2 };
const RISK_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };
const APP_LABEL: Record<string, string> = { consumer: 'Consumer app', business: 'Business app' };

const totalsOf = (list: Feature[]): FeatureTotals => ({
  covered: list.filter(f => f.status === 'covered').length,
  partial: list.filter(f => f.status === 'partial').length,
  missing: list.filter(f => f.status === 'missing').length,
});

/** Claude's latest feature map: every feature of both apps rated covered / partial /
 *  missing. Gaps can be ticked and handed to a "Create scenarios" session. */
export function FeatureMapSection({ fmap, loaded, canStart, starting, onCreate, onOpenSession }: {
  fmap: FeatureMap | null; loaded: boolean; canStart: boolean; starting: boolean;
  onCreate: (features: string[]) => void; onOpenSession: (id: string) => void;
}) {
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [showCovered, setShowCovered] = useState(true);
  const n = Object.keys(picked).length;
  const apps = fmap ? Object.entries(fmap.apps || {}).filter(([, l]) => Array.isArray(l)) as [string, Feature[]][] : [];

  const toggle = (key: string, text: string) => setPicked(p => {
    const next = { ...p };
    if (next[key]) delete next[key]; else next[key] = text;
    return next;
  });

  return (
    <div style={section}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
        <h3 style={{ ...h3, marginBottom: 0 }}><MapIcon size={18} /> Feature map</h3>
        {fmap && (
          <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem', ...wrapText }}>
            {fmap.mode ? `${fmap.mode} analysis` : 'analysis'} · {fmap.finished_at ? when(fmap.finished_at) : fmap.generated}
            {fmap.session_id && (
              <> · <button onClick={() => onOpenSession(fmap.session_id!)}
                          style={{ background: 'none', border: 'none', padding: 0, color: 'var(--accent-primary)',
                                   cursor: 'pointer', font: 'inherit' }}>open the session</button></>
            )}
          </span>
        )}
        {fmap && (
          <label style={{ marginLeft: 'auto', display: 'inline-flex', gap: 6, alignItems: 'center', ...muted }}>
            <input type="checkbox" checked={showCovered} onChange={e => setShowCovered(e.target.checked)} /> Show covered features
          </label>
        )}
      </div>

      {!loaded ? (
        <div className="card" style={{ padding: 20 }}><Loader2 size={16} className="spin" /> Loading…</div>
      ) : !fmap ? (
        <div className="card" style={{ padding: 20, transform: 'none', ...muted }}>
          No analysis yet — run <b>Analyze apps</b>.
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 12 }}>
            {apps.map(([app, list]) => {
              const t = fmap.totals?.[app] ?? totalsOf(list);
              return (
                <div key={app} style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                  <span style={{ fontWeight: 600, fontSize: '0.88rem' }}>{APP_LABEL[app] ?? app}</span>
                  {chip(`${t.covered} covered`, GREEN)}{chip(`${t.partial} partial`, AMBER)}{chip(`${t.missing} missing`, RED)}
                </div>
              );
            })}
          </div>

          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 12, flexWrap: 'wrap' }}>
            <button className="btn" disabled={!n || !canStart || starting} onClick={() => onCreate(Object.values(picked))}
                    title={canStart ? '' : 'A session is already running'}>
              {starting ? <Loader2 size={16} className="spin" /> : <Sparkles size={16} />}
              Create scenarios for selected{n ? ` (${n})` : ''}
            </button>
            {n > 0 && <button style={ghost} onClick={() => setPicked({})}>Clear</button>}
            <span style={muted}>Tick partial or missing features to write scenarios for them.</span>
          </div>

          <div style={{ display: 'grid', gap: 16 }}>
            {apps.map(([app, list]) => {
              const rows = list
                .map((f, i) => ({ f, key: `${app}|${i}` }))
                .filter(({ f }) => showCovered || f.status !== 'covered')
                .sort((a, b) => ((ORDER[a.f.status] ?? 3) - (ORDER[b.f.status] ?? 3))
                  || ((RISK_ORDER[a.f.risk] ?? 3) - (RISK_ORDER[b.f.risk] ?? 3)));
              return (
                <div key={app} className="card" style={{ padding: 0, transform: 'none', overflowX: 'auto', maxWidth: '100%', minWidth: 0 }}>
                  <div style={{ padding: '14px 16px 4px', fontWeight: 600 }}>{APP_LABEL[app] ?? app}</div>
                  {rows.length === 0 ? (
                    <p style={{ ...muted, padding: '6px 16px 14px' }}>Every feature is covered.</p>
                  ) : (
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.84rem' }}>
                      <thead><tr>
                        <th style={{ ...th, width: 28 }} /><th style={th}>Feature</th><th style={th}>Role</th>
                        <th style={th}>Status</th><th style={th}>Risk</th><th style={th}>Covered by</th><th style={th}>Gap</th>
                      </tr></thead>
                      <tbody>
                        {rows.map(({ f, key }) => {
                          const gap = f.status !== 'covered';
                          return (
                            <tr key={key} style={{ background: picked[key] ? 'rgba(129,140,248,0.08)' : 'transparent' }}>
                              <td style={td}>
                                {gap && <input type="checkbox" checked={!!picked[key]}
                                               onChange={() => toggle(key, `[${app}] ${f.feature}: ${f.gap || f.scenario || ''}`)} />}
                              </td>
                              <td style={td}>
                                <div style={{ fontWeight: 600 }}>{f.feature}</div>
                                {arr(f.screens).length > 0 && (
                                  <div style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>{arr(f.screens).join(', ')}</div>
                                )}
                              </td>
                              <td style={td}>{f.role || '—'}</td>
                              <td style={td}>{chip(f.status, COV[f.status] ?? GREY)}</td>
                              <td style={td}>{chip(f.risk, RISK[f.risk] ?? GREY)}</td>
                              <td style={{ ...td, ...mono, color: 'var(--text-secondary)' }}>
                                {arr(f.covered_by).length ? arr(f.covered_by).join(', ') : '—'}
                              </td>
                              <td style={{ ...td, color: 'var(--text-secondary)', minWidth: 220 }}>
                                {f.gap || '—'}
                                {f.scenario && gap && (
                                  <div style={{ color: 'var(--text-muted)', fontSize: '0.78rem', marginTop: 2 }}>Would test: {f.scenario}</div>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

const arr = (x: unknown): string[] => (Array.isArray(x) ? x.filter(v => typeof v === 'string') : []);

function Bar({ value, total }: { value: number; total: number }) {
  const pct = total ? Math.round((value / total) * 100) : 0;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <div style={{ flex: 1, height: 8, borderRadius: 99, background: 'var(--bg-tertiary)', overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: pct >= 70 ? GREEN : pct >= 40 ? AMBER : RED }} />
      </div>
      <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', minWidth: 90 }}>{value}/{total} ({pct}%)</span>
    </div>
  );
}

/** Every screen found in the apps' navigation, and whether a test drives one of its
 *  own element ids. Computed from the source, no AI. */
export function CoverageSection({ coverage, onRefresh, busy }: {
  coverage: Coverage | null; onRefresh: () => void; busy: boolean;
}) {
  const [showApp, setShowApp] = useState<string>('');
  return (
    <div style={section}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
        <h3 style={{ ...h3, marginBottom: 0 }}><ShieldCheck size={18} /> Screen coverage</h3>
        <button style={ghost} disabled={busy} onClick={onRefresh}>
          {busy ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} Recompute
        </button>
      </div>
      <p style={{ ...muted, marginBottom: 12 }}>
        Every screen found in the apps' navigation, and whether any test drives one of its own element ids.
        Computed from the source — no AI.
      </p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(320px, 100%), 1fr))', gap: 16 }}>
        {coverage ? Object.entries(coverage).map(([app, a]) => (
          <div key={app} className="card" style={{ padding: 20, transform: 'none', minWidth: 0 }}>
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
                  <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'baseline', minWidth: 0 }}>
                    {chip(r.covered ? 'tested' : 'untested', r.covered ? GREEN : RED)}
                    <span style={{ fontWeight: 600, ...wrapText }}>{r.route}</span>
                    <span style={{ color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
                      {r.file ?? ''} · {r.ids} ids{r.covered_ids.length ? ` · uses ${r.covered_ids.slice(0, 3).join(', ')}` : ''}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )) : <div className="card" style={{ padding: 20 }}><Loader2 size={16} className="spin" /> Mapping the apps…</div>}
      </div>
    </div>
  );
}
