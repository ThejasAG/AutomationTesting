import { useEffect, useState } from 'react';
import { getOpsMetrics, getOpsAlerts, getFullHealth } from '../api';
import { Activity, Server, AlertTriangle, ShieldCheck, Cpu, CheckCircle2, XCircle, RefreshCw, Heart } from 'lucide-react';

export default function OperationsDashboard() {
    const [metrics, setMetrics] = useState<any>(null);
    const [alerts, setAlerts] = useState<any[]>([]);
    const [health, setHealth] = useState<any>(null);
    const [activeTab, setActiveTab] = useState<'overview' | 'health'>('overview');
    const [healthLoading, setHealthLoading] = useState(false);

    useEffect(() => {
        const load = async () => {
            try {
                const m = await getOpsMetrics();
                setMetrics(m);
                const a = await getOpsAlerts();
                setAlerts(a.alerts);
            } catch (e) {
                console.error(e);
            }
        };
        load();
        const interval = setInterval(load, 10000);
        return () => clearInterval(interval);
    }, []);

    const runHealthCheck = async () => {
        setHealthLoading(true);
        try {
            const h = await getFullHealth();
            setHealth(h);
        } catch (e) {
            console.error(e);
        } finally {
            setHealthLoading(false);
        }
    };

    const tabStyle = (tab: string) => ({
        padding: '10px 20px',
        borderRadius: '8px 8px 0 0',
        cursor: 'pointer',
        border: 'none',
        fontWeight: 600,
        background: activeTab === tab ? 'rgba(59,130,246,0.15)' : 'transparent',
        color: activeTab === tab ? '#60a5fa' : '#94a3b8',
        borderBottom: activeTab === tab ? '2px solid #60a5fa' : '2px solid transparent',
    });

    return (
        <div className="animate-fade-in" style={{ padding: '8px' }}>
            <header className="page-header">
                <h1 className="page-title">Operations Dashboard</h1>
                <p className="page-subtitle">Global platform health, agent fleet, and execution metrics.</p>
            </header>

            {/* Tabs */}
            <div style={{ display: 'flex', gap: '4px', marginBottom: '24px', borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
                <button style={tabStyle('overview')} onClick={() => setActiveTab('overview')}>
                    <Activity size={16} style={{ display: 'inline', marginRight: '6px' }} />
                    Overview
                </button>
                <button style={tabStyle('health')} onClick={() => { setActiveTab('health'); if (!health) runHealthCheck(); }}>
                    <Heart size={16} style={{ display: 'inline', marginRight: '6px' }} />
                    Health Center
                </button>
            </div>

            {activeTab === 'overview' && (
                <>
                    {!metrics ? (
                        <div style={{ textAlign: 'center', padding: '60px', color: 'var(--text-muted)' }}>Loading operations data...</div>
                    ) : (
                        <>
                            <div className="grid-4" style={{ marginBottom: '24px' }}>
                                <div className="card">
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                                        <ShieldCheck size={18} color={metrics.platform_health === 'Optimal' ? 'var(--success)' : 'var(--warning)'} /> Health
                                    </div>
                                    <div className="stat-value" style={{ fontSize: '1.75rem' }}>{metrics.platform_health}</div>
                                </div>
                                <div className="card">
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                                        <Server size={18} color="var(--primary)" /> Agents
                                    </div>
                                    <div className="stat-value">{metrics.active_agents} / {metrics.total_agents}</div>
                                    <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem', marginTop: '6px' }}>Active nodes</div>
                                </div>
                                <div className="card">
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                                        <Activity size={18} color="var(--primary)" /> Jobs
                                    </div>
                                    <div className="stat-value">{metrics.running_jobs}</div>
                                    <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem', marginTop: '6px' }}>{metrics.queued_jobs} queued</div>
                                </div>
                                <div className="card">
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                                        <Cpu size={18} color="var(--success)" /> Pass Rate
                                    </div>
                                    <div className="stat-value">{metrics.pass_rate}%</div>
                                    <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem', marginTop: '6px' }}>{metrics.total_executions} executions</div>
                                </div>
                            </div>

                            <h2 style={{ marginBottom: '16px', fontSize: '1.1rem', display: 'flex', alignItems: 'center', gap: '8px' }}>
                                <AlertTriangle size={18} color="var(--danger)" /> Active System Alerts
                            </h2>
                            {alerts.length === 0 ? (
                                <div className="card" style={{ backgroundColor: 'rgba(34,197,94,0.05)', border: '1px solid rgba(34,197,94,0.2)', color: 'var(--success)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                                    <CheckCircle2 size={18} /> No active system alerts. Platform is operating normally.
                                </div>
                            ) : (
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                                    {alerts.map((a: any) => (
                                        <div key={a.id} className="card" style={{
                                            border: `1px solid ${a.severity === 'critical' ? 'rgba(239,68,68,0.3)' : 'rgba(234,179,8,0.3)'}`,
                                            backgroundColor: a.severity === 'critical' ? 'rgba(239,68,68,0.07)' : 'rgba(234,179,8,0.07)'
                                        }}>
                                            <div style={{ fontWeight: 'bold', display: 'flex', alignItems: 'center', gap: '8px', color: a.severity === 'critical' ? 'var(--danger)' : 'var(--warning)' }}>
                                                <AlertTriangle size={16} /> {a.type?.toUpperCase().replace('_', ' ')}
                                            </div>
                                            <p style={{ marginTop: '6px', color: 'var(--text-secondary)' }}>{a.message}</p>
                                            <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '6px' }}>{new Date(a.created_at).toLocaleString()}</p>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </>
                    )}
                </>
            )}

            {activeTab === 'health' && (
                <div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
                        <div>
                            <h2 style={{ fontSize: '1.25rem', marginBottom: '4px' }}>Platform Health Center</h2>
                            <p style={{ color: 'var(--text-muted)', fontSize: '0.875rem' }}>Comprehensive check of all platform components</p>
                        </div>
                        <button
                            onClick={runHealthCheck}
                            disabled={healthLoading}
                            style={{ padding: '10px 16px', borderRadius: '8px', backgroundColor: 'var(--primary)', color: '#fff', border: 'none', display: 'flex', alignItems: 'center', gap: '8px', cursor: healthLoading ? 'wait' : 'pointer' }}
                        >
                            <RefreshCw size={16} style={{ animation: healthLoading ? 'spin 1s linear infinite' : 'none' }} />
                            {healthLoading ? 'Checking...' : 'Run Health Check'}
                        </button>
                    </div>

                    {health && (
                        <>
                            {/* Score */}
                            <div className="card" style={{
                                marginBottom: '24px',
                                textAlign: 'center',
                                background: health.health_score >= 80
                                    ? 'linear-gradient(135deg, rgba(34,197,94,0.1), rgba(59,130,246,0.05))'
                                    : health.health_score >= 50
                                    ? 'linear-gradient(135deg, rgba(234,179,8,0.1), rgba(59,130,246,0.05))'
                                    : 'linear-gradient(135deg, rgba(239,68,68,0.1), rgba(59,130,246,0.05))',
                                border: `1px solid ${health.health_score >= 80 ? 'rgba(34,197,94,0.2)' : health.health_score >= 50 ? 'rgba(234,179,8,0.2)' : 'rgba(239,68,68,0.2)'}`
                            }}>
                                <div style={{ fontSize: '3.5rem', fontWeight: 'bold', color: health.health_score >= 80 ? 'var(--success)' : health.health_score >= 50 ? 'var(--warning)' : 'var(--danger)' }}>
                                    {health.health_score}%
                                </div>
                                <div style={{ fontSize: '1.25rem', fontWeight: 600, marginTop: '8px', color: 'var(--text-primary)' }}>{health.verdict}</div>
                                <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '4px' }}>
                                    Last checked: {new Date(health.timestamp).toLocaleString()}
                                </div>
                            </div>

                            {/* Components */}
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '12px' }}>
                                {health.components.map((c: any) => (
                                    <div key={c.name} className="card" style={{
                                        border: `1px solid ${c.status === 'ok' ? 'rgba(34,197,94,0.2)' : 'rgba(239,68,68,0.2)'}`,
                                        backgroundColor: c.status === 'ok' ? 'rgba(34,197,94,0.04)' : 'rgba(239,68,68,0.04)'
                                    }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px' }}>
                                            {c.status === 'ok'
                                                ? <CheckCircle2 size={18} color="var(--success)" />
                                                : <XCircle size={18} color="var(--danger)" />}
                                            <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{c.name}</span>
                                        </div>
                                        <div style={{ fontSize: '0.8rem', color: c.status === 'ok' ? 'var(--text-secondary)' : 'var(--danger)', fontFamily: 'monospace', wordBreak: 'break-word' }}>
                                            {c.detail}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </>
                    )}

                    {!health && !healthLoading && (
                        <div style={{ textAlign: 'center', padding: '60px', color: 'var(--text-muted)' }}>
                            Click "Run Health Check" to evaluate all platform components.
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
