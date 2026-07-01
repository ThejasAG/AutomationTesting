import { useEffect, useState } from 'react';
import { getOpsAgents, drainOpsAgent } from '../api';
import { Server, Smartphone, Cpu, Power, PowerOff } from 'lucide-react';

export default function DeviceOpsCenter() {
    const [agents, setAgents] = useState<any[]>([]);

    const loadAgents = () => {
        getOpsAgents().then(data => setAgents(data.agents)).catch(console.error);
    };

    useEffect(() => {
        loadAgents();
        const interval = setInterval(loadAgents, 10000);
        return () => clearInterval(interval);
    }, []);

    const handleDrain = async (id: string) => {
        await drainOpsAgent(id);
        loadAgents();
    };

    return (
        <div className="p-8 max-w-7xl mx-auto space-y-8 animate-fade-in">
            <header className="mb-8">
                <h1 className="text-3xl font-bold bg-gradient-to-r from-blue-400 to-indigo-400 bg-clip-text text-transparent flex items-center gap-3">
                    <Smartphone size={32} className="text-blue-400" />
                    Device Ops Center
                </h1>
                <p className="text-slate-400 mt-2">Manage execution agents and view connected device fleets.</p>
            </header>

            <div className="space-y-6">
                {agents.length === 0 ? (
                    <div className="p-12 text-center text-slate-400">No agents registered in the fleet.</div>
                ) : agents.map(agent => (
                    <div key={agent.id} className="bg-slate-800/50 rounded-2xl border border-slate-700/50 overflow-hidden">
                        <div className="p-6 border-b border-slate-700/50 flex justify-between items-center bg-slate-900/30">
                            <div className="flex items-center gap-4">
                                <Server className={agent.status === 'offline' ? 'text-slate-500' : 'text-emerald-400'} size={28} />
                                <div>
                                    <h2 className="text-xl font-bold text-white flex items-center gap-3">
                                        {agent.hostname}
                                        <span className={`text-xs px-2 py-1 rounded-full ${agent.status === 'offline' ? 'bg-slate-500/20 text-slate-400' : 'bg-emerald-500/20 text-emerald-400'}`}>
                                            {agent.is_draining ? 'DRAINING' : agent.status.toUpperCase()}
                                        </span>
                                    </h2>
                                    <p className="text-sm text-slate-400">OS: {agent.os} • Uptime: {Math.floor(agent.uptime_seconds / 3600)}h</p>
                                </div>
                            </div>
                            <div className="flex gap-4">
                                <div className="text-right">
                                    <p className="text-sm text-slate-400 flex items-center gap-2"><Cpu size={14} /> CPU</p>
                                    <p className="font-mono text-white">{agent.cpu_usage.toFixed(1)}%</p>
                                </div>
                                <div className="text-right">
                                    <p className="text-sm text-slate-400 flex items-center gap-2"><HardDrive size={14} /> RAM</p>
                                    <p className="font-mono text-white">{agent.memory_usage.toFixed(1)}%</p>
                                </div>
                                <button 
                                    onClick={() => handleDrain(agent.id)}
                                    disabled={agent.is_draining || agent.status === 'offline'}
                                    className="ml-4 px-4 py-2 bg-yellow-500/10 text-yellow-400 rounded-lg hover:bg-yellow-500/20 transition disabled:opacity-50 border border-yellow-500/20 flex items-center gap-2">
                                    <PowerOff size={16} /> Drain Node
                                </button>
                            </div>
                        </div>
                        <div className="p-6">
                            <h3 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-4">Connected Devices</h3>
                            <div className="grid grid-cols-4 gap-4">
                                {(!agent.connected_devices || agent.connected_devices.length === 0) ? (
                                    <div className="col-span-4 text-slate-500 italic text-sm">No devices attached to this node.</div>
                                ) : agent.connected_devices.map((device: any, i: number) => (
                                    <div key={i} className="bg-slate-900/50 p-4 rounded-xl border border-slate-700/30 flex items-center gap-3">
                                        <Smartphone className="text-indigo-400" size={24} />
                                        <div>
                                            <p className="font-mono text-sm text-slate-200">{typeof device === 'string' ? device : device.id}</p>
                                            <p className="text-xs text-emerald-400 mt-1 flex items-center gap-1"><Power size={12}/> Ready</p>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}

function HardDrive(props: any) {
    return <svg {...props} xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="12" x2="2" y2="12"></line><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path><line x1="6" y1="16" x2="6.01" y2="16"></line><line x1="10" y1="16" x2="10.01" y2="16"></line></svg>;
}
