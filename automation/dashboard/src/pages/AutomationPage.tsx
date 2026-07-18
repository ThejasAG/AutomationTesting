import { useEffect, useState, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { getDevices, getProjects, startRun, stopRun, getLiveStatus } from '../api';
import type { Device, Project } from '../api';
import { Play, Square, Smartphone, FolderTree, GitBranch, Activity, CheckCircle2, XCircle, FolderGit2 } from 'lucide-react';
import ScenarioPanel from '../components/ScenarioPanel';

export default function AutomationPage() {
    const navigate = useNavigate();
    const [devices, setDevices] = useState<Device[]>([]);
    const [projects, setProjects] = useState<Project[]>([]);
    const [selectedDevice, setSelectedDevice] = useState('');
    const [selectedProject, setSelectedProject] = useState('');
    
    const [activeRunId, setActiveRunId] = useState<string | null>(null);
    const [runStatus, setRunStatus] = useState<any>(null);
    const [polling, setPolling] = useState(false);
    
    const logsEndRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        let mounted = true;
        
        async function fetchDevices() {
            try {
                const res = await getDevices();
                if (mounted) {
                    setDevices(res.devices);
                    if (res.devices.length > 0 && !selectedDevice) {
                        setSelectedDevice(res.devices[0].id);
                    }
                }
            } catch (e) {
                console.error("Failed to load devices", e);
            }
        }
        
        async function fetchProjects() {
            try {
                const p = await getProjects();
                if (mounted) {
                    setProjects(p);
                    if (p.length > 0 && !selectedProject) {
                        setSelectedProject(p[0].id);
                    }
                }
            } catch (e) {
                console.error("Failed to load projects", e);
            }
        }
        
        fetchProjects();
        fetchDevices();
        const interval = setInterval(fetchDevices, 5000);
        
        return () => {
            mounted = false;
            clearInterval(interval);
        };
    }, [selectedDevice, selectedProject]);

    useEffect(() => {
        let interval: any;
        if (polling && activeRunId) {
            interval = setInterval(async () => {
                try {
                    const status = await getLiveStatus(activeRunId);
                    setRunStatus(status);
                    if (status.status === 'Passed' || status.status === 'Failed' || status.status === 'Stopped') {
                        setPolling(false);
                        // Refresh projects to update health status after run
                        const p = await getProjects();
                        setProjects(p);
                    }
                } catch (e) {
                    console.error("Failed to poll status");
                }
            }, 1000);
        }
        return () => clearInterval(interval);
    }, [polling, activeRunId]);

    useEffect(() => {
        logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [runStatus?.logs]);

    const handleRun = async () => {
        if (!selectedDevice || !selectedProject) return alert("Select device and project");
        try {
            const runId = await startRun(selectedProject, selectedDevice);
            setActiveRunId(runId);
            setRunStatus({ status: 'Started', logs: [], duration_ms: 0 });
            setPolling(true);
            // Jump straight to the Run Details page so the Live View panel opens
            // immediately (it renders "Waiting for stream…" until frames arrive).
            navigate(`/run/${runId}`);
        } catch (e: any) {
            alert(`Failed to start run: ${e.message}`);
        }
    };
    
    const handleStop = async () => {
        if (!activeRunId) return;
        try {
            await stopRun(activeRunId);
        } catch (e) {
            alert("Failed to stop run");
        }
    };
    
    return (
        <div className="animate-fade-in">
            <header className="page-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                    <h1 className="page-title">Test Orchestration</h1>
                    <p className="page-subtitle">Select a registered project and device, then execute.</p>
                </div>
                {/* Project registration/editing lives solely on the Projects page. */}
                <button
                    onClick={() => navigate('/projects')}
                    style={{ padding: '10px 16px', borderRadius: '8px', backgroundColor: 'rgba(255,255,255,0.08)', color: '#fff', border: 'none', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}
                >
                    <FolderGit2 size={18}/> Manage Projects
                </button>
            </header>

            <div className="grid-2">
                <div className="card">
                    <h2 style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px', fontSize: '1.25rem' }}><Smartphone size={20}/> Target Device</h2>
                    <select 
                        value={selectedDevice} 
                        onChange={e => setSelectedDevice(e.target.value)}
                        style={{ width: '100%', padding: '12px', borderRadius: '8px', backgroundColor: 'var(--bg-elevated)', color: 'var(--text-primary)', border: '1px solid rgba(255,255,255,0.1)', marginBottom: '16px' }}
                    >
                        {devices.map(d => (
                            <option key={d.id} value={d.id}>{d.name} ({d.platform}) - {d.status}</option>
                        ))}
                        {devices.length === 0 && <option value="">No connected devices found</option>}
                    </select>
                    
                    <h2 style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px', marginTop: '24px', fontSize: '1.25rem' }}><FolderTree size={20}/> Automation Projects</h2>
                    <div style={{ maxHeight: '400px', overflowY: 'auto', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px', padding: '12px', backgroundColor: 'rgba(0,0,0,0.2)' }}>
                        {projects.length === 0 ? (
                            <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: '40px' }}>
                                No automation projects registered. <br/>Register one on the <a href="/projects" style={{ color: 'var(--primary)' }}>Projects</a> page.
                            </div>
                        ) : (
                            projects.map(project => (
                                <div 
                                    key={project.id} 
                                    onClick={() => setSelectedProject(project.id)}
                                    style={{ 
                                        marginBottom: '12px', 
                                        padding: '12px', 
                                        borderRadius: '6px', 
                                        cursor: 'pointer',
                                        border: selectedProject === project.id ? '1px solid var(--primary)' : '1px solid rgba(255,255,255,0.05)',
                                        backgroundColor: selectedProject === project.id ? 'rgba(59, 130, 246, 0.1)' : 'var(--bg-elevated)'
                                    }}
                                >
                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                                        <div style={{ fontWeight: 'bold', color: selectedProject === project.id ? 'var(--primary)' : 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                                            <GitBranch size={16} /> {project.name}
                                        </div>
                                        <span className={`badge ${project.status === 'active' ? 'passed' : 'failed'}`}>{project.status}</span>
                                    </div>
                                    
                                    {/* Health chips must reflect the project's OWN toolchain. A React
                                        Native / native app has no Python venv, so showing a red "Venv"
                                        chip for it reports a failure that does not exist. */}
                                    <div style={{ display: 'flex', gap: '16px', fontSize: '0.8rem', color: 'var(--text-secondary)', flexWrap: 'wrap' }}>
                                        <span style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                                            {project.health.repository_exists ? <CheckCircle2 size={12} color="var(--success)"/> : <XCircle size={12} color="var(--danger)"/>}
                                            Repo
                                        </span>
                                        <span style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                                            {project.health.automation_yaml ? <CheckCircle2 size={12} color="var(--success)"/> : <XCircle size={12} color="var(--warning)"/>}
                                            YAML
                                        </span>
                                        {/* Venv is only meaningful for a Python project. */}
                                        {project.project_type === 'python' && (
                                            <span style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                                                {project.health.venv_exists ? <CheckCircle2 size={12} color="var(--success)"/> : <XCircle size={12} color="var(--danger)"/>}
                                                Venv
                                            </span>
                                        )}
                                        <span style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                                            {project.health.dependencies_ok ? <CheckCircle2 size={12} color="var(--success)"/> : <XCircle size={12} color="var(--warning)"/>}
                                            Deps
                                        </span>
                                        {project.project_type_label && project.project_type !== 'unknown' && (
                                            <span style={{ color: 'var(--text-muted)' }}>
                                                {project.project_type_label}
                                            </span>
                                        )}
                                    </div>
                                </div>
                            ))
                        )}
                    </div>
                    
                    <div style={{ marginTop: '24px', display: 'flex', gap: '12px' }}>
                        <button 
                            onClick={handleRun}
                            disabled={polling || !selectedDevice || !selectedProject}
                            style={{ flex: 1, padding: '12px', borderRadius: '8px', backgroundColor: 'var(--primary)', color: '#fff', border: 'none', display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', cursor: (polling || !selectedDevice || !selectedProject) ? 'not-allowed' : 'pointer', opacity: (polling || !selectedDevice || !selectedProject) ? 0.5 : 1 }}
                        >
                            <Play size={18}/> Execute
                        </button>
                        <button 
                            onClick={handleStop}
                            disabled={!polling}
                            style={{ flex: 1, padding: '12px', borderRadius: '8px', backgroundColor: 'var(--danger)', color: '#fff', border: 'none', display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', cursor: !polling ? 'not-allowed' : 'pointer', opacity: !polling ? 0.5 : 1 }}
                        >
                            <Square size={18}/> Stop
                        </button>
                    </div>
                </div>
                
                <div className="card" style={{ display: 'flex', flexDirection: 'column' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                        <h2 style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '1.25rem', margin: 0 }}><Activity size={20}/> Orchestration Logs</h2>
                        {runStatus && (
                            <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                                <span style={{ fontFamily: 'monospace', color: 'var(--text-secondary)' }}>
                                    {(runStatus.duration_ms / 1000).toFixed(1)}s
                                </span>
                                <span className={`badge ${runStatus.status.toLowerCase()}`}>
                                    {runStatus.status}
                                </span>
                            </div>
                        )}
                    </div>
                    
                    <div style={{ flex: 1, backgroundColor: '#0d1117', borderRadius: '8px', padding: '16px', fontFamily: 'monospace', fontSize: '0.875rem', color: '#c9d1d9', overflowY: 'auto', minHeight: '400px', border: '1px solid rgba(255,255,255,0.1)' }}>
                        {!runStatus ? (
                            <div style={{ color: 'var(--text-muted)', textAlign: 'center', marginTop: '150px' }}>
                                Select a project and device, then click Execute to orchestrate tests.
                            </div>
                        ) : (
                            runStatus.logs.map((log: string, i: number) => (
                                <div key={i} style={{ marginBottom: '4px', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{log}</div>
                            ))
                        )}
                        <div ref={logsEndRef} />
                    </div>
                </div>
            </div>

            {selectedProject && (
                <div style={{ marginTop: 24 }}>
                    <ScenarioPanel projectId={selectedProject} />
                </div>
            )}
        </div>
    );
}
