import { useEffect, useState } from 'react';
import { getProjects, getRecommendations, getIntelligenceMetrics, askChat } from '../api';
import type { Project } from '../api';
import { BrainCircuit, MessageSquare, ShieldCheck, Terminal } from 'lucide-react';

export default function InsightsPage() {
    const [projects, setProjects] = useState<Project[]>([]);
    const [selectedProject, setSelectedProject] = useState('');
    const [recommendations, setRecommendations] = useState<any>(null);
    const [metrics, setMetrics] = useState<any>(null);
    
    const [chatInput, setChatInput] = useState('');
    const [chatHistory, setChatHistory] = useState<{role: string, text: string}[]>([]);
    const [isChatLoading, setIsChatLoading] = useState(false);
    
    useEffect(() => {
        getProjects().then(p => {
            setProjects(p);
            if (p.length > 0) setSelectedProject(p[0].id);
        });
        getIntelligenceMetrics().then(m => setMetrics(m));
    }, []);
    
    useEffect(() => {
        if (selectedProject) {
            getRecommendations(selectedProject).then(r => setRecommendations(r));
        }
    }, [selectedProject]);
    
    const handleChatSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!chatInput.trim()) return;
        
        const userMsg = chatInput;
        setChatHistory(prev => [...prev, {role: 'user', text: userMsg}]);
        setChatInput('');
        setIsChatLoading(true);
        
        try {
            const res = await askChat(userMsg, { project_id: selectedProject, recommendations });
            setChatHistory(prev => [...prev, {role: 'assistant', text: res.reply}]);
        } catch (e: any) {
            setChatHistory(prev => [...prev, {role: 'assistant', text: "Error: " + e.message}]);
        } finally {
            setIsChatLoading(false);
        }
    };
    
    return (
        <div className="animate-fade-in" style={{ height: 'calc(100vh - 120px)', display: 'flex', flexDirection: 'column' }}>
            <header className="page-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
                <div>
                    <h1 className="page-title" style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                        <BrainCircuit size={28} color="var(--primary)" /> 
                        AI Test Intelligence
                    </h1>
                    <p className="page-subtitle">Proactive execution recommendations and conversational knowledge base.</p>
                </div>
                {metrics && (
                    <div style={{ display: 'flex', gap: '24px', backgroundColor: 'var(--bg-elevated)', padding: '12px 24px', borderRadius: '12px', border: '1px solid rgba(255,255,255,0.05)' }}>
                        <div>
                            <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>Global Pass Rate</div>
                            <div style={{ fontSize: '1.2rem', fontWeight: 'bold', color: metrics.pass_rate > 80 ? 'var(--success)' : 'var(--warning)' }}>
                                {metrics.pass_rate}%
                            </div>
                        </div>
                        <div>
                            <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>Total Executions</div>
                            <div style={{ fontSize: '1.2rem', fontWeight: 'bold' }}>{metrics.total_runs}</div>
                        </div>
                    </div>
                )}
            </header>
            
            <div className="grid-2" style={{ flex: 1, overflow: 'hidden' }}>
                {/* Left side: Recommendations & Flakiness */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: '24px', overflowY: 'auto', paddingRight: '8px' }}>
                    
                    <div className="card">
                        <div style={{ marginBottom: '16px' }}>
                            <label style={{ display: 'block', marginBottom: '8px', color: 'var(--text-secondary)', fontWeight: 500 }}>Target Project for Impact Analysis</label>
                            <select 
                                value={selectedProject} 
                                onChange={e => setSelectedProject(e.target.value)}
                                style={{ width: '100%', padding: '12px', borderRadius: '8px', backgroundColor: 'var(--bg-elevated)', color: 'var(--text-primary)', border: '1px solid rgba(255,255,255,0.1)' }}
                            >
                                {projects.map(d => (
                                    <option key={d.id} value={d.id}>{d.name} ({d.git_url})</option>
                                ))}
                            </select>
                        </div>
                    </div>
                    
                    {recommendations ? (
                        <div className="card" style={{ border: '1px solid rgba(139, 92, 246, 0.3)', background: 'linear-gradient(180deg, rgba(139, 92, 246, 0.05) 0%, rgba(0,0,0,0) 100%)' }}>
                            <h2 style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#a78bfa', marginBottom: '16px', fontSize: '1.25rem' }}>
                                <ShieldCheck size={20} /> Smart Execution Plan
                            </h2>
                            
                            <div style={{ backgroundColor: 'rgba(0,0,0,0.3)', padding: '16px', borderRadius: '8px', marginBottom: '16px', borderLeft: '4px solid #a78bfa' }}>
                                <div style={{ fontWeight: 'bold', marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                                    <BrainCircuit size={16} /> AI Rationale
                                </div>
                                <p style={{ color: 'var(--text-secondary)', margin: 0, lineHeight: 1.5 }}>
                                    {recommendations.explanation}
                                </p>
                            </div>
                            
                            <div style={{ display: 'flex', gap: '24px' }}>
                                <div style={{ flex: 1 }}>
                                    <h3 style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', marginBottom: '8px', textTransform: 'uppercase' }}>Affected Modules</h3>
                                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                                        {recommendations.affected_modules.length > 0 ? (
                                            recommendations.affected_modules.map((m: string) => (
                                                <span key={m} style={{ padding: '4px 12px', backgroundColor: 'rgba(245, 158, 11, 0.1)', color: '#fcd34d', borderRadius: '100px', fontSize: '0.85rem', border: '1px solid rgba(245, 158, 11, 0.2)' }}>
                                                    {m}
                                                </span>
                                            ))
                                        ) : (
                                            <span style={{ color: 'var(--text-muted)' }}>None detected</span>
                                        )}
                                    </div>
                                </div>
                                <div style={{ flex: 1 }}>
                                    <h3 style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', marginBottom: '8px', textTransform: 'uppercase' }}>Recommended Suites</h3>
                                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                                        {recommendations.recommended_tests.length > 0 ? (
                                            recommendations.recommended_tests.map((t: string) => (
                                                <span key={t} style={{ padding: '4px 12px', backgroundColor: 'rgba(16, 185, 129, 0.1)', color: '#6ee7b7', borderRadius: '100px', fontSize: '0.85rem', border: '1px solid rgba(16, 185, 129, 0.2)' }}>
                                                    {t}
                                                </span>
                                            ))
                                        ) : (
                                            <span style={{ color: 'var(--text-muted)' }}>None</span>
                                        )}
                                    </div>
                                </div>
                            </div>
                            
                            <button style={{ width: '100%', marginTop: '24px', padding: '12px', borderRadius: '8px', backgroundColor: 'var(--primary)', color: '#fff', border: 'none', display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', cursor: 'pointer', fontWeight: 500 }}>
                                <Terminal size={18} /> Execute Recommended Plan
                            </button>
                        </div>
                    ) : (
                        <div className="card" style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '300px', color: 'var(--text-muted)' }}>
                            Analyzing repository state...
                        </div>
                    )}
                </div>
                
                {/* Right side: AI Chat */}
                <div className="card" style={{ display: 'flex', flexDirection: 'column', padding: 0, overflow: 'hidden' }}>
                    <div style={{ padding: '20px', borderBottom: '1px solid rgba(255,255,255,0.05)', display: 'flex', alignItems: 'center', gap: '12px', backgroundColor: 'rgba(0,0,0,0.2)' }}>
                        <MessageSquare size={20} color="var(--primary)" />
                        <h2 style={{ fontSize: '1.1rem', margin: 0 }}>Intelligence Assistant</h2>
                    </div>
                    
                    <div style={{ flex: 1, overflowY: 'auto', padding: '20px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
                        {chatHistory.length === 0 && (
                            <div style={{ margin: 'auto', textAlign: 'center', color: 'var(--text-muted)', maxWidth: '300px' }}>
                                <BrainCircuit size={48} style={{ margin: '0 auto 16px', opacity: 0.2 }} />
                                Ask me about flaky tests, historical failures, or why specific modules are unstable.
                            </div>
                        )}
                        {chatHistory.map((msg, i) => (
                            <div key={i} style={{ 
                                alignSelf: msg.role === 'user' ? 'flex-end' : 'flex-start',
                                backgroundColor: msg.role === 'user' ? 'var(--primary)' : 'rgba(255,255,255,0.05)',
                                color: '#fff',
                                padding: '12px 16px',
                                borderRadius: '12px',
                                borderBottomRightRadius: msg.role === 'user' ? '4px' : '12px',
                                borderBottomLeftRadius: msg.role === 'assistant' ? '4px' : '12px',
                                maxWidth: '80%',
                                lineHeight: 1.5,
                                border: msg.role === 'assistant' ? '1px solid rgba(255,255,255,0.1)' : 'none'
                            }}>
                                {msg.text}
                            </div>
                        ))}
                        {isChatLoading && (
                            <div style={{ alignSelf: 'flex-start', color: 'var(--text-muted)', fontSize: '0.9rem', fontStyle: 'italic' }}>
                                Analyzing knowledge base...
                            </div>
                        )}
                    </div>
                    
                    <form onSubmit={handleChatSubmit} style={{ padding: '16px', borderTop: '1px solid rgba(255,255,255,0.05)', backgroundColor: 'rgba(0,0,0,0.2)' }}>
                        <div style={{ display: 'flex', gap: '12px' }}>
                            <input 
                                type="text" 
                                value={chatInput}
                                onChange={e => setChatInput(e.target.value)}
                                placeholder="E.g., Why did the Login suite fail yesterday?" 
                                style={{ flex: 1, padding: '12px', borderRadius: '8px', backgroundColor: '#0d1117', border: '1px solid rgba(255,255,255,0.1)', color: '#fff' }}
                                disabled={isChatLoading}
                            />
                            <button 
                                type="submit" 
                                disabled={isChatLoading || !chatInput.trim()}
                                style={{ padding: '0 24px', borderRadius: '8px', backgroundColor: 'var(--primary)', color: '#fff', border: 'none', cursor: (isChatLoading || !chatInput.trim()) ? 'not-allowed' : 'pointer', opacity: (isChatLoading || !chatInput.trim()) ? 0.5 : 1 }}
                            >
                                Send
                            </button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    );
}
