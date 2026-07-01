import { useEffect, useState } from 'react';
import { getRuns } from '../api';
import type { TestRun } from '../api';
import { GitBranch, GitCommit, Play, Hash } from 'lucide-react';
import { formatDistanceToNow, parseISO } from 'date-fns';

export default function CIPage() {
    const [pipelineRuns, setPipelineRuns] = useState<TestRun[]>([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        async function load() {
            try {
                const allRuns = await getRuns();
                // Filter only runs triggered by a CI/CD system
                const ciRuns = allRuns.filter(r => r.triggered_by);
                setPipelineRuns(ciRuns);
            } catch (e) {
                console.error(e);
            }
            setLoading(false);
        }
        load();
    }, []);

    if (loading) return <div className="page-header"><h1 className="page-title animate-fade-in">Loading CI/CD Pipelines...</h1></div>;

    return (
        <div className="animate-fade-in">
            <header className="page-header">
                <h1 className="page-title">CI/CD Pipelines</h1>
                <p className="page-subtitle">Track automated executions triggered by GitHub Actions and Jenkins.</p>
            </header>

            <div className="card">
                <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
                    <thead>
                        <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
                            <th style={{ padding: '12px 8px', color: 'var(--text-secondary)' }}>Status</th>
                            <th style={{ padding: '12px 8px', color: 'var(--text-secondary)' }}>Triggered By</th>
                            <th style={{ padding: '12px 8px', color: 'var(--text-secondary)' }}>Build</th>
                            <th style={{ padding: '12px 8px', color: 'var(--text-secondary)' }}>Branch</th>
                            <th style={{ padding: '12px 8px', color: 'var(--text-secondary)' }}>Commit</th>
                            <th style={{ padding: '12px 8px', color: 'var(--text-secondary)' }}>Time</th>
                        </tr>
                    </thead>
                    <tbody>
                        {pipelineRuns.length === 0 ? (
                            <tr>
                                <td colSpan={6} style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)' }}>
                                    No pipeline executions found. Trigger a run from GitHub Actions or Jenkins!
                                </td>
                            </tr>
                        ) : (
                            pipelineRuns.map(run => (
                                <tr key={run.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                                    <td style={{ padding: '12px 8px' }}>
                                        <span className={`badge ${run.status}`}>{run.status}</span>
                                    </td>
                                    <td style={{ padding: '12px 8px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                                        <Play size={16} color="var(--primary)" /> {run.triggered_by}
                                    </td>
                                    <td style={{ padding: '12px 8px' }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                                            <Hash size={14} color="var(--text-secondary)" /> {run.build_number}
                                        </div>
                                    </td>
                                    <td style={{ padding: '12px 8px' }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                                            <GitBranch size={14} color="var(--text-secondary)" /> {run.branch || 'main'}
                                        </div>
                                    </td>
                                    <td style={{ padding: '12px 8px' }}>
                                        <div style={{ display: 'flex', alignItems: 'center', gap: '4px', fontFamily: 'monospace' }}>
                                            <GitCommit size={14} color="var(--text-secondary)" /> {run.commit_sha ? run.commit_sha.substring(0, 7) : 'N/A'}
                                        </div>
                                    </td>
                                    <td style={{ padding: '12px 8px', color: 'var(--text-secondary)', fontSize: '0.875rem' }}>
                                        {formatDistanceToNow(parseISO(run.started_at))} ago
                                    </td>
                                </tr>
                            ))
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
