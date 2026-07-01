import { Settings } from 'lucide-react';

export default function SettingsPage() {
  return (
    <div className="animate-fade-in">
      <header className="page-header">
        <h1 className="page-title">Settings</h1>
        <p className="page-subtitle">Configure your automation platform preferences.</p>
      </header>
      
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '24px', color: 'var(--text-primary)' }}>
          <Settings size={24} />
          <h2 style={{ fontSize: '1.25rem', margin: 0 }}>General Settings</h2>
        </div>
        
        <div style={{ color: 'var(--text-muted)' }}>
          <p>Settings panel is under construction.</p>
        </div>
      </div>
    </div>
  );
}
