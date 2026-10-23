import { useState } from 'react';
import type { CSSProperties } from 'react';
import { ChevronDown, ChevronRight, Loader2, Map as MapIcon, RefreshCw, ShieldCheck, Sparkles } from 'lucide-react';
import type { Coverage, Feature, FeatureMap, FeatureTotals } from '../../agentApi';
import { AMBER, BLUE, GREEN, GREY, RED, chip, ghost, h3, mono, muted, section, when, wrapText } from './ui';

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
  const [openApp, setOpenApp] = useState<Record<string, boolean>>({});   // folded until clicked
  const [openRow, setOpenRow] = useState<Record<string, boolean>>({});
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
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 12, flexWrap: 'wrap' }}>
            <button className="btn" disabled={!n || !canStart || starting} onClick={() => onCreate(Object.values(picked))}
                    title={canStart ? '' : 'A session is already running'}>
              {starting ? <Loader2 size={16} className="spin" /> : <Sparkles size={16} />}
              Create scenarios for selected{n ? ` (${n})` : ''}
            </button>
            {n > 0 && <button style={ghost} onClick={() => setPicked({})}>Clear</button>}
            <span style={muted}>Tick partial or missing features to write scenarios for them.</span>
          </div>

          <div style={{ display: 'grid', gap: 12 }}>
            {apps.map(([app, list]) => {
              const t = fmap.totals?.[app] ?? totalsOf(list);
              const isOpen = !!openApp[app];
              const chosenHere = Object.keys(picked).filter(k => k.startsWith(`${app}|`)).length;
              const rows = list
                .map((f, i) => ({ f, key: `${app}|${i}` }))
                .filter(({ f }) => showCovered || f.status !== 'covered')
                .sort((a, b) => ((ORDER[a.f.status] ?? 3) - (ORDER[b.f.status] ?? 3))
                  || ((RISK_ORDER[a.f.risk] ?? 3) - (RISK_ORDER[b.f.risk] ?? 3)));
              return (
                <div key={app} className="card" style={{ padding: 0, transform: 'none', minWidth: 0 }}>
                  <button type="button" aria-expanded={isOpen} onClick={() => setOpenApp(o => ({ ...o, [app]: !isOpen }))}
                          style={{ ...bareButton, width: '100%', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
                                   padding: '14px 18px' }}>
                    {isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                    <span style={{ fontWeight: 600 }}>{APP_LABEL[app] ?? app}</span>
                    <span style={muted}>{list.length} features</span>
                    <span style={{ marginLeft: 'auto', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {chosenHere > 0 && chip(`${chosenHere} selected`, BLUE)}
                      {chip(`${t.covered} covered`, GREEN)}{chip(`${t.partial} partial`, AMBER)}{chip(`${t.missing} missing`, RED)}
                    </span>
                  </button>
                  {isOpen && (
                    <div style={{ borderTop: '1px solid var(--border-color)' }}>
                      {rows.length === 0 ? (
                        <p style={{ ...muted, padding: '12px 18px' }}>Every feature is covered.</p>
                      ) : rows.map(({ f, key }, i) => (
                        <FeatureRow key={key} f={f} first={i === 0} picked={!!picked[key]} open={!!openRow[key]}
                                    onPick={() => toggle(key, `[${app}] ${f.feature}: ${f.gap || f.scenario || ''}`)}
                                    onToggle={() => setOpenRow(o => ({ ...o, [key]: !o[key] }))} />
                      ))}
                    </div>
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

const bareButton: CSSProperties = {
  background: 'none', border: 'none', color: 'inherit', font: 'inherit', cursor: 'pointer', textAlign: 'left',
};

/** One feature as a single line (tick, name, status, risk); click it for the details. */
function FeatureRow({ f, first, picked, open, onPick, onToggle }: {
  f: Feature; first: boolean; picked: boolean; open: boolean; onPick: () => void; onToggle: () => void;
}) {
  const gap = f.status !== 'covered';
  const label = (text: string) => <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{text}: </span>;
  return (
    <div style={{ borderTop: first ? 'none' : '1px solid var(--border-color)',
                  background: picked ? 'rgba(129,140,248,0.08)' : 'transparent' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 18px', minWidth: 0 }}>
        <span style={{ width: 16, flexShrink: 0, display: 'flex' }}>
          {gap && <input type="checkbox" checked={picked} onChange={onPick} aria-label={`Select ${f.feature}`} />}
        </span>
        <button type="button" aria-expanded={open} onClick={onToggle}
                style={{ ...bareButton, flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: 8, padding: 0 }}>
          {open ? <ChevronDown size={14} style={{ flexShrink: 0 }} /> : <ChevronRight size={14} style={{ flexShrink: 0 }} />}
          <span style={{ fontWeight: 600, fontSize: '0.86rem', minWidth: 0,
                         ...(open ? wrapText : { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }) }}>
            {f.feature}
          </span>
        </button>
        <span style={{ width: 78, flexShrink: 0, textAlign: 'right' }}>{chip(f.status, COV[f.status] ?? GREY)}</span>
        <span style={{ width: 70, flexShrink: 0, textAlign: 'right' }}>{chip(f.risk, RISK[f.risk] ?? GREY)}</span>
      </div>
      {open && (
        <div style={{ padding: '0 18px 14px 60px', display: 'grid', gap: 6, fontSize: '0.82rem',
                      color: 'var(--text-secondary)', ...wrapText }}>
          {f.gap && <div>{label('Gap')}{f.gap}</div>}
          {gap && f.scenario && <div>{label('Would test')}{f.scenario}</div>}
          {arr(f.screens).length > 0 && <div>{label('Screens')}{arr(f.screens).join(', ')}</div>}
          <div>{label('Covered by')}
            {arr(f.covered_by).length ? <span style={mono}>{arr(f.covered_by).join(', ')}</span> : 'no flow yet'}</div>
          {f.role && <div>{label('Role')}{f.role}</div>}
        </div>
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
