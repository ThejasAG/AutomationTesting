import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import {
  FileText, X, Loader2, Download, Sparkles, CheckCircle2, AlertTriangle, Clock, Smartphone, Bug, FileDown,
} from 'lucide-react';
import { getReports, getReport, generateReport, getReportTrends, getReportConfig, fileJira, API_BASE, getHeaders } from '../api';
import type { ReportRow, FullReport, ReportTrends } from '../api';
import ModalPortal from '../components/ModalPortal';
import { formatDistanceToNow } from 'date-fns';
import { parseServerDate } from '../time';
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, Legend } from 'recharts';

const ghostBtn: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', fontSize: '0.8rem',
  background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)',
  color: 'var(--text-secondary)', cursor: 'pointer',
};

function statusColor(s: string) {
  const t = (s || '').toLowerCase();
  if (t === 'passed' || t === 'completed') return 'passed';
  if (t === 'failed' || t === 'error') return 'failed';
  return t;
}

// Honest verdict badge: a run with 0 scenarios verified nothing — amber "no tests",
// never a green pass.
function VerdictBadge({ verdict }: { verdict: string }) {
  if (verdict === 'no-tests') return (
    <span className="badge" style={{ background: 'rgba(251,191,36,0.16)', color: '#fbbf24' }}>
      <AlertTriangle size={12} /> no tests
    </span>
  );
  if (verdict === 'failed') return <span className="badge failed"><AlertTriangle size={12} /> failed</span>;
  if (verdict === 'passed') return <span className="badge passed"><CheckCircle2 size={12} /> passed</span>;
  return <span className={`badge ${statusColor(verdict)}`}>{verdict}</span>;
}

// The rich report endpoint requires a login. window.open() performs a plain
// browser navigation with no Authorization header, so it 401s — fetch the HTML
// with credentials and open that instead of leaving the endpoint unprotected.
async function openRichReport(runId: string) {
    const tab = window.open('', '_blank');
    try {
        const res = await fetch(`${API_BASE}/reports/${runId}/rich`, { headers: getHeaders() });
        if (!res.ok) throw new Error(`Report failed to load (${res.status})`);
        const html = await res.text();
        if (tab) { tab.document.open(); tab.document.write(html); tab.document.close(); }
    } catch (e) {
        if (tab) tab.document.body.innerText = `Could not open the report: ${(e as Error).message}`;
    }
}

export default function ReportsPage() {
  const [rows, setRows] = useState<ReportRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<string | null>(null);
  const [trends, setTrends] = useState<ReportTrends | null>(null);

  useEffect(() => {
    getReports().then(setRows).catch(() => {}).finally(() => setLoading(false));
    getReportTrends(30).then(setTrends).catch(() => {});
  }, []);

  return (
    <div className="animate-fade-in">
      <header className="page-header">
        <h1 className="page-title">Test Reports</h1>
        <p className="page-subtitle">Every test run as a proper, stored report — results, root cause, and a plain-English explanation you can export.</p>
      </header>

      {trends && trends.total_runs > 0 && (
        <div style={{ marginBottom: 28 }}>
          <div className="grid-3" style={{ marginBottom: 16 }}>
            <div className="card"><div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Pass rate (30d)</div><div className="stat-value" style={{ color: trends.pass_rate >= 80 ? 'var(--success)' : trends.pass_rate >= 50 ? '#fbbf24' : 'var(--danger)' }}>{trends.pass_rate}%</div></div>
            <div className="card"><div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Runs (30d)</div><div className="stat-value">{trends.total_runs}</div><div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>{trends.passed} passed · {trends.failed} failed</div></div>
            <div className="card"><div style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>Flaky tests</div><div className="stat-value" style={{ color: trends.flaky.length ? '#fbbf24' : 'var(--success)' }}>{trends.flaky.length}</div><div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>pass sometimes, fail others</div></div>
          </div>

          {trends.daily.length > 1 && (
            <div className="card" style={{ marginBottom: 16 }}>
              <div style={{ fontWeight: 600, fontSize: '0.9rem', marginBottom: 12 }}>Pass / fail over time</div>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={trends.daily} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
                  <XAxis dataKey="date" tick={{ fontSize: 11, fill: 'var(--text-muted)' }} tickFormatter={(d: string) => d.slice(5)} />
                  <YAxis tick={{ fontSize: 11, fill: 'var(--text-muted)' }} allowDecimals={false} />
                  <Tooltip contentStyle={{ background: '#1c1d2b', border: '1px solid var(--border-color)', borderRadius: 8, fontSize: 12 }} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="passed" stackId="a" fill="#34d399" radius={[0, 0, 0, 0]} />
                  <Bar dataKey="failed" stackId="a" fill="#f87171" radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div className="card">
              <div style={{ fontWeight: 600, fontSize: '0.9rem', marginBottom: 10 }}>By environment</div>
              {trends.by_environment.map(e => (
                <div key={e.environment} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid rgba(255,255,255,0.04)', fontSize: '0.84rem' }}>
                  <span>{e.environment}</span>
                  <span><span style={{ color: e.pass_rate >= 80 ? 'var(--success)' : '#fbbf24' }}>{e.pass_rate}%</span> <span style={{ color: 'var(--text-muted)' }}>({e.passed}✓/{e.failed}✗)</span></span>
                </div>
              ))}
            </div>
            <div className="card">
              <div style={{ fontWeight: 600, fontSize: '0.9rem', marginBottom: 10 }}>Flaky tests</div>
              {trends.flaky.length === 0 ? <div style={{ fontSize: '0.83rem', color: 'var(--text-muted)' }}>None — nothing flip-flopped. ✓</div>
                : trends.flaky.map(f => (
                  <div key={f.test_name} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid rgba(255,255,255,0.04)', fontSize: '0.82rem' }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '65%' }}>{f.test_name}</span>
                    <span style={{ color: 'var(--text-muted)' }}>{f.passed}✓ / {f.failed}✗</span>
                  </div>
                ))}
            </div>
          </div>
        </div>
      )}

      {loading ? (
        <div style={{ padding: 40, color: 'var(--text-secondary)' }}><Loader2 size={18} className="spin" /> Loading…</div>
      ) : rows.length === 0 ? (
        <div className="card" style={{ textAlign: 'center', padding: 48, color: 'var(--text-muted)' }}>
          <FileText size={30} style={{ opacity: 0.5, marginBottom: 12 }} />
          <div style={{ color: 'var(--text-secondary)' }}>No test runs yet — reports appear here once tests run.</div>
        </div>
      ) : (
        <div className="table-container table-scroll">
          <table>
            <thead><tr><th>Status</th><th>Test</th><th>Suite</th><th>Device</th><th>Scenarios</th><th>When</th><th>Report</th></tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id} className="row-link" onClick={() => setOpen(r.id)}>
                  <td><VerdictBadge verdict={r.verdict} /></td>
                  <td style={{ fontWeight: 500 }}>{r.test_name}</td>
                  <td><span style={{ color: 'var(--text-secondary)', fontFamily: 'monospace', fontSize: '0.8rem' }}>{r.test_suite}</span></td>
                  <td><div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-secondary)', fontSize: '0.8rem' }}><Smartphone size={13} /> {(r.device_name || 'N/A').slice(0, 10)}</div></td>
                  <td style={{ color: 'var(--text-secondary)' }}>{r.scenarios_total ? `${r.scenarios_passed}/${r.scenarios_total}` : '—'}</td>
                  <td style={{ color: 'var(--text-secondary)' }}>{r.created_at ? formatDistanceToNow(parseServerDate(r.created_at), { addSuffix: true }) : '—'}</td>
                  <td>{r.has_report
                    ? <span style={{ fontSize: '0.75rem', color: 'var(--success)' }}>● ready</span>
                    : <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>— none</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && <ReportModal runId={open} onClose={() => setOpen(null)} onChanged={() => getReports().then(setRows).catch(() => {})} />}
    </div>
  );
}

function ReportModal({ runId, onClose, onChanged }: { runId: string; onClose: () => void; onChanged: () => void }) {
  const [rep, setRep] = useState<FullReport | null>(null);
  const [gen, setGen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cfg, setCfg] = useState<{ slack: boolean; jira: boolean } | null>(null);

  useEffect(() => { getReport(runId).then(setRep).catch(e => setError(e?.message || 'Could not load')); }, [runId]);
  useEffect(() => { getReportConfig().then(setCfg).catch(() => {}); }, []);

  const generate = async () => {
    setGen(true); setError(null);
    try {
      const r = await generateReport(runId);
      setRep(p => p ? { ...p, report_summary: r.report_summary, report_generated_at: r.report_generated_at, has_report: true } : p);
      onChanged();
    } catch (e: any) { setError(e?.message || 'Could not generate the report'); }
    setGen(false);
  };

  const buildHtml = (): string => {
    if (!rep) return '';
    const esc = (s: string) => (s || '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]!));
    const rowsHtml = rep.scenarios.map(s =>
      `<tr><td>${esc(s.scenario_num || '')}</td><td>${esc(s.scenario_name || '')}</td><td>${esc(s.status)}</td><td>${esc(s.error || '')}</td></tr>`).join('');
    return `<!doctype html><html><head><meta charset="utf-8"><title>Report — ${esc(rep.test_name)}</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:820px;margin:40px auto;color:#1a1a2e;line-height:1.6;padding:0 20px}
h1{margin-bottom:4px}.meta{color:#666;font-size:14px;margin-bottom:24px}.badge{padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600}
.pass{background:#d1fae5;color:#065f46}.fail{background:#fee2e2;color:#991b1b}
table{width:100%;border-collapse:collapse;margin:16px 0;font-size:14px}th,td{text-align:left;padding:8px;border-bottom:1px solid #eee}
th{background:#f8f8fa}.section{margin-top:28px}pre{white-space:pre-wrap}@media print{body{margin:0}}</style></head><body>
<h1>${esc(rep.test_name)}</h1>
<div class="meta"><span class="badge ${statusColor(rep.status) === 'failed' ? 'fail' : 'pass'}">${esc(rep.status)}</span>
&nbsp; ${esc(rep.test_suite)} · ${esc(rep.device_name)} (${esc(rep.platform)}) · ${rep.scenarios_passed}/${rep.scenarios_total} scenarios passed
${rep.branch ? ` · ${esc(rep.branch)}@${esc((rep.commit_sha || '').slice(0, 8))}` : ''}</div>
${rep.report_summary ? `<div class="section"><h2>Summary</h2><div>${esc(rep.report_summary).replace(/\n/g, '<br>')}</div></div>` : ''}
${rep.rca ? `<div class="section"><h2>Root Cause</h2><p><b>${esc(rep.rca.root_cause || '')}</b></p><p>${esc(rep.rca.suggested_fix || '')}</p></div>` : ''}
${rep.scenarios.length ? `<div class="section"><h2>Scenario Results</h2><table><thead><tr><th>#</th><th>Scenario</th><th>Status</th><th>Detail</th></tr></thead><tbody>${rowsHtml}</tbody></table></div>` : ''}
<div class="meta" style="margin-top:32px">Generated by the Automation Platform${rep.report_generated_at ? ` · ${parseServerDate(rep.report_generated_at).toLocaleString()}` : ''}</div>
</body></html>`;
  };

  const exportHtml = () => {
    if (!rep) return;
    const blob = new Blob([buildHtml()], { type: 'text/html' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `report-${(rep.test_name || 'run').replace(/[^a-z0-9]+/gi, '_')}.html`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const exportPdf = () => {
    // Open the styled report and invoke the browser's print → "Save as PDF".
    const w = window.open('', '_blank');
    if (!w) return;
    w.document.write(buildHtml());
    w.document.close();
    w.onload = () => { w.focus(); w.print(); };
    setTimeout(() => { try { w.focus(); w.print(); } catch { /* */ } }, 400);
  };

  const [jira, setJira] = useState<{ key: string; url: string } | null>(null);
  const [jiraBusy, setJiraBusy] = useState(false);
  const doJira = async () => {
    setJiraBusy(true); setError(null);
    try { setJira(await fileJira(runId)); }
    catch (e: any) { setError(e?.message || 'Could not file the ticket'); }
    setJiraBusy(false);
  };

  return (
    <ModalPortal onClose={onClose}>
      <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)', backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 999, padding: 16 }} onClick={onClose}>
        <div className="card modal-pop" style={{ width: 760, maxWidth: '96vw', maxHeight: '92vh', overflowY: 'auto', padding: 26, position: 'relative' }} onClick={e => e.stopPropagation()}>
          <button onClick={onClose} style={{ position: 'absolute', top: 16, right: 16, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex' }}><X size={18} /></button>

          {!rep ? (
            <div style={{ padding: 24 }}>{error ? <span style={{ color: 'var(--danger)' }}>{error}</span> : <><Loader2 size={16} className="spin" /> Loading…</>}</div>
          ) : (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6, flexWrap: 'wrap' }}>
                <VerdictBadge verdict={rep.verdict} />
                <h3 style={{ margin: 0 }}>{rep.test_name}</h3>
              </div>
              {rep.verdict === 'no-tests' && (
                <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', background: 'rgba(251,191,36,0.1)', border: '1px solid rgba(251,191,36,0.3)', borderRadius: 'var(--radius-sm)', padding: 12, margin: '8px 0 16px', fontSize: '0.84rem' }}>
                  <AlertTriangle size={16} color="#fbbf24" style={{ flexShrink: 0, marginTop: 2 }} />
                  <div><strong>Not a validated pass — 0 scenarios ran.</strong><br />
                  The app may have built successfully, but nothing was actually tested. Record/tag scenarios for this app so PRs are really verified.</div>
                </div>
              )}
              <div style={{ fontSize: '0.82rem', color: 'var(--text-muted)', display: 'flex', gap: 14, flexWrap: 'wrap', marginBottom: 18 }}>
                <span>{rep.test_suite}</span>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><Smartphone size={12} /> {rep.device_name} ({rep.platform})</span>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><Clock size={12} /> {((rep.duration_ms || 0) / 1000).toFixed(1)}s</span>
                {rep.scenarios_total > 0 && <span>{rep.scenarios_passed}/{rep.scenarios_total} scenarios passed</span>}
                {rep.branch && <span style={{ fontFamily: 'monospace' }}>{rep.branch}@{(rep.commit_sha || '').slice(0, 8)}</span>}
              </div>

              {/* AI narrative */}
              <div style={{ border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: 16, marginBottom: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: '0.9rem' }}><Sparkles size={15} color="var(--accent-primary)" /> Report summary</div>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <button onClick={() => openRichReport(runId)} className="btn"
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', fontSize: '0.8rem', background: 'var(--accent-primary)' }}
                      title="Open the full styled report (VAT tables, per-scenario validation) — print to PDF from there">
                      <FileText size={13} /> Rich Report
                    </button>
                    <button onClick={generate} disabled={gen} className="btn" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px', fontSize: '0.8rem' }}>
                      {gen ? <Loader2 size={13} className="spin" /> : <Sparkles size={13} />}{rep.report_summary ? 'Regenerate' : 'Generate'}
                    </button>
                    {rep.report_summary && <button onClick={exportHtml} style={ghostBtn}><Download size={13} /> HTML</button>}
                    {rep.report_summary && <button onClick={exportPdf} style={ghostBtn}><FileDown size={13} /> PDF</button>}
                    {cfg?.jira && (jira
                      ? <a href={jira.url} target="_blank" rel="noreferrer" style={{ ...ghostBtn, color: 'var(--success)', textDecoration: 'none' }}><Bug size={13} /> {jira.key}</a>
                      : <button onClick={doJira} disabled={jiraBusy} style={ghostBtn}>{jiraBusy ? <Loader2 size={13} className="spin" /> : <Bug size={13} />} File Jira</button>)}
                  </div>
                </div>
                {gen ? <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}><Loader2 size={12} className="spin" /> Writing the report with the local model (Ollama)…</div>
                  : rep.report_summary
                    ? <div className="markdown-body" style={{ fontSize: '0.88rem' }}><ReactMarkdown>{rep.report_summary}</ReactMarkdown></div>
                    : <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>No written report yet — click <strong>Generate</strong> to create a plain-English explanation of this run.</div>}
                {error && <p style={{ color: 'var(--danger)', fontSize: '0.82rem' }}>{error}</p>}
              </div>

              {rep.error_message && (
                <div style={{ border: '1px solid rgba(248,113,113,0.3)', background: 'rgba(248,113,113,0.06)', borderRadius: 'var(--radius-sm)', padding: 12, marginBottom: 16 }}>
                  <div style={{ fontWeight: 600, fontSize: '0.82rem', color: 'var(--danger)', marginBottom: 4 }}>Raw error</div>
                  <div style={{ fontFamily: 'monospace', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>{rep.error_message}</div>
                </div>
              )}

              {rep.rca && (rep.rca.root_cause || rep.rca.suggested_fix) && (
                <div style={{ marginBottom: 16 }}>
                  <div style={{ fontWeight: 600, fontSize: '0.9rem', marginBottom: 8 }}>Root cause</div>
                  {rep.rca.root_cause && <p style={{ fontSize: '0.85rem', margin: '4px 0' }}>{rep.rca.root_cause}</p>}
                  {rep.rca.suggested_fix && <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', margin: '4px 0' }}><strong>Fix:</strong> {rep.rca.suggested_fix}</p>}
                </div>
              )}

              {rep.scenarios.length > 0 && (
                <div>
                  <div style={{ fontWeight: 600, fontSize: '0.9rem', marginBottom: 8 }}>Scenario results</div>
                  <div className="table-container" style={{ maxHeight: 260, overflowY: 'auto' }}>
                    <table><thead><tr><th>#</th><th>Scenario</th><th>Status</th></tr></thead>
                      <tbody>{rep.scenarios.map((s, i) => (
                        <tr key={i}><td style={{ color: 'var(--text-muted)' }}>{s.scenario_num}</td><td>{s.scenario_name}</td>
                          <td><span className={`badge ${statusColor(s.status)}`} style={{ fontSize: '0.68rem' }}>{s.status}</span></td></tr>
                      ))}</tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </ModalPortal>
  );
}
