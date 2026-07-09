import { useState } from 'react';
import { Code2, Sparkles, Copy, Check, ChevronDown, ChevronUp, Key, Smartphone, FileCode2, Loader2, TriangleAlert } from 'lucide-react';
import { getHeaders } from '../api';

const API_BASE = 'http://localhost:8000/api/v1';

const PROVIDERS = [
  { id: 'openai', label: 'OpenAI (GPT-4o)', placeholder: 'sk-...' },
  { id: 'gemini', label: 'Google Gemini', placeholder: 'AIza...' },
  { id: 'claude', label: 'Anthropic Claude', placeholder: 'sk-ant-...' },
];

const PLATFORMS = ['Android', 'iOS'];
const FRAMEWORKS = ['Appium + pytest (Python)', 'Appium + WebdriverIO (JS)'];

export default function ScriptGeneratorPage() {
  const [provider, setProvider] = useState('openai');
  const [apiKey, setApiKey] = useState('');
  const [appName, setAppName] = useState('');
  const [platform, setPlatform] = useState('Android');
  const [framework, setFramework] = useState('Appium + pytest (Python)');
  const [requirements, setRequirements] = useState('');
  const [generatedScript, setGeneratedScript] = useState('');
  const [generatedYaml, setGeneratedYaml] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState<'script' | 'yaml' | null>(null);
  const [showScript, setShowScript] = useState(true);
  const [showYaml, setShowYaml] = useState(true);

  const providerInfo = PROVIDERS.find(p => p.id === provider)!;

  const handleGenerate = async () => {
    if (!apiKey.trim()) { setError('Please enter your API key.'); return; }
    if (!appName.trim()) { setError('Please enter the app name.'); return; }
    if (!requirements.trim()) { setError('Please describe what you want to test.'); return; }
    setError('');
    setGeneratedScript('');
    setGeneratedYaml('');
    setIsLoading(true);
    try {
      const res = await fetch(`${API_BASE}/intelligence/generate-scripts`, {
        method: 'POST',
        headers: { ...getHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, api_key: apiKey, app_name: appName, platform, framework, requirements }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Generation failed');
      }
      const data = await res.json();
      setGeneratedScript(data.test_script);
      setGeneratedYaml(data.automation_yaml);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setIsLoading(false);
    }
  };

  const copyToClipboard = async (text: string, type: 'script' | 'yaml') => {
    await navigator.clipboard.writeText(text);
    setCopied(type);
    setTimeout(() => setCopied(null), 2000);
  };

  return (
    <div className="animate-fade-in" style={{ maxWidth: '1100px', margin: '0 auto' }}>
      {/* Header */}
      <header className="page-header" style={{ marginBottom: '32px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          <div style={{
            width: 48, height: 48, borderRadius: '14px',
            background: 'linear-gradient(135deg, #7c3aed, #4f46e5)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            <Sparkles size={24} color="white" />
          </div>
          <div>
            <h1 className="page-title" style={{ margin: 0 }}>AI Script Generator</h1>
            <p className="page-subtitle" style={{ margin: 0 }}>
              Describe your app &amp; tests — get production-ready Appium scripts instantly
            </p>
          </div>
        </div>
      </header>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px' }}>
        {/* ── Left: Config Form ── */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>

          {/* Provider + API Key */}
          <div className="card" style={{ padding: '24px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '20px' }}>
              <Key size={18} color="var(--primary)" />
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>LLM Provider &amp; API Key</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div style={{ display: 'flex', gap: '10px' }}>
                {PROVIDERS.map(p => (
                  <button
                    key={p.id}
                    onClick={() => setProvider(p.id)}
                    style={{
                      flex: 1, padding: '10px 8px', borderRadius: '10px', border: '1.5px solid',
                      borderColor: provider === p.id ? 'var(--primary)' : 'rgba(255,255,255,0.08)',
                      background: provider === p.id ? 'rgba(99,102,241,0.15)' : 'var(--bg-elevated)',
                      color: provider === p.id ? 'var(--primary)' : 'var(--text-secondary)',
                      cursor: 'pointer', fontSize: '12px', fontWeight: 600, transition: 'all 0.2s',
                    }}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              <input
                type="password"
                value={apiKey}
                onChange={e => setApiKey(e.target.value)}
                placeholder={providerInfo.placeholder}
                style={{
                  width: '100%', padding: '12px 16px', borderRadius: '10px',
                  border: '1.5px solid rgba(255,255,255,0.08)',
                  background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                  fontSize: '14px', fontFamily: 'monospace', outline: 'none',
                  boxSizing: 'border-box',
                }}
              />
              <p style={{ margin: 0, fontSize: '12px', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <TriangleAlert size={12} />
                Your API key is sent directly to the selected provider and never stored.
              </p>
            </div>
          </div>

          {/* App Config */}
          <div className="card" style={{ padding: '24px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '20px' }}>
              <Smartphone size={18} color="var(--primary)" />
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>App Configuration</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div>
                <label style={{ display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '6px', fontWeight: 500 }}>
                  App Name / Package
                </label>
                <input
                  type="text"
                  value={appName}
                  onChange={e => setAppName(e.target.value)}
                  placeholder="e.g. Banking App  or  com.mybank.app"
                  style={{
                    width: '100%', padding: '11px 14px', borderRadius: '10px',
                    border: '1.5px solid rgba(255,255,255,0.08)',
                    background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                    fontSize: '14px', outline: 'none', boxSizing: 'border-box',
                  }}
                />
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                <div>
                  <label style={{ display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '6px', fontWeight: 500 }}>Platform</label>
                  <select value={platform} onChange={e => setPlatform(e.target.value)} style={{
                    width: '100%', padding: '11px 14px', borderRadius: '10px',
                    border: '1.5px solid rgba(255,255,255,0.08)',
                    background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                    fontSize: '14px', outline: 'none',
                  }}>
                    {PLATFORMS.map(p => <option key={p}>{p}</option>)}
                  </select>
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '6px', fontWeight: 500 }}>Framework</label>
                  <select value={framework} onChange={e => setFramework(e.target.value)} style={{
                    width: '100%', padding: '11px 14px', borderRadius: '10px',
                    border: '1.5px solid rgba(255,255,255,0.08)',
                    background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                    fontSize: '14px', outline: 'none',
                  }}>
                    {FRAMEWORKS.map(f => <option key={f}>{f}</option>)}
                  </select>
                </div>
              </div>
            </div>
          </div>

          {/* Requirements */}
          <div className="card" style={{ padding: '24px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
              <FileCode2 size={18} color="var(--primary)" />
              <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>What do you want to test?</span>
            </div>
            <textarea
              value={requirements}
              onChange={e => setRequirements(e.target.value)}
              rows={7}
              placeholder={`Describe your test scenarios in plain English. For example:\n\n• Test login with valid and invalid credentials\n• Verify the dashboard loads with account balance\n• Test fund transfer between accounts\n• Verify logout clears the session`}
              style={{
                width: '100%', padding: '14px', borderRadius: '10px',
                border: '1.5px solid rgba(255,255,255,0.08)',
                background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                fontSize: '14px', resize: 'vertical', outline: 'none',
                lineHeight: 1.6, boxSizing: 'border-box', fontFamily: 'inherit',
              }}
            />
          </div>

          {error && (
            <div style={{
              padding: '14px 18px', borderRadius: '10px',
              background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.3)',
              color: '#f87171', fontSize: '14px', display: 'flex', alignItems: 'center', gap: '8px',
            }}>
              <TriangleAlert size={16} /> {error}
            </div>
          )}

          <button
            onClick={handleGenerate}
            disabled={isLoading}
            style={{
              padding: '16px', borderRadius: '12px', border: 'none', cursor: isLoading ? 'not-allowed' : 'pointer',
              background: isLoading ? 'rgba(99,102,241,0.4)' : 'linear-gradient(135deg, #7c3aed, #4f46e5)',
              color: 'white', fontSize: '15px', fontWeight: 700,
              display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '10px',
              transition: 'all 0.2s', boxShadow: isLoading ? 'none' : '0 4px 24px rgba(99,102,241,0.4)',
            }}
          >
            {isLoading ? <><Loader2 size={20} style={{ animation: 'spin 1s linear infinite' }} /> Generating Scripts...</> : <><Sparkles size={20} /> Generate Appium Scripts</>}
          </button>
        </div>

        {/* ── Right: Generated Output ── */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          {!generatedScript && !isLoading && (
            <div className="card" style={{
              padding: '60px 40px', display: 'flex', flexDirection: 'column',
              alignItems: 'center', justifyContent: 'center', gap: '16px',
              border: '2px dashed rgba(255,255,255,0.08)', background: 'transparent', flex: 1, minHeight: '400px',
            }}>
              <div style={{
                width: 64, height: 64, borderRadius: '20px',
                background: 'rgba(99,102,241,0.1)', display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                <Code2 size={32} color="var(--primary)" />
              </div>
              <p style={{ color: 'var(--text-secondary)', textAlign: 'center', margin: 0, fontSize: '15px' }}>
                Fill in the form and click <strong style={{ color: 'var(--primary)' }}>Generate Appium Scripts</strong> to get production-ready test code.
              </p>
              <p style={{ color: 'var(--text-muted)', textAlign: 'center', margin: 0, fontSize: '13px' }}>
                Scripts are editable — copy them, modify them, and add to your repo.
              </p>
            </div>
          )}

          {isLoading && (
            <div className="card" style={{
              padding: '60px 40px', display: 'flex', flexDirection: 'column',
              alignItems: 'center', justifyContent: 'center', gap: '20px', flex: 1, minHeight: '400px',
            }}>
              <div style={{
                width: 64, height: 64, borderRadius: '50%',
                background: 'linear-gradient(135deg, #7c3aed, #4f46e5)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                animation: 'pulse 2s ease-in-out infinite',
              }}>
                <Sparkles size={28} color="white" />
              </div>
              <p style={{ color: 'var(--text-primary)', fontWeight: 600, margin: 0 }}>AI is writing your tests...</p>
              <p style={{ color: 'var(--text-muted)', margin: 0, fontSize: '13px', textAlign: 'center' }}>
                Generating Page Objects, test cases, fixtures, and automation.yaml
              </p>
            </div>
          )}

          {generatedScript && (
            <>
              {/* Test Script */}
              <div className="card" style={{ padding: '0', overflow: 'hidden' }}>
                <div
                  onClick={() => setShowScript(s => !s)}
                  style={{
                    padding: '16px 20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                    cursor: 'pointer', borderBottom: showScript ? '1px solid rgba(255,255,255,0.06)' : 'none',
                    background: 'rgba(99,102,241,0.06)',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <Code2 size={16} color="var(--primary)" />
                    <span style={{ fontWeight: 600, color: 'var(--text-primary)', fontSize: '14px' }}>test_generated.py</span>
                    <span style={{
                      padding: '2px 8px', borderRadius: '20px', fontSize: '11px', fontWeight: 600,
                      background: 'rgba(34,197,94,0.15)', color: '#4ade80',
                    }}>Pytest + Appium</span>
                  </div>
                  <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                    <button
                      onClick={e => { e.stopPropagation(); copyToClipboard(generatedScript, 'script'); }}
                      style={{
                        padding: '6px 12px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.1)',
                        background: 'var(--bg-elevated)', color: copied === 'script' ? '#4ade80' : 'var(--text-secondary)',
                        cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px',
                      }}
                    >
                      {copied === 'script' ? <><Check size={12} /> Copied!</> : <><Copy size={12} /> Copy</>}
                    </button>
                    {showScript ? <ChevronUp size={16} color="var(--text-muted)" /> : <ChevronDown size={16} color="var(--text-muted)" />}
                  </div>
                </div>
                {showScript && (
                  <textarea
                    value={generatedScript}
                    onChange={e => setGeneratedScript(e.target.value)}
                    rows={22}
                    style={{
                      width: '100%', padding: '20px', border: 'none', outline: 'none',
                      background: '#0d1117', color: '#e6edf3',
                      fontFamily: '"Fira Code", "Cascadia Code", "Courier New", monospace',
                      fontSize: '13px', lineHeight: 1.65, resize: 'vertical', boxSizing: 'border-box',
                    }}
                  />
                )}
              </div>

              {/* automation.yaml */}
              <div className="card" style={{ padding: '0', overflow: 'hidden' }}>
                <div
                  onClick={() => setShowYaml(s => !s)}
                  style={{
                    padding: '16px 20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                    cursor: 'pointer', borderBottom: showYaml ? '1px solid rgba(255,255,255,0.06)' : 'none',
                    background: 'rgba(251,146,60,0.06)',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <FileCode2 size={16} color="#fb923c" />
                    <span style={{ fontWeight: 600, color: 'var(--text-primary)', fontSize: '14px' }}>automation.yaml</span>
                    <span style={{
                      padding: '2px 8px', borderRadius: '20px', fontSize: '11px', fontWeight: 600,
                      background: 'rgba(251,146,60,0.15)', color: '#fb923c',
                    }}>Platform Config</span>
                  </div>
                  <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                    <button
                      onClick={e => { e.stopPropagation(); copyToClipboard(generatedYaml, 'yaml'); }}
                      style={{
                        padding: '6px 12px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.1)',
                        background: 'var(--bg-elevated)', color: copied === 'yaml' ? '#4ade80' : 'var(--text-secondary)',
                        cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px',
                      }}
                    >
                      {copied === 'yaml' ? <><Check size={12} /> Copied!</> : <><Copy size={12} /> Copy</>}
                    </button>
                    {showYaml ? <ChevronUp size={16} color="var(--text-muted)" /> : <ChevronDown size={16} color="var(--text-muted)" />}
                  </div>
                </div>
                {showYaml && (
                  <textarea
                    value={generatedYaml}
                    onChange={e => setGeneratedYaml(e.target.value)}
                    rows={16}
                    style={{
                      width: '100%', padding: '20px', border: 'none', outline: 'none',
                      background: '#0d1117', color: '#e6edf3',
                      fontFamily: '"Fira Code", "Cascadia Code", "Courier New", monospace',
                      fontSize: '13px', lineHeight: 1.65, resize: 'vertical', boxSizing: 'border-box',
                    }}
                  />
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
