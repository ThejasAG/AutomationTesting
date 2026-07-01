import { useEffect, useState } from 'react';
import { getRecommendationsList, updateRecommendationStatus } from '../api';
import type { AIRecommendation } from '../api';
import { BrainCircuit, CheckCircle, XCircle, AlertTriangle, FileWarning, PlayCircle } from 'lucide-react';

export default function CommandCenter() {
    const [recommendations, setRecommendations] = useState<AIRecommendation[]>([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        loadRecommendations();
    }, []);

    const loadRecommendations = async () => {
        setLoading(true);
        try {
            const data = await getRecommendationsList("pending");
            setRecommendations(data);
        } catch (e) {
            console.error(e);
        }
        setLoading(false);
    };

    const handleAction = async (id: string, action: string) => {
        await updateRecommendationStatus(id, action);
        loadRecommendations();
    };

    const getIcon = (type: string) => {
        if (type === 'test_plan') return <PlayCircle className="text-blue-400" />;
        if (type === 'self_heal') return <AlertTriangle className="text-yellow-400" />;
        if (type === 'bug_report') return <FileWarning className="text-red-400" />;
        return <BrainCircuit />;
    };

    return (
        <div className="p-8 max-w-7xl mx-auto space-y-8 animate-fade-in">
            <header className="flex justify-between items-center mb-8">
                <div>
                    <h1 className="text-3xl font-bold bg-gradient-to-r from-indigo-400 to-purple-400 bg-clip-text text-transparent flex items-center gap-3">
                        <BrainCircuit size={32} className="text-indigo-400" />
                        AI Command Center
                    </h1>
                    <p className="text-slate-400 mt-2">Approve, reject, or modify AI engineering recommendations.</p>
                </div>
            </header>

            {loading ? (
                <div className="flex justify-center p-12"><div className="animate-spin rounded-full h-12 w-12 border-b-2 border-indigo-500"></div></div>
            ) : recommendations.length === 0 ? (
                <div className="bg-slate-800/50 rounded-2xl border border-slate-700/50 p-12 text-center text-slate-400 backdrop-blur-xl">
                    <BrainCircuit size={48} className="mx-auto mb-4 opacity-50" />
                    <p>No pending AI recommendations.</p>
                </div>
            ) : (
                <div className="grid gap-6">
                    {recommendations.map(rec => (
                        <div key={rec.id} className="bg-slate-800/50 rounded-2xl border border-slate-700/50 p-6 backdrop-blur-xl hover:border-indigo-500/30 transition-colors shadow-lg">
                            <div className="flex items-start justify-between">
                                <div className="flex gap-4">
                                    <div className="p-3 bg-slate-900/50 rounded-xl">
                                        {getIcon(rec.type)}
                                    </div>
                                    <div>
                                        <h3 className="text-xl font-semibold text-white capitalize">{rec.type.replace('_', ' ')}</h3>
                                        <p className="text-slate-400 text-sm mt-1">Project: {rec.project_id}</p>
                                        
                                        <div className="mt-4 bg-slate-900/50 rounded-lg p-4 font-mono text-sm text-indigo-300">
                                            <pre className="whitespace-pre-wrap">{JSON.stringify(rec.payload, null, 2)}</pre>
                                        </div>
                                    </div>
                                </div>
                                <div className="flex gap-3">
                                    <button 
                                        onClick={() => handleAction(rec.id, 'accepted')}
                                        className="px-4 py-2 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 rounded-lg flex items-center gap-2 transition-colors border border-emerald-500/20">
                                        <CheckCircle size={18} /> Accept
                                    </button>
                                    <button 
                                        onClick={() => handleAction(rec.id, 'rejected')}
                                        className="px-4 py-2 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg flex items-center gap-2 transition-colors border border-red-500/20">
                                        <XCircle size={18} /> Reject
                                    </button>
                                </div>
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
