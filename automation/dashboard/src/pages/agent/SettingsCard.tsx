import { useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { CheckCircle2, Loader2, Settings } from 'lucide-react';
import { agentApi } from '../../agentApi';
import type { AgentSettings, AgentStatus, ScheduleState } from '../../agentApi';
import { AMBER, GREEN, GREY, RED, chip, h3, input, muted, section, wrapText } from './ui';

type Bools = 'nightly_enabled' | 'auto_heal_after_run_all' | 'include_demos' | 'report_slack' | 'report_manual_sessions';

const scheduleChip = (sch: ScheduleState | undefined, enabled: boolean): ReactNode => {
  if (!sch) return null;
  if (sch.error) return chip(`Schedule error: ${sch.error}`, RED, true);
  if (sch.installed) return chip(`Scheduled daily at ${sch.time ?? '—'}${sch.loaded === false ? ' (not loaded)' : ''}`,
    sch.loaded === false ? AMBER : GREEN);
  return chip(enabled ? 'On, but not scheduled yet' : 'Not scheduled', enabled ? AMBER : GREY);
};

/** Agent settings. The form is seeded once from `s`: the page reloads its status
 *  every few seconds, and re-seeding on each reload would wipe edits in progress.
 *  `bare` renders just the form, for a dialog that supplies its own frame and title. */
export default function SettingsCard({ s, schedule, secrets, onSaved, bare = false }: {
  s: AgentSettings; schedule: ScheduleState; secrets: AgentStatus['secrets']; onSaved: () => void; bare?: boolean;
}) {
  const [f, setF] = useState<AgentSettings>(s);
  const [emails, setEmails] = useState((s.report_emails ?? []).join(', '));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [note, setNote] = useState('');
  const [sched, setSched] = useState<ScheduleState | undefined>(undefined);

  const set = <K extends keyof AgentSettings>(k: K, v: AgentSettings[K]) => setF(x => ({ ...x, [k]: v }));
  const row: CSSProperties = { display: 'flex', alignItems: 'center', gap: 10, minHeight: 36, flexWrap: 'wrap' };
  const label: CSSProperties = { width: 210, maxWidth: '100%', color: 'var(--text-secondary)', fontSize: '0.88rem', flexShrink: 0 };
  const box = (k: Bools, text: ReactNode) => (
    <label style={{ ...row, cursor: 'pointer' }}>
      <input type="checkbox" checked={!!f[k]} onChange={e => set(k, e.target.checked)} /> {text}
    </label>
  );
  const num = (k: 'max_new_scenarios' | 'max_fix_attempts' | 'max_changed_lines', text: string, max: number) => (
    <div style={row}><span style={label}>{text}</span>
      <input type="number" min={1} max={max} style={{ ...input, width: 100 }} value={f[k]}
             onChange={e => set(k, Number(e.target.value))} /></div>
  );

  const save = async () => {
    const next: AgentSettings = { ...f, report_emails: emails.split(',').map(x => x.trim()).filter(Boolean) };
    const changes: Record<string, unknown> = {};
    for (const k of Object.keys(next) as (keyof AgentSettings)[]) {
      if (JSON.stringify(next[k]) !== JSON.stringify(s[k])) changes[k] = next[k];
    }
    setErr('');
    setNote('');
    if (!Object.keys(changes).length) { setNote('Nothing changed.'); return; }
    setBusy(true);
    try {
      const r = await agentApi.saveSettings(changes as Partial<AgentSettings>);
      setF(r.settings);
      setEmails((r.settings.report_emails ?? []).join(', '));
      if (r.schedule) setSched(r.schedule);
      setNote('Settings saved.');
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const form = (
      <div className={bare ? undefined : 'card'}
           style={{ ...(bare ? {} : { padding: 22, transform: 'none' }), display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 8, ...wrapText }}>
        <div style={{ fontWeight: 600, marginBottom: 2 }}>Nightly</div>
        {box('nightly_enabled', 'Run every night')}
        <div style={row}><span style={label}>Start time</span>
          <input type="time" style={input} value={f.nightly_time} onChange={e => set('nightly_time', e.target.value)} />
          {scheduleChip(sched ?? schedule, f.nightly_enabled)}
        </div>
        <div style={row}><span style={label}>What it does</span>
          <select style={input} value={f.nightly_mode} onChange={e => set('nightly_mode', e.target.value as AgentSettings['nightly_mode'])}>
            <option value="auto">Auto — full analysis on Sunday, changes only on other nights</option>
            <option value="delta">Changes only</option>
            <option value="full">Full analysis every night</option>
          </select></div>

        <div style={{ fontWeight: 600, margin: '10px 0 2px' }}>Runs</div>
        {box('auto_heal_after_run_all', 'When a Run all has failures, start Run & heal on them')}
        {box('include_demos', 'Include the quick demo flows in Run & heal and nightly')}
        <div style={row}><span style={label}>Environment</span>
          <select style={input} value={f.env} onChange={e => set('env', e.target.value)}>
            <option value="staging">Staging</option><option value="prod">Old Vya (prod)</option></select></div>

        <div style={{ fontWeight: 600, margin: '10px 0 2px' }}>Claude Code</div>
        <div style={row}><span style={label}>Model</span>
          <select style={input} value={f.model} onChange={e => set('model', e.target.value)}>
            <option value="opus">Opus</option><option value="sonnet">Sonnet</option></select></div>
        <div style={row}><span style={label}>Effort</span>
          <select style={input} value={f.effort} onChange={e => set('effort', e.target.value)}>
            {['low', 'medium', 'high', 'xhigh', 'max'].map(x => <option key={x} value={x}>{x}</option>)}</select></div>
        {num('max_new_scenarios', 'New scenarios per session', 20)}
        {num('max_fix_attempts', 'Fix attempts per failure', 5)}
        {num('max_changed_lines', 'Largest change it may propose (lines)', 10000)}

        <div style={{ fontWeight: 600, margin: '10px 0 2px' }}>Report</div>
        {box('report_slack', <>Post the report to Slack{!secrets.slack && <span style={muted}> (Slack is not set up in .env)</span>}</>)}
        {box('report_manual_sessions', 'Also email sessions started by hand')}
        <div style={row}><span style={label}>Report emails</span>
          <input style={{ ...input, flex: 1, minWidth: 220 }} placeholder="empty = REPORT_EMAIL_TO from .env" value={emails}
                 onChange={e => setEmails(e.target.value)} />
          {!secrets.smtp && <span style={muted}>Email is not set up in .env</span>}</div>

        <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginTop: 8, flexWrap: 'wrap' }}>
          <button className="btn" disabled={busy} onClick={save}>
            {busy ? <Loader2 size={16} className="spin" /> : <CheckCircle2 size={16} />} Save settings
          </button>
          {note && <span style={{ color: GREEN, fontSize: '0.85rem' }}>{note}</span>}
          {err && <span style={{ color: RED, fontSize: '0.85rem', ...wrapText }}>{err}</span>}
          <span style={{ ...muted, marginLeft: 'auto' }}>Only an admin can change these.</span>
        </div>
      </div>
  );
  if (bare) return form;
  return (
    <div style={section}>
      <h3 style={h3}><Settings size={18} /> Settings</h3>
      {form}
    </div>
  );
}
