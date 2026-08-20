import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getProjects, getPerformanceTrends } from '../api';
import type { Project, PerformanceTrends } from '../api';
import { Gauge, AlertTriangle, TrendingUp, TrendingDown, Loader2 } from 'lucide-react';
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Cell,
} from 'recharts';

function gradeColor(grade: string | null | undefined): string {
  switch ((grade || '').toUpperCase()) {
    case 'A': return '#22c55e';
    case 'B': return '#3b82f6';
    case 'C': return '#eab308';
    case 'D': return '#f97316';
    default:  return '#ef4444';
  }
}

export default function PerformancePage() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string>('');
  const [trends, setTrends] = useState<PerformanceTrends | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getProjects().then(ps => {
      setProjects(ps);
      if (ps.length) setProjectId(ps[0].id);
    }).catch(() => {}).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!projectId) return;
    setTrends(null);
    getPerformanceTrends(projectId, 10).then(setTrends).catch(() => setTrends(null));
  }, [projectId]);

  const chartData = (trends?.trend || []).map((t, i) => ({
    idx: i + 1,
    run_id: t.run_id,
    score: t.score ?? 0,
    grade: t.grade,
    date: t.date ? new Date(t.date).toLocaleDateString() : `#${i + 1}`,
  }));

  return (
    <div className="animate-fade-in">
      <header className="page-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h1 className="page-title" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Gauge size={26} color="var(--accent-primary)" /> Performance
          </h1>
          <p className="page-subtitle">Performance score trends and regressions across runs.</p>
        </div>
        {projects.length > 0 && (
          <select value={projectId} onChange={e => setProjectId(e.target.value)}
            style={{ padding: '8px 12px', borderRadius: 8, background: 'var(--bg-secondary)', color: 'var(--text-primary)', border: '1px solid var(--border-color)' }}>
            {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        )}
      </header>

      {loading ? (
        <div className="card" style={{ display: 'flex', gap: 10, alignItems: 'center' }}><Loader2 size={16} className="spin" /> Loading…</div>
      ) : !trends || trends.trend.length === 0 ? (
        <div className="card" style={{ color: 'var(--text-secondary)' }}>
          No performance data yet for this project. Runs must be executed with the performance collector enabled.
        </div>
      ) : (
        <>
          {/* Regression alert */}
          {trends.regression && (
            <div className="card" style={{ marginBottom: 20, borderLeft: '5px solid #ef4444', background: 'rgba(239,68,68,0.06)', display: 'flex', alignItems: 'center', gap: 12 }}>
              <AlertTriangle size={22} color="#ef4444" />
              <div>
                <strong style={{ color: '#ef4444' }}>Performance Regression</strong>
                <div style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
                  Score dropped {trends.regression.drop} points ({trends.regression.from_score} → {trends.regression.to_score}) on the latest run.
                </div>
              </div>
            </div>
          )}

          {/* Summary KPIs */}
          <div className="grid-3" style={{ marginBottom: 24 }}>
            <div className="card">
              <div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Average Score</div>
              <div className="stat-value">{trends.avg_score}<span style={{ fontSize: '1rem', color: 'var(--text-secondary)' }}>/100</span></div>
            </div>
            <div className="card">
              <div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Trend</div>
              <div className="stat-value" style={{ display: 'flex', alignItems: 'center', gap: 8, color: trends.improving ? '#22c55e' : '#ef4444' }}>
                {trends.improving ? <TrendingUp size={26} /> : <TrendingDown size={26} />}
                {trends.improving ? 'Improving' : 'Declining'}
              </div>
            </div>
            <div className="card">
              <div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Runs Analyzed</div>
              <div className="stat-value">{trends.trend.length}</div>
            </div>
          </div>

          {/* Score over time */}
          <div className="card" style={{ marginBottom: 24 }}>
            <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem' }}>Performance Score Over Time</h3>
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border-color)" />
                <XAxis dataKey="date" stroke="var(--text-secondary)" fontSize={11} />
                <YAxis domain={[0, 100]} stroke="var(--text-secondary)" fontSize={11} />
                <Tooltip contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--border-color)' }} />
                <Line type="monotone" dataKey="score" stroke="var(--accent-primary)" strokeWidth={2}
                  dot={{ r: 4 }} activeDot={{ r: 6 }} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Per-run score bars (click to open run) */}
          <div className="card">
            <h3 style={{ margin: '0 0 12px', fontSize: '0.95rem' }}>Score by Run (click a bar to open)</h3>
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={chartData} onClick={(e: any) => { const p = e?.activePayload?.[0]?.payload; if (p?.run_id) navigate(`/run/${p.run_id}`); }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border-color)" />
                <XAxis dataKey="date" stroke="var(--text-secondary)" fontSize={11} />
                <YAxis domain={[0, 100]} stroke="var(--text-secondary)" fontSize={11} />
                <Tooltip contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--border-color)' }} />
                <Bar dataKey="score" radius={[4, 4, 0, 0]} cursor="pointer">
                  {chartData.map((d, i) => <Cell key={i} fill={gradeColor(d.grade)} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </div>
  );
}
