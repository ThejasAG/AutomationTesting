import { useEffect, useState, useCallback } from 'react';
import { getWorkflowCoverage, getWorkflowGraph, getWorkflowPath, recreateWorkflow,
         getWorkflowDiagram, getWorkflowDelta, getWorkflowRefs } from '../api';
import type { Coverage, SpineNode, Handoff, WorkflowDiagram, WorkflowDelta } from '../api';
import { GitBranch, CheckCircle2, Circle, Ban, Loader2, Smartphone, Store, ArrowRight, Sparkles, Play, Map, GitCompare } from 'lucide-react';
import ArchifyFrame from '../components/ArchifyFrame';

function statusColor(status: string, built?: boolean): string {
  if (status === 'manual') return '#6b7280';         // grey = can't automate
  if (built || status === 'built') return '#22c55e'; // green = built
  return '#eab308';                                   // amber = automatable, pending
}

function Lane({ title, icon, nodes, tint }: { title: string; icon: React.ReactNode; nodes: SpineNode[]; tint: string }) {
  return (
    <div style={{ border: `1px solid ${tint}55`, borderRadius: 10, padding: '12px 14px', background: `${tint}0d` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, color: tint, fontWeight: 700, fontSize: '0.85rem' }}>
        {icon} {title}
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6 }}>
        {nodes.map((n, i) => (
          <div key={n.id} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 5, padding: '6px 10px', borderRadius: 8,
              border: `1.5px solid ${statusColor(n.status)}`, background: `${statusColor(n.status)}18`, fontSize: '0.78rem', fontWeight: 600, whiteSpace: 'nowrap' }}>
              {n.status === 'built' ? <CheckCircle2 size={13} color={statusColor(n.status)} /> : <Circle size={13} color={statusColor(n.status)} />}
              {n.label}
            </div>
            {i < nodes.length - 1 && <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>→</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

export default function WorkflowPage() {
  const [cov, setCov] = useState<Coverage | null>(null);
  const [consumer, setConsumer] = useState<SpineNode[]>([]);
  const [business, setBusiness] = useState<SpineNode[]>([]);
  const [branches, setBranches] = useState<SpineNode[]>([]);
  const [handoffs, setHandoffs] = useState<Handoff[]>([]);
  const [loading, setLoading] = useState(true);
  // "Recreate a flow" — the agent reads the workflow and composes the path.
  const [goal, setGoal] = useState('b_pay_cash');
  const [plan, setPlan] = useState<{ path: { id: string; label: string }[]; scenarios: string[] } | null>(null);
  const [recMsg, setRecMsg] = useState('');
  const [allNodes, setAllNodes] = useState<SpineNode[]>([]);
  // The Archify map of the SAME catalog. Loaded separately from the lanes above so a
  // renderer problem degrades to a note instead of blanking the page.
  const [diagram, setDiagram] = useState<WorkflowDiagram | null>(null);
  const [diagramLoading, setDiagramLoading] = useState(true);
  const [baseRef, setBaseRef] = useState('');   // filled from the server's comparable refs
  const [delta, setDelta] = useState<WorkflowDelta | null>(null);
  const [refs, setRefs] = useState<string[]>([]);
  const [refsError, setRefsError] = useState('');
  const [deltaLoading, setDeltaLoading] = useState(false);

  useEffect(() => {
    Promise.all([getWorkflowCoverage(), getWorkflowGraph()])
      .then(([c, g]) => { setCov(c); setConsumer(g.consumer); setBusiness(g.business); setBranches(g.branches); setHandoffs(g.handoffs); setAllNodes(g.nodes); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    getWorkflowDiagram()
      .then(setDiagram)
      .catch(e => setDiagram({ ok: false, reason: String(e) }))
      .finally(() => setDiagramLoading(false));
  }, []);

  // Report WHY the ref list is empty. Swallowing the error and showing an empty
  // list said "no comparable refs found" for a stale page, an expired session and
  // a real empty repo alike — three very different problems, one wrong message.
  const loadRefs = useCallback(async () => {
    try {
      const r = await getWorkflowRefs();
      setRefs(r.refs);
      setRefsError(r.refs.length ? '' : 'This repository has no ref containing catalog.py.');
      setBaseRef(prev => (prev && r.refs.includes(prev)) ? prev : r.default);
    } catch (e) {
      setRefs([]);
      setRefsError(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => { loadRefs(); }, [loadRefs]);

  const loadDelta = async () => {
    setDeltaLoading(true);
    try { setDelta(await getWorkflowDelta(baseRef)); }
    catch (e) { setDelta({ ok: false, reason: String(e) }); }
    finally { setDeltaLoading(false); }
  };

  const computePath = async (g: string) => {
    setGoal(g); setRecMsg('');
    try { setPlan(await getWorkflowPath(g)); } catch { setPlan(null); }
  };
  const runComposed = async () => {
    setRecMsg('Composing from the workflow and running…');
    try { const r = await recreateWorkflow(goal); setRecMsg(r.started ? `▶ ${r.message}` : `✗ ${r.error}`); }
    catch (e) { setRecMsg('Failed: ' + String(e)); }
  };

  if (loading) return <div className="page-header"><h1 className="page-title"><Loader2 className="spin" /> Loading workflow…</h1></div>;
  if (!cov) return <div className="card">Could not load the workflow.</div>;

  const s = cov.summary;
  const pct = s.total ? Math.round((cov.categories.flatMap(c => c.scenarios).filter(x => x.built).length / s.total) * 100) : 0;

  return (
    <div className="animate-fade-in">
      <header className="page-header">
        <h1 className="page-title" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <GitBranch size={26} color="var(--accent-primary)" /> Application Workflow &amp; Coverage
        </h1>
        <p className="page-subtitle">The whole app's flow + what's automated, pending, or manual — one map.</p>
      </header>

      {/* Summary */}
      <div className="grid-3" style={{ marginBottom: 24 }}>
        <div className="card"><div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Total scenarios</div><div className="stat-value">{s.total}</div><div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>{s.categories} categories</div></div>
        <div className="card"><div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Automatable</div><div className="stat-value" style={{ color: '#eab308' }}>{s.automatable}</div><div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>build as spine variations</div></div>
        <div className="card"><div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Manual / blocked</div><div className="stat-value" style={{ color: '#6b7280' }}>{s.manual_blocked}</div><div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>never false-green</div></div>
      </div>

      {/* The cross-app spine: two lanes + handoffs */}
      <div className="card" style={{ marginBottom: 24 }}>
        <h3 style={{ margin: '0 0 4px', fontSize: '1rem' }}>Cross-app flow — Consumer ⇄ Business, and where they connect</h3>
        <p style={{ margin: '0 0 16px', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>Everything else in the suite is a variation of this core flow.</p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <Lane title="CONSUMER APP" icon={<Smartphone size={15} />} nodes={consumer} tint="#3b82f6" />
          <Lane title="BUSINESS APP (waiter + kitchen)" icon={<Store size={15} />} nodes={business} tint="#a855f7" />
        </div>

        {/* Handoffs — the cross-app connection */}
        <div style={{ marginTop: 18 }}>
          <div style={{ fontSize: '0.8rem', fontWeight: 700, marginBottom: 8, color: 'var(--accent-primary)' }}>🔗 Cross-app handoffs (how the two apps connect)</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {handoffs.map((h, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                <ArrowRight size={14} color="var(--accent-primary)" />
                {h.label}
              </div>
            ))}
          </div>
        </div>

        {/* Branches (decision points) */}
        {branches.length > 0 && (
          <div style={{ marginTop: 18 }}>
            <div style={{ fontSize: '0.8rem', fontWeight: 700, marginBottom: 8 }}>🔀 Branches — after <b>Notify payment</b> the flow can go different ways:</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              {branches.map(b => (
                <div key={b.id} style={{ padding: '6px 10px', borderRadius: 8, border: `1.5px dashed ${statusColor(b.status)}`, fontSize: '0.76rem', fontWeight: 600 }}>{b.label}</div>
              ))}
              <div style={{ padding: '6px 10px', borderRadius: 8, border: '1.5px dashed #3b82f6', fontSize: '0.76rem', fontWeight: 600, color: '#3b82f6' }}>Pay: Consumer App</div>
            </div>
          </div>
        )}

        <div style={{ marginTop: 16, display: 'flex', gap: 16, fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
          <span>🟢 built</span><span>🟡 automatable — pending</span><span>⚫ manual / blocked</span>
        </div>
      </div>

      {/* Interactive map of the SAME catalog (Archify artifact) */}
      <div className="card" style={{ marginBottom: 24 }}>
        <h3 style={{ margin: '0 0 4px', fontSize: '1rem', display: 'flex', alignItems: 'center', gap: 8 }}>
          <Map size={17} color="var(--accent-primary)" /> Interactive system map
        </h3>
        <p style={{ margin: '0 0 14px', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
          The same spine, rendered from the same catalog — search nodes, trace a route, and
          compare roles. Dashed edges are the cross-app handoffs.
        </p>
        <ArchifyFrame
          title="Vyapy cross-app spine"
          html={diagram?.ok ? diagram.html : undefined}
          loading={diagramLoading}
          reason={diagram?.reason}
        />
      </div>

      {/* Before / Delta / After — what a change did to the flow */}
      <div className="card" style={{ marginBottom: 24 }}>
        <h3 style={{ margin: '0 0 4px', fontSize: '1rem', display: 'flex', alignItems: 'center', gap: 8 }}>
          <GitCompare size={17} color="var(--accent-primary)" /> What changed about the flow
        </h3>
        <p style={{ margin: '0 0 12px', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
          The spine is defined in <code>automation/workflow/catalog.py</code>, so a change to it
          changes this map. Compare a base ref against the working tree — these are
          branches of the platform repo, not of the app under test.
        </p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 14 }}>
          <select
            value={baseRef}
            onChange={e => setBaseRef(e.target.value)}
            style={{ padding: '7px 10px', borderRadius: 8, border: '1px solid var(--border-color)',
                     background: 'transparent', color: 'inherit', font: 'inherit', width: 300,
                     cursor: 'pointer' }}
          >
            {refs.length === 0 && <option value="">{refsError ? 'Could not load refs' : 'Loading…'}</option>}
            {refs.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
          <button className="btn" onClick={loadDelta} disabled={deltaLoading || !baseRef.trim()}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '8px 16px' }}>
            {deltaLoading ? <Loader2 size={14} className="spin" /> : <GitCompare size={14} />} Compare
          </button>
          {refsError && (
            <button className="btn" onClick={loadRefs}
              style={{ padding: '8px 14px' }}>Retry</button>
          )}
        </div>
        {refsError && (
          <div style={{ marginBottom: 14, fontSize: '0.78rem', color: '#fbbf24' }}>
            Could not load the ref list: {refsError}. If the page has been open a while,
            reload it — the backend may have restarted since.
          </div>
        )}

        {delta?.ok && delta.summary && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 14 }}>
            {([['Steps', delta.summary.components], ['Connections', delta.summary.connections],
               ['Regions', delta.summary.boundaries]] as const).map(([label, c]) => (
              <div key={label} style={{ border: '1px solid var(--border-color)', borderRadius: 8,
                                        padding: '7px 11px', fontSize: '0.78rem' }}>
                <strong>{label}</strong>{' '}
                <span style={{ color: '#22c55e' }}>+{c.added}</span>{' · '}
                <span style={{ color: '#ef4444' }}>−{c.removed}</span>{' · '}
                <span style={{ color: '#eab308' }}>~{c.changed}</span>
              </div>
            ))}
            {!delta.summary.components.added && !delta.summary.components.removed &&
             !delta.summary.connections.added && !delta.summary.connections.removed && (
              <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', alignSelf: 'center' }}>
                No structural change to the flow.
              </div>
            )}
          </div>
        )}

        {delta && (
          <ArchifyFrame
            title="Spine delta"
            html={delta.ok ? delta.html : undefined}
            loading={deltaLoading}
            reason={delta.reason}
            height={680}
          />
        )}
      </div>

      {/* Agent reads the workflow to recreate a flow */}
      <div className="card" style={{ marginBottom: 24, borderLeft: '4px solid var(--accent-primary)' }}>
        <h3 style={{ margin: '0 0 4px', fontSize: '1rem', display: 'flex', alignItems: 'center', gap: 8 }}>
          <Sparkles size={16} color="var(--accent-primary)" /> Recreate a flow from the workflow
        </h3>
        <p style={{ margin: '0 0 12px', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
          Pick a goal — the platform reads the graph, figures out the path, and composes which building blocks to run. No hand-written steps.
        </p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <select value={goal} onChange={e => computePath(e.target.value)}
            style={{ padding: '8px 10px', borderRadius: 8, background: 'var(--bg-secondary)', color: 'var(--text-primary)', border: '1px solid var(--border-color)' }}>
            {allNodes.map(n => <option key={n.id} value={n.id}>{n.label}</option>)}
          </select>
          <button className="btn" onClick={() => computePath(goal)}>Compute path</button>
          {plan && plan.scenarios.length > 0 && (
            <button className="btn" onClick={runComposed} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <Play size={13} /> Run composed flow
            </button>
          )}
        </div>
        {plan && (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: '0.82rem' }}><b>Path:</b> {plan.path.map(p => p.label).join('  →  ')}</div>
            <div style={{ fontSize: '0.82rem', marginTop: 6 }}><b>Building blocks the platform will run:</b> {plan.scenarios.length ? plan.scenarios.join(' · ') : '(none built yet for this goal)'}</div>
          </div>
        )}
        {recMsg && <div style={{ marginTop: 10, fontSize: '0.82rem', color: 'var(--text-secondary)' }}>{recMsg}</div>}
      </div>

      {/* Coverage matrix */}
      <h3 style={{ margin: '0 0 12px', fontSize: '1rem' }}>Coverage matrix ({pct}% of scenarios built as reusable flows)</h3>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 14 }}>
        {cov.categories.map(cat => (
          <div key={cat.category} className="card" style={{ padding: '14px 16px', opacity: cat.blocked ? 0.85 : 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
              {cat.blocked && <Ban size={14} color="#6b7280" />}
              <strong style={{ fontSize: '0.9rem' }}>{cat.category}</strong>
              <span style={{ marginLeft: 'auto', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                {cat.scenarios.filter(x => x.built).length}/{cat.scenarios.length}
              </span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
              {cat.scenarios.map((sc, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.78rem' }}>
                  <span style={{ width: 8, height: 8, borderRadius: 999, background: statusColor(sc.status, sc.built), flexShrink: 0 }} />
                  <span style={{ color: sc.status === 'manual' ? 'var(--text-muted)' : 'var(--text-primary)', textDecoration: sc.status === 'manual' ? 'line-through' : 'none' }}>{sc.name}</span>
                  {sc.built && <span style={{ marginLeft: 'auto', fontSize: '0.65rem', color: '#22c55e' }}>built</span>}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
