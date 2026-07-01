import { useEffect, useState } from 'react';
import { getAuditLogs } from '../api';
import { Shield, Users, Key, Database, Fingerprint } from 'lucide-react';

export default function AdminCenter() {
    const [logs, setLogs] = useState<any[]>([]);

    useEffect(() => {
        getAuditLogs().then(data => setLogs(data.logs)).catch(console.error);
    }, []);

    return (
        <div className="p-8 max-w-7xl mx-auto space-y-8 animate-fade-in">
            <header className="mb-8">
                <h1 className="text-3xl font-bold bg-gradient-to-r from-red-400 to-orange-400 bg-clip-text text-transparent flex items-center gap-3">
                    <Shield size={32} className="text-red-400" />
                    Admin Center
                </h1>
                <p className="text-slate-400 mt-2">Manage users, permissions, integrations, and view audit logs.</p>
            </header>

            <div className="grid grid-cols-4 gap-6 mb-8">
                <div className="bg-slate-800/50 p-6 rounded-2xl border border-slate-700/50 hover:bg-slate-800 transition cursor-pointer">
                    <Users className="text-blue-400 mb-4" size={32} />
                    <h3 className="font-bold text-white">Users & Roles</h3>
                    <p className="text-sm text-slate-400">Manage access</p>
                </div>
                <div className="bg-slate-800/50 p-6 rounded-2xl border border-slate-700/50 hover:bg-slate-800 transition cursor-pointer">
                    <Database className="text-emerald-400 mb-4" size={32} />
                    <h3 className="font-bold text-white">Projects</h3>
                    <p className="text-sm text-slate-400">Manage repositories</p>
                </div>
                <div className="bg-slate-800/50 p-6 rounded-2xl border border-slate-700/50 hover:bg-slate-800 transition cursor-pointer">
                    <Key className="text-yellow-400 mb-4" size={32} />
                    <h3 className="font-bold text-white">Integrations</h3>
                    <p className="text-sm text-slate-400">Jira & GitHub setup</p>
                </div>
                <div className="bg-slate-800/50 p-6 rounded-2xl border border-slate-700/50 hover:bg-slate-800 transition cursor-pointer">
                    <Fingerprint className="text-purple-400 mb-4" size={32} />
                    <h3 className="font-bold text-white">Security</h3>
                    <p className="text-sm text-slate-400">API keys & Secrets</p>
                </div>
            </div>

            <div className="bg-slate-800/50 rounded-2xl border border-slate-700/50 overflow-hidden">
                <div className="p-6 border-b border-slate-700/50">
                    <h2 className="text-xl font-bold text-white">System Audit Logs</h2>
                </div>
                <div className="overflow-x-auto">
                    <table className="w-full text-left">
                        <thead className="bg-slate-900/50 text-slate-400">
                            <tr>
                                <th className="p-4 font-medium">Timestamp</th>
                                <th className="p-4 font-medium">User</th>
                                <th className="p-4 font-medium">Action</th>
                                <th className="p-4 font-medium">Resource</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-700/50">
                            {logs.length === 0 ? (
                                <tr><td colSpan={4} className="p-4 text-center text-slate-400">No audit logs found.</td></tr>
                            ) : (
                                logs.map(log => (
                                    <tr key={log.id} className="hover:bg-slate-700/30">
                                        <td className="p-4 text-sm text-slate-400">{new Date(log.timestamp).toLocaleString()}</td>
                                        <td className="p-4 font-mono text-sm text-blue-300">{log.user_id || 'system'}</td>
                                        <td className="p-4 text-slate-200">{log.action}</td>
                                        <td className="p-4 text-slate-400">{log.resource_type} {log.resource_id}</td>
                                    </tr>
                                ))
                            )}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
}
