import { useEffect, useState } from 'react';
import { getTrends } from '../api';
import type { Trends } from '../api';
import { BarChart3, AlertTriangle, TrendingDown } from 'lucide-react';

export default function AnalyticsPage() {
    const [trends, setTrends] = useState<Trends | null>(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        async function load() {
            try {
                const t = await getTrends();
                setTrends(t);
            } catch (e) {
                console.error(e);
            }
            setLoading(false);
        }
        load();
    }, []);

    if (loading) return <div className="page-header"><h1 className="page-title animate-fade-in">Loading Analytics...</h1></div>;
    if (!trends) return <div className="page-header"><h1 className="page-title">Failed to load analytics</h1></div>;

    return (
        <div className="animate-fade-in">
            <header className="page-header">
                <h1 className="page-title">Enterprise Analytics</h1>
                <p className="page-subtitle">Track flakiness, failure trends, and overall test health.</p>
            </header>

            <div className="dashboard-grid">
                {/* Flaky Tests */}
                <div className="card">
                    <div className="card-header">
                        <AlertTriangle className="icon" color="var(--warning)" />
                        <h2 className="card-title">Top Flaky Tests</h2>
                    </div>
                    {trends.flaky_tests.length === 0 ? (
                        <p style={{ color: 'var(--text-secondary)' }}>No flaky tests detected recently.</p>
                    ) : (
                        <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: '12px' }}>
                            {trends.flaky_tests.map((ft, idx) => (
                                <li key={idx} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px', background: 'rgba(255,255,255,0.02)', borderRadius: '8px' }}>
                                    <span style={{ fontWeight: 500 }}>{ft.test_name}</span>
                                    <span style={{ background: 'rgba(255,171,0,0.1)', color: 'var(--warning)', padding: '4px 8px', borderRadius: '4px', fontSize: '0.85rem' }}>
                                        {ft.fail_count} failures
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>

                {/* Common Root Causes */}
                <div className="card">
                    <div className="card-header">
                        <BarChart3 className="icon" color="var(--primary)" />
                        <h2 className="card-title">Failure Categories</h2>
                    </div>
                    {trends.common_root_causes.length === 0 ? (
                        <p style={{ color: 'var(--text-secondary)' }}>No failure data available.</p>
                    ) : (
                        <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: '12px' }}>
                            {trends.common_root_causes.map((rc, idx) => (
                                <li key={idx} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px', background: 'rgba(255,255,255,0.02)', borderRadius: '8px' }}>
                                    <span style={{ fontWeight: 500 }}>{rc.failure_category || 'Unknown'}</span>
                                    <span style={{ background: 'rgba(59,130,246,0.1)', color: 'var(--primary)', padding: '4px 8px', borderRadius: '4px', fontSize: '0.85rem' }}>
                                        {rc.count} occurrences
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
                
                {/* Overall Health */}
                <div className="card" style={{ gridColumn: '1 / -1' }}>
                    <div className="card-header">
                        <TrendingDown className="icon" color={trends.failure_rate > 20 ? 'var(--danger)' : 'var(--success)'} />
                        <h2 className="card-title">Platform Health</h2>
                    </div>
                    <div style={{ display: 'flex', gap: '48px', marginTop: '16px' }}>
                        <div>
                            <div style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginBottom: '8px' }}>Failure Rate</div>
                            <div style={{ fontSize: '2rem', fontWeight: '700', color: trends.failure_rate > 20 ? 'var(--danger)' : 'var(--text-primary)' }}>
                                {trends.failure_rate.toFixed(1)}%
                            </div>
                        </div>
                        <div>
                            <div style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginBottom: '8px' }}>Total Executions</div>
                            <div style={{ fontSize: '2rem', fontWeight: '700' }}>
                                {trends.total_executions}
                            </div>
                        </div>
                        <div>
                            <div style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginBottom: '8px' }}>Failed Executions</div>
                            <div style={{ fontSize: '2rem', fontWeight: '700', color: 'var(--danger)' }}>
                                {trends.failed_executions}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
