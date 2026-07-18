import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import * as d3 from 'd3';
import {
  getAppGroups, scanDependencies, getDependencyGraph, analyzeHybridImpact,
  getDevices, getProjects, getModuleGraph,
} from '../api';
import type {
  AppGroup, DependencyGraphData, GraphNode, HybridImpact, Device, Project,
} from '../api';
import {
  Radar, Loader2, AlertTriangle, Play, Zap, Info, Network, CheckCircle2, FileCode,
} from 'lucide-react';

type GraphType = 'api' | 'module';

const AFFECTED = '#ef4444';
const DIMMED = '#334155';

// Node palette (bright, readable on a light-ish canvas).
const APP_COLORS = ['#6366f1', '#06b6d4', '#10b981', '#f43f5e', '#a855f7', '#f59e0b', '#0ea5e9', '#84cc16'];
const SHARED_ENDPOINT = '#f59e0b';   // used by >1 app — the cross-app links that matter
const PRIVATE_ENDPOINT = '#64748b';  // called by a single app
const ISOLATED = '#3f4756';          // connected to nothing

const idOf = (v: any): string => (typeof v === 'string' ? v : v?.id);

const isSharedEndpoint = (d: any) => d.type === 'endpoint' && ((d.used_by?.length ?? 0) > 1 || d.shared);

function LegendRow({ swatch, label }: { swatch: React.ReactNode; label: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <span style={{ display: 'inline-flex', width: 16, justifyContent: 'center' }}>{swatch}</span>
      {label}
    </div>
  );
}

export default function GraphPage() {
  const navigate = useNavigate();
  const svgRef = useRef<SVGSVGElement | null>(null);
  const tipRef = useRef<HTMLDivElement | null>(null);

  const [graphType, setGraphType] = useState<GraphType>('api');
  const [groups, setGroups] = useState<AppGroup[]>([]);
  const [groupId, setGroupId] = useState('');
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState('');
  const [graph, setGraph] = useState<DependencyGraphData | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);

  const [loading, setLoading] = useState(true);
  const [moduleLoading, setModuleLoading] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);

  const [changedText, setChangedText] = useState('');
  const [impact, setImpact] = useState<HybridImpact | null>(null);
  const [running, setRunning] = useState(false);

  // ── Load ───────────────────────────────────────────────────────────────────

  useEffect(() => {
    (async () => {
      try {
        const gs = await getAppGroups();
        setGroups(gs);
        if (gs.length) setGroupId(gs[0].id);
      } catch (e: any) {
        setError(e.message ?? 'Failed to load app groups.');
      } finally {
        setLoading(false);
      }
      getProjects()
        .then(ps => {
          const cloned = ps.filter(p => p.clone_status !== 'not_cloned' && p.clone_status !== 'clone_failed');
          setProjects(cloned);
          if (cloned.length) setProjectId(cloned[0].id);
        })
        .catch(() => {});
      getDevices().then(r => setDevices(r.devices)).catch(() => {});
    })();
  }, []);

  // One loader, two sources: the cross-app endpoint graph (per group) or the
  // intra-app module graph (per project). Both return {nodes, links} so the same
  // renderer draws either.
  useEffect(() => {
    setImpact(null); setSelected(null); setPinned(null); setError(null);
    if (graphType === 'api') {
      if (!groupId) { setGraph(null); return; }
      getDependencyGraph(groupId)
        .then(setGraph)
        .catch(e => setError(e.message ?? 'Failed to load graph.'));
    } else {
      if (!projectId) { setGraph(null); return; }
      setGraph(null);
      setModuleLoading(true);
      getModuleGraph(projectId)
        .then(setGraph)
        .catch(e => setError(e.message ?? 'Failed to build module graph.'))
        .finally(() => setModuleLoading(false));
    }
  }, [graphType, groupId, projectId]);

  // ── Impact-derived highlight sets ──────────────────────────────────────────

  const affectedApps = useMemo(() => {
    if (!impact) return new Set<string>();
    const s = new Set<string>(impact.cross_app_impact.map(i => i.app));
    if (impact.primary_app) s.add(impact.primary_app);
    return s;
  }, [impact]);

  const affectedEndpoints = useMemo(
    () => new Set(impact?.affected_endpoints ?? []),
    [impact],
  );

  // Highlight source: a pinned click wins over a transient hover.
  const focus = pinned ?? hovered;

  const neighbours = useMemo(() => {
    const s = new Set<string>();
    if (!focus || !graph) return s;
    s.add(focus);
    graph.links.forEach(l => {
      const a = idOf(l.source), b = idOf(l.target);
      if (a === focus) s.add(b);
      if (b === focus) s.add(a);
    });
    return s;
  }, [focus, graph]);

  // ── Actions ────────────────────────────────────────────────────────────────

  const doScan = async () => {
    if (!groupId) return;
    setScanning(true); setError(null);
    try {
      const res = await scanDependencies(groupId);
      if (res.skipped_not_cloned?.length) {
        setError(`Scanned, but not cloned (skipped): ${res.skipped_not_cloned.join(', ')}`);
      }
      setGraph(await getDependencyGraph(groupId));
      setImpact(null);
    } catch (e: any) {
      setError(e.message ?? 'Scan failed.');
    } finally {
      setScanning(false);
    }
  };

  const doAnalyze = async (create_runs = false) => {
    const files = changedText.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    if (!files.length) return alert('Paste one or more changed file paths.');
    if (!groupId) return;

    create_runs ? setRunning(true) : setAnalyzing(true);
    setError(null);
    try {
      const device = devices[0];
      const res = await analyzeHybridImpact(groupId, files, {
        create_runs,
        device_id: device?.id,
      });
      setImpact(res);
      if (res.error) setError(res.error);
      if (create_runs) {
        const first = res.queued_runs?.find(r => r.run_id);
        if (first?.run_id) navigate(`/run/${first.run_id}`);
      }
    } catch (e: any) {
      setError(e.message ?? 'Impact analysis failed.');
    } finally {
      setAnalyzing(false); setRunning(false);
    }
  };

  // ── D3 force simulation ────────────────────────────────────────────────────

  useEffect(() => {
    if (!svgRef.current || !graph?.nodes.length) return;

    const width = svgRef.current.clientWidth || 900;
    const height = svgRef.current.clientHeight || 600;
    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();
    svg.attr('viewBox', `0 0 ${width} ${height}`);

    // A big graph (module view) needs a much airier layout than a handful of apps.
    const dense = graph.nodes.length > 45;

    // d3-force mutates what it is given — copy, or it rewrites React state.
    const nodes: any[] = graph.nodes.map(n => ({ ...n }));
    const links: any[] = graph.links.map(l => ({ ...l }));

    // Connectivity: a node touching no link is "isolated" — nothing depends on it
    // and it depends on nothing in this group. That's a real signal (an app that
    // shares no endpoint, or a dead endpoint), so it gets its own muted style.
    const degree: Record<string, number> = {};
    links.forEach(l => {
      degree[idOf(l.source)] = (degree[idOf(l.source)] ?? 0) + 1;
      degree[idOf(l.target)] = (degree[idOf(l.target)] ?? 0) + 1;
    });
    const isIsolated = (d: any) => !degree[d.id];

    // Assign a stable palette. Apps get distinct bright hues; endpoints are amber
    // if shared across apps (the important ones) or slate if private to one.
    const appIds = nodes.filter(n => n.type === 'app').map(n => n.id);
    const appColor = d3.scaleOrdinal<string>().domain(appIds).range(APP_COLORS);
    nodes.forEach(n => {
      if (isIsolated(n)) n.color = ISOLATED;
      else if (n.type === 'app') n.color = appColor(n.id);
      else n.color = isSharedEndpoint(n) ? SHARED_ENDPOINT : PRIVATE_ENDPOINT;
      n.__isolated = isIsolated(n);
      n.__shared = isSharedEndpoint(n);
    });

    // Soft glow so shared endpoints and apps read as "alive".
    const defs = svg.append('defs');
    const glow = defs.append('filter').attr('id', 'glow').attr('x', '-50%').attr('y', '-50%').attr('width', '200%').attr('height', '200%');
    glow.append('feGaussianBlur').attr('stdDeviation', '3').attr('result', 'blur');
    const merge = glow.append('feMerge');
    merge.append('feMergeNode').attr('in', 'blur');
    merge.append('feMergeNode').attr('in', 'SourceGraphic');

    const g = svg.append('g');
    svg.call(
      d3.zoom<SVGSVGElement, unknown>()
        .scaleExtent([0.3, 3])
        .on('zoom', ev => g.attr('transform', ev.transform)) as any,
    );

    // Click empty canvas → clear the pin and show the whole graph again. Without
    // this, once a node is pinned the only way back is clicking that exact node,
    // which is easy to lose.
    svg.on('click', (ev: any) => {
      if (ev.target === svgRef.current) { setPinned(null); setSelected(null); }
    });

    // Node size reflects how many connections it has. Smaller in dense graphs so
    // there's room to breathe.
    const conn = (d: any) => d.connections ?? 1;
    const rScale = d3.scaleSqrt()
      .domain([1, d3.max(nodes, conn) as number || 1])
      .range(dense ? [7, 22] : [18, 40]);
    const radiusOf = (d: any) => (d.type === 'app' ? rScale(conn(d)) : dense ? 13 : 30);

    const sim = d3.forceSimulation(nodes)
      // Longer links + far stronger repulsion + a bigger no-overlap gap = space.
      .force('link', d3.forceLink(links).id((d: any) => d.id)
        .distance(dense ? 70 : 140).strength(dense ? 0.12 : 0.55))
      .force('charge', d3.forceManyBody()
        .strength(dense ? -520 : -480).distanceMax(dense ? 600 : Infinity))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide()
        .radius((d: any) => radiusOf(d) + (dense ? 22 : 12)).iterations(2))
      // Gentle gravity keeps the cloud from drifting apart into the corners.
      .force('x', d3.forceX(width / 2).strength(dense ? 0.03 : 0.02))
      .force('y', d3.forceY(height / 2).strength(dense ? 0.03 : 0.02));

    const link = g.append('g')
      .selectAll('line')
      .data(links)
      .join('line')
      .attr('class', 'dep-link')
      .attr('stroke', DIMMED)
      .attr('stroke-opacity', 0.5)
      .attr('stroke-width', 1.5);

    const node = g.append('g')
      .selectAll<SVGGElement, any>('g')
      .data(nodes)
      .join('g')
      .style('cursor', 'pointer')
      .call(d3.drag<SVGGElement, any>()
        .on('start', (ev, d) => { if (!ev.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
        .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
        .on('end', (ev, d) => { if (!ev.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }) as any)
      .on('click', (ev, d) => {
        ev.stopPropagation();  // don't let the canvas-clear handler fire
        setSelected(d);
        setPinned(prev => (prev === d.id ? null : d.id));
      })
      .on('mouseenter', (ev, d) => {
        setHovered(d.id);
        const tip = tipRef.current;
        if (!tip) return;
        tip.style.display = 'block';
        tip.style.left = `${ev.clientX + 14}px`;
        tip.style.top = `${ev.clientY + 14}px`;
        tip.innerHTML = d.type === 'app'
          ? `<strong>${d.id}</strong><br/>role: ${d.app_role ?? '—'}<br/>suite: ${d.test_suite ?? '—'}<br/>${d.connections} endpoint(s)`
          : `<strong>${d.id}</strong><br/>used by: ${(d.used_by ?? []).join(', ') || '—'}${d.shared ? '<br/><em>shared endpoint</em>' : ''}`;
      })
      .on('mousemove', ev => {
        const tip = tipRef.current;
        if (tip) { tip.style.left = `${ev.clientX + 14}px`; tip.style.top = `${ev.clientY + 14}px`; }
      })
      .on('mouseleave', () => {
        setHovered(null);
        if (tipRef.current) tipRef.current.style.display = 'none';
      });

    // Apps = circles sized by connections. Connected ones glow; isolated ones are
    // dimmed with a dashed ring so "shares nothing" is obvious at a glance.
    node.filter((d: any) => d.type === 'app')
      .append('circle')
      .attr('r', (d: any) => rScale(conn(d)))
      .attr('fill', (d: any) => d.color)
      .attr('fill-opacity', (d: any) => (d.__isolated ? 0.3 : 1))
      .attr('stroke', (d: any) => (d.__isolated ? '#64748b' : '#e2e8f0'))
      .attr('stroke-width', 2)
      .attr('stroke-dasharray', (d: any) => (d.__isolated ? '4 3' : 'none'))
      .attr('filter', (d: any) => (d.__isolated ? null : 'url(#glow)'));

    // Endpoints = rounded rects. Shared (cross-app) endpoints get amber + glow +
    // a heavier ring; private ones stay muted; isolated ones dashed.
    const w = (d: any) => Math.min(Math.max(String(d.id).length * 6, 90), 230);
    node.filter((d: any) => d.type === 'endpoint')
      .append('rect')
      .attr('width', w)
      .attr('height', (d: any) => (d.__shared ? 28 : 24))
      .attr('x', (d: any) => -w(d) / 2)
      .attr('y', (d: any) => (d.__shared ? -14 : -12))
      .attr('rx', 5)
      .attr('fill', (d: any) => d.color)
      .attr('fill-opacity', (d: any) => (d.__isolated ? 0.35 : d.__shared ? 1 : 0.85))
      .attr('stroke', (d: any) => (d.__shared ? '#fde68a' : '#1e293b'))
      .attr('stroke-width', (d: any) => (d.__shared ? 2 : 1))
      .attr('stroke-dasharray', (d: any) => (d.__isolated ? '4 3' : 'none'))
      .attr('filter', (d: any) => (d.__shared ? 'url(#glow)' : null));

    // In a dense graph, labelling every node is noise — only label the hubs
    // (top ~40% by connections); the rest reveal their name on hover.
    const labelCut = dense
      ? (d3.quantile(nodes.map(conn).sort(d3.ascending), 0.6) ?? 0)
      : -1;

    node.append('text')
      .text((d: any) => (dense && conn(d) < labelCut ? '' : (d.label ?? d.id)))
      .attr('text-anchor', 'middle')
      .attr('dy', (d: any) => (d.type === 'app' ? radiusOf(d) + (dense ? 11 : 16) : 4))
      .attr('font-size', (d: any) => (d.type === 'app' ? (dense ? 8.5 : 12) : 9.5))
      .attr('font-family', 'monospace')
      .attr('font-weight', (d: any) => (d.type === 'app' ? 700 : d.__shared ? 700 : 500))
      // App labels sit below the node (dark, on the light canvas); endpoint labels
      // sit inside the rect, so they need white for contrast on amber/slate.
      .attr('fill', (d: any) => (d.type === 'app' ? '#e2e8f0' : '#ffffff'))
      .style('pointer-events', 'none');

    sim.on('tick', () => {
      link
        .attr('x1', (d: any) => d.source.x).attr('y1', (d: any) => d.source.y)
        .attr('x2', (d: any) => d.target.x).attr('y2', (d: any) => d.target.y);
      node.attr('transform', (d: any) => `translate(${d.x},${d.y})`);
    });

    return () => { sim.stop(); };
  }, [graph]);

  // Highlight + impact colouring, applied without restarting the simulation.
  useEffect(() => {
    if (!svgRef.current || !graph) return;
    const svg = d3.select(svgRef.current);
    const dim = (id: string) => focus !== null && !neighbours.has(id);

    svg.selectAll<SVGGElement, any>('g > g')
      .attr('opacity', (d: any) => (d?.id && dim(d.id) ? 0.28 : 1));

    svg.selectAll<SVGCircleElement, any>('circle')
      .attr('fill', (d: any) =>
        !impact ? d.color : affectedApps.has(d.id) ? AFFECTED : DIMMED);

    svg.selectAll<SVGRectElement, any>('rect')
      .attr('fill', (d: any) =>
        !impact ? d.color : affectedEndpoints.has(d.id) ? AFFECTED : DIMMED);

    svg.selectAll<SVGLineElement, any>('line')
      .attr('stroke', (d: any) => {
        const s = idOf(d.source), t = idOf(d.target);
        return impact && affectedApps.has(s) && affectedEndpoints.has(t) ? AFFECTED : DIMMED;
      })
      .attr('stroke-opacity', (d: any) => {
        const s = idOf(d.source), t = idOf(d.target);
        if (focus) return s === focus || t === focus ? 1 : 0.06;
        return 0.5;
      })
      // Animated dashes mark the edges the impact actually travels along.
      .attr('class', (d: any) => {
        const s = idOf(d.source), t = idOf(d.target);
        return impact && affectedApps.has(s) && affectedEndpoints.has(t)
          ? 'dep-link dep-link-affected' : 'dep-link';
      });
  }, [focus, neighbours, impact, graph, affectedApps, affectedEndpoints]);

  if (loading) {
    return <div className="page-header"><h1 className="page-title">Loading dependency graph…</h1></div>;
  }

  return (
    <div className="animate-fade-in">
      <style>{`
        @keyframes spin { to { transform: rotate(360deg) } }
        .spin { animation: spin 1s linear infinite }
        @keyframes dashflow { to { stroke-dashoffset: -16 } }
        .dep-link-affected {
          stroke-width: 2.5px;
          stroke-dasharray: 6 4;
          animation: dashflow .6s linear infinite;
        }
        .graph-tip {
          position: fixed; display: none; z-index: 999; pointer-events: none;
          background: var(--bg-elevated, #1e293b); color: var(--text-primary, #e2e8f0);
          border: 1px solid rgba(255,255,255,0.12); border-radius: 6px;
          padding: 8px 10px; font-size: .75rem; line-height: 1.5;
          box-shadow: 0 8px 24px rgba(0,0,0,.4);
        }
      `}</style>

      <div ref={tipRef} className="graph-tip" />

      <header className="page-header">
        <h1 className="page-title">Dependency Graph</h1>
        <p className="page-subtitle">
          Graphify maps each app's internal dependencies; the endpoint graph joins the apps.
          A change to a file with no HTTP call of its own still reaches the apps it can break.
        </p>
      </header>

      {/* Top bar */}
      <div className="card" style={{ marginBottom: 20, display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        {/* Graph type toggle */}
        <div style={{ display: 'flex', background: 'var(--bg-base, #0d1117)', borderRadius: 8, padding: 3, border: '1px solid rgba(255,255,255,0.1)' }}>
          {([['api', 'Cross-app (API)', Network], ['module', 'Module (files)', FileCode]] as const).map(([t, label, Icon]) => (
            <button key={t} onClick={() => setGraphType(t)}
              style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '7px 14px', borderRadius: 6, border: 'none', cursor: 'pointer', fontSize: '0.8rem',
                background: graphType === t ? 'var(--primary)' : 'transparent',
                color: graphType === t ? '#fff' : 'var(--text-secondary)' }}>
              <Icon size={14} /> {label}
            </button>
          ))}
        </div>

        {graphType === 'api' ? (
          <select value={groupId} onChange={e => setGroupId(e.target.value)}
            style={{ padding: 10, borderRadius: 6, background: 'var(--bg-elevated)', color: 'var(--text-primary)', border: '1px solid rgba(255,255,255,0.1)', minWidth: 220 }}>
            {groups.length === 0 && <option value="">No app groups</option>}
            {groups.map(g => <option key={g.id} value={g.id}>{g.name} ({g.member_count} apps)</option>)}
          </select>
        ) : (
          <select value={projectId} onChange={e => setProjectId(e.target.value)}
            style={{ padding: 10, borderRadius: 6, background: 'var(--bg-elevated)', color: 'var(--text-primary)', border: '1px solid rgba(255,255,255,0.1)', minWidth: 220 }}>
            {projects.length === 0 && <option value="">No cloned projects</option>}
            {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        )}

        {graphType === 'api' && (
          <>
            <button onClick={doScan} disabled={!groupId || scanning}
              style={{ padding: '10px 16px', borderRadius: 6, background: 'var(--primary)', color: '#fff', border: 'none', display: 'flex', alignItems: 'center', gap: 8, cursor: !groupId || scanning ? 'not-allowed' : 'pointer', opacity: !groupId || scanning ? 0.5 : 1 }}>
              {scanning ? <Loader2 size={16} className="spin" /> : <Radar size={16} />}
              {scanning ? 'Scanning…' : 'Scan'}
            </button>
            <button onClick={() => doAnalyze(false)} disabled={!graph?.nodes.length || analyzing}
              style={{ padding: '10px 16px', borderRadius: 6, background: 'var(--bg-elevated)', color: 'var(--text-primary)', border: '1px solid rgba(255,255,255,0.12)', display: 'flex', alignItems: 'center', gap: 8, cursor: !graph?.nodes.length || analyzing ? 'not-allowed' : 'pointer', opacity: !graph?.nodes.length || analyzing ? 0.5 : 1 }}>
              {analyzing ? <Loader2 size={16} className="spin" /> : <Zap size={16} />}
              Analyze Impact
            </button>
          </>
        )}

        <div style={{ flex: 1 }} />

        {graph?.stats && (
          <div style={{ display: 'flex', gap: 16, fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
            {graphType === 'api' ? (
              <>
                <span>{graph.stats.apps ?? 0} apps</span>
                <span>{graph.stats.endpoints ?? 0} endpoints</span>
                <span style={{ color: 'var(--warning)' }}>{graph.stats.shared_endpoints ?? 0} shared</span>
              </>
            ) : (
              <>
                <span>{graph.stats.apps ?? 0} files</span>
                <span>{graph.stats.edges ?? 0} dependencies</span>
                {(graph.stats as any).truncated && (
                  <span style={{ color: 'var(--text-muted)' }}>top {graph.stats.apps} of {(graph.stats as any).total_files}</span>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {error && (
        <div className="card" style={{ marginBottom: 20, border: '1px solid var(--warning)', background: 'rgba(234,179,8,0.08)' }}>
          <div style={{ color: 'var(--warning)', display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertTriangle size={16} /> {error}
          </div>
        </div>
      )}

      {/* Graph + right panel */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 20 }}>
        <div className="card" style={{ padding: 0, overflow: 'hidden', position: 'relative',
          background: 'radial-gradient(circle at 50% 35%, #1b2540 0%, #131a2b 55%, #0e1420 100%)' }}>
          {moduleLoading ? (
            <div style={{ padding: 60, textAlign: 'center', color: 'var(--text-muted)', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12 }}>
              <Loader2 size={28} className="spin" />
              Building the module graph from the app's AST…
            </div>
          ) : !graph?.nodes.length ? (
            <div style={{ padding: 60, textAlign: 'center', color: 'var(--text-muted)' }}>
              {graphType === 'module'
                ? <>Select a cloned project to see its internal file dependencies.</>
                : groups.length
                ? <>No graph yet. Click <strong>Scan</strong> to map this group.</>
                : <>Create an app group first, then scan it.</>}
            </div>
          ) : (
            <>
              <svg ref={svgRef} style={{ width: '100%', height: (graph?.nodes.length ?? 0) > 45 ? 780 : 600, display: 'block' }} />
              {pinned && (
                <button
                  onClick={() => { setPinned(null); setSelected(null); }}
                  style={{ position: 'absolute', top: 12, right: 12, padding: '7px 14px', borderRadius: 8,
                    background: 'rgba(10,14,22,0.82)', color: 'var(--text-primary)', border: '1px solid rgba(255,255,255,0.18)',
                    fontSize: '0.78rem', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6 }}
                >
                  <Network size={13} /> Show whole graph
                </button>
              )}
              {/* Legend */}
              <div style={{ position: 'absolute', top: 12, left: 12, display: 'flex', flexDirection: 'column', gap: 6,
                background: 'rgba(10,14,22,0.72)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, padding: '10px 12px', fontSize: '0.72rem', color: 'var(--text-secondary)' }}>
                <LegendRow swatch={<span style={{ width: 12, height: 12, borderRadius: '50%', background: APP_COLORS[0], boxShadow: '0 0 6px ' + APP_COLORS[0] }} />} label="App" />
                <LegendRow swatch={<span style={{ width: 16, height: 10, borderRadius: 3, background: SHARED_ENDPOINT, boxShadow: '0 0 6px ' + SHARED_ENDPOINT }} />} label="Shared endpoint (cross-app)" />
                <LegendRow swatch={<span style={{ width: 16, height: 10, borderRadius: 3, background: PRIVATE_ENDPOINT }} />} label="Private endpoint" />
                <LegendRow swatch={<span style={{ width: 12, height: 12, borderRadius: '50%', background: 'transparent', border: '1.5px dashed #64748b' }} />} label="Isolated (connects to nothing)" />
              </div>
            </>
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {/* Analyze Impact input — cross-app only (module graph is a single app) */}
          {graphType === 'api' && (
          <div className="card">
            <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem', display: 'flex', alignItems: 'center', gap: 8 }}>
              <Zap size={15} /> Analyze Impact
            </h3>
            <textarea
              value={changedText}
              onChange={e => setChangedText(e.target.value)}
              placeholder={'Changed files, one per line:\nOrderStatus.swift\nApp/Screens/Order.js'}
              rows={4}
              style={{ width: '100%', boxSizing: 'border-box', padding: 8, borderRadius: 6, background: 'var(--bg-base, #0d1117)', color: 'var(--text-primary)', border: '1px solid rgba(255,255,255,0.1)', fontFamily: 'monospace', fontSize: '0.75rem', resize: 'vertical' }}
            />
            <button onClick={() => doAnalyze(false)} disabled={analyzing || !graph?.nodes.length}
              style={{ width: '100%', marginTop: 10, padding: 9, borderRadius: 6, background: 'var(--primary)', color: '#fff', border: 'none', display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 8, cursor: analyzing ? 'not-allowed' : 'pointer', opacity: analyzing ? 0.6 : 1 }}>
              {analyzing ? <Loader2 size={14} className="spin" /> : <Zap size={14} />} Analyze
            </button>
          </div>
          )}

          {/* Impact results */}
          {impact && (
            <div className="card" style={{ border: impact.total_apps_affected ? '1px solid var(--danger)' : '1px solid rgba(255,255,255,0.08)' }}>
              <h3 style={{ margin: '0 0 4px', fontSize: '0.95rem', display: 'flex', alignItems: 'center', gap: 8 }}>
                <Network size={15} color={impact.total_apps_affected ? 'var(--danger)' : 'var(--text-muted)'} />
                Impact
              </h3>
              <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: 12 }}>
                <strong style={{ color: impact.total_apps_affected ? 'var(--danger)' : 'var(--text-primary)' }}>
                  {impact.total_apps_affected + (impact.primary_app ? 1 : 0)} app(s) affected
                </strong>
                {', '}{impact.all_test_suites_to_run.length} suite(s) selected
              </div>

              {/* Graphify blast radius — the value the hybrid adds */}
              <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase' }}>
                Blast radius (Graphify)
              </div>
              <div style={{ fontSize: '0.72rem', fontFamily: 'monospace', color: 'var(--text-secondary)', marginBottom: 12, maxHeight: 90, overflowY: 'auto' }}>
                {impact.intra_app_blast_radius.length
                  ? impact.intra_app_blast_radius.map(f => <div key={f}>{f}</div>)
                  : <span style={{ color: 'var(--text-muted)' }}>—</span>}
                <div style={{ color: 'var(--text-muted)', marginTop: 4 }}>
                  {impact.graphify_nodes_affected} node(s) via AST
                  {!impact.graphify_available && ' · graphify unavailable'}
                </div>
              </div>

              <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase' }}>
                Endpoints
              </div>
              <div style={{ marginBottom: 12 }}>
                {impact.affected_endpoints.length ? impact.affected_endpoints.map(e => (
                  <code key={e} style={{ display: 'block', fontSize: '0.7rem', padding: '2px 6px', marginBottom: 3, borderRadius: 4, background: 'rgba(239,68,68,0.12)', color: AFFECTED }}>{e}</code>
                )) : <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>None</span>}
              </div>

              <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginBottom: 4, textTransform: 'uppercase' }}>
                Affected apps
              </div>
              {impact.primary_app && (
                <div style={{ marginBottom: 6 }}>
                  <span style={{ display: 'inline-block', padding: '2px 8px', borderRadius: 999, background: 'rgba(239,68,68,0.18)', color: AFFECTED, fontSize: '0.72rem', fontWeight: 600 }}>
                    {impact.primary_app} (changed)
                  </span>
                </div>
              )}
              {impact.cross_app_impact.map(c => (
                <div key={c.app} style={{ marginBottom: 8, padding: 8, borderRadius: 6, background: 'rgba(239,68,68,0.07)', border: '1px solid rgba(239,68,68,0.2)' }}>
                  <div style={{ color: AFFECTED, fontWeight: 600, fontSize: '0.8rem' }}>{c.app}</div>
                  <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)' }}>{c.reason}</div>
                  <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', fontFamily: 'monospace', marginTop: 3 }}>
                    {c.affected_files.join(', ')}
                  </div>
                </div>
              ))}

              <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', margin: '10px 0 4px', textTransform: 'uppercase' }}>
                Suites that will run
              </div>
              <div style={{ marginBottom: 12 }}>
                {impact.all_test_suites_to_run.map(s => (
                  <code key={s} style={{ display: 'inline-block', marginRight: 6, marginBottom: 4, padding: '2px 6px', borderRadius: 4, background: 'rgba(59,130,246,0.15)', color: 'var(--primary)', fontSize: '0.7rem' }}>{s}</code>
                ))}
                {!impact.all_test_suites_to_run.length && <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>None</span>}
              </div>

              {impact.queued_runs?.length ? (
                <div style={{ fontSize: '0.75rem', color: 'var(--success)', display: 'flex', alignItems: 'center', gap: 6 }}>
                  <CheckCircle2 size={13} /> {impact.queued_runs.filter(r => r.run_id).length} run(s) queued
                </div>
              ) : (
                <button
                  onClick={() => doAnalyze(true)}
                  disabled={running || !impact.all_test_suites_to_run.length}
                  style={{ width: '100%', padding: 9, borderRadius: 6, background: 'var(--danger)', color: '#fff', border: 'none', display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 8, cursor: running || !impact.all_test_suites_to_run.length ? 'not-allowed' : 'pointer', opacity: running || !impact.all_test_suites_to_run.length ? 0.5 : 1 }}>
                  {running ? <Loader2 size={14} className="spin" /> : <Play size={14} />} Run All Affected Tests
                </button>
              )}
            </div>
          )}

          {/* Node details */}
          <div className="card">
            <h3 style={{ margin: '0 0 10px', fontSize: '0.95rem', display: 'flex', alignItems: 'center', gap: 8 }}>
              <Info size={15} /> Details
            </h3>
            {!selected ? (
              <p style={{ color: 'var(--text-muted)', fontSize: '0.8rem', margin: 0 }}>
                Click an app to pin its endpoints; click an endpoint to pin the apps using it. Click again to unpin.
              </p>
            ) : selected.type === 'app' ? (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                  <span style={{ width: 11, height: 11, borderRadius: '50%', background: (selected as any).color }} />
                  <strong style={{ fontSize: '0.85rem' }}>{selected.id}</strong>
                </div>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>role: {selected.app_role ?? '—'}</div>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', marginBottom: 8 }}>suite: {selected.test_suite ?? '—'}</div>
                <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: 4 }}>Calls</div>
                <div style={{ maxHeight: 130, overflowY: 'auto' }}>
                  {(graph?.links ?? []).filter(l => idOf(l.source) === selected.id).map((l, i) => (
                    <div key={i} style={{ fontSize: '0.68rem', fontFamily: 'monospace', color: 'var(--text-secondary)' }}>{idOf(l.target)}</div>
                  ))}
                </div>
              </>
            ) : (
              <>
                <code style={{ fontSize: '0.75rem', color: 'var(--text-primary)', wordBreak: 'break-all' }}>{selected.id}</code>
                {(selected as any).shared && (
                  <div style={{ fontSize: '0.7rem', color: 'var(--warning)', margin: '6px 0' }}>
                    Shared — a change here can break multiple apps.
                  </div>
                )}
                <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', margin: '8px 0 4px' }}>Used by</div>
                {(selected.used_by ?? []).map(a => (
                  <div key={a} style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>{a}</div>
                ))}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
