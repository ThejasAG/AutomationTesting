import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import ModalPortal from '../components/ModalPortal';
import {
  Save,
  Play,
  FileCode,
  ChevronDown,
  Loader2,
  CheckCircle2,
  AlertCircle,
  MonitorSmartphone,
  Wand2,
  X,
} from 'lucide-react';
import FileTree from '../components/FileTree';
import type { FileEntry } from '../components/FileTree';
import MonacoEditor from '../components/MonacoEditor';
import ScenarioPanel from '../components/ScenarioPanel';
import BundleIdSelect from '../components/BundleIdSelect';
import {
  getProjects,
  getProjectFiles,
  getProjectFileContent,
  saveProjectFile,
  runProjectFile,
  generateScript,
  captureLocators,
  getDevices,
  getScenarioDevices,
} from '../api';
import type { Project, Device, SimDevice } from '../api';

// ── Types ────────────────────────────────────────────────────────────────────

type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Infer Monaco language from a file extension. */
function detectLanguage(filePath: string | null): string {
  if (!filePath) return 'python';
  if (filePath.endsWith('.yaml') || filePath.endsWith('.yml')) return 'yaml';
  if (filePath.endsWith('.json')) return 'json';
  if (filePath.endsWith('.md')) return 'markdown';
  if (filePath.endsWith('.txt')) return 'plaintext';
  return 'python';
}

// ── Device Selector Modal ─────────────────────────────────────────────────────

interface DeviceModalProps {
  deviceId: string;
  devices: SimDevice[];
  onDeviceChange: (val: string) => void;
  onConfirm: () => void;
  onClose: () => void;
}

function DeviceModal({ deviceId, devices, onDeviceChange, onConfirm, onClose }: DeviceModalProps) {
  return (
    <ModalPortal onClose={onClose}>
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.72)',
        backdropFilter: 'blur(6px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 999,
        padding: 16,
      }}
      onClick={onClose}
    >
      <div
        className="card modal-pop"
        style={{ width: 420, maxWidth: '94vw', maxHeight: '90vh', overflowY: 'auto', padding: '32px', position: 'relative' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Close button */}
        <button
          onClick={onClose}
          style={{
            position: 'absolute',
            top: 16,
            right: 16,
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
            color: 'var(--text-muted)',
            display: 'flex',
          }}
        >
          <X size={18} />
        </button>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '20px' }}>
          <MonitorSmartphone size={24} color="var(--accent-primary)" />
          <h3 style={{ margin: 0 }}>Select Target Device</h3>
        </div>

        <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginBottom: '20px' }}>
          Pick the simulator to run this script on. Booted simulators are marked ●.
        </p>

        <select
          value={deviceId}
          onChange={e => onDeviceChange(e.target.value)}
          autoFocus
          style={{
            width: '100%', boxSizing: 'border-box',
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid var(--border-highlight)',
            borderRadius: 'var(--radius-sm)',
            color: 'var(--text-primary)',
            padding: '10px 14px',
            fontSize: '0.95rem',
            marginBottom: '24px',
            outline: 'none',
            cursor: 'pointer',
          }}
        >
          {devices.length === 0 && <option value="">No simulators found</option>}
          {devices.map(d => (
            <option key={d.udid} value={d.udid}>
              {d.name}{d.state === 'Booted' ? ' ● booted' : ''} — iOS {d.ios}
            </option>
          ))}
        </select>

        <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
          <button
            onClick={onClose}
            style={{
              background: 'transparent',
              border: '1px solid var(--border-color)',
              borderRadius: 'var(--radius-sm)',
              color: 'var(--text-secondary)',
              padding: '8px 20px',
              cursor: 'pointer',
              fontSize: '0.875rem',
              fontFamily: 'inherit',
            }}
          >
            Cancel
          </button>
          <button
            className="btn"
            disabled={!deviceId.trim()}
            onClick={onConfirm}
            style={{ padding: '8px 20px', fontSize: '0.875rem', opacity: deviceId.trim() ? 1 : 0.5 }}
          >
            <Play size={14} /> Run File
          </button>
        </div>
      </div>
    </div>
    </ModalPortal>
  );
}

// ── AI Generate Modal ─────────────────────────────────────────────────────────

interface GenModalProps {
  onClose: () => void;
  /** Called with the generated script + a suggested filename. */
  onGenerated: (script: string, filename: string) => void;
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  background: 'rgba(255,255,255,0.05)',
  border: '1px solid var(--border-highlight)',
  borderRadius: 'var(--radius-sm)',
  color: 'var(--text-primary)',
  padding: '9px 12px',
  fontSize: '0.9rem',
  outline: 'none',
  fontFamily: 'inherit',
};

function GenerateModal({ onClose, onGenerated }: GenModalProps) {
  const [provider, setProvider] = useState('ollama');   // local, no key, default
  const [apiKey, setApiKey] = useState('');
  const [appName, setAppName] = useState('');
  const [requirements, setRequirements] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [error, setError] = useState<string | null>(null);

  // Optional grounding: capture real testIDs from a running app first.
  const [ground, setGround] = useState(false);
  const [devices, setDevices] = useState<Device[]>([]);
  const [deviceId, setDeviceId] = useState('');
  const [bundleId, setBundleId] = useState('');

  const needsKey = provider !== 'ollama';

  useEffect(() => {
    if (!ground) return;
    getDevices().then(r => {
      setDevices(r.devices);
      if (r.devices.length && !deviceId) setDeviceId(r.devices[0].id);
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ground]);

  const canGenerate =
    appName.trim() && requirements.trim() && !busy &&
    (!needsKey || apiKey.trim()) &&
    (!ground || (deviceId && bundleId.trim()));

  const handleGenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      let known: string[] | undefined;
      let folded: string[] | undefined;
      if (ground) {
        setStatus('Capturing real locators from the running app…');
        const cap = await captureLocators(deviceId, bundleId.trim());
        known = cap.known_testids;
        folded = cap.folded_testids;
        setStatus(`Captured ${known.length} testIDs — generating…`);
      } else {
        setStatus('Generating…');
      }
      const { test_script } = await generateScript({
        provider,
        api_key: needsKey ? apiKey.trim() : undefined,
        app_name: appName.trim(),
        requirements: requirements.trim(),
        known_testids: known,
        folded_testids: folded,
      });
      const slug = appName.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
      onGenerated(test_script, `e2e/test_${slug || 'generated'}.py`);
    } catch (e: any) {
      setError(e?.message || 'Generation failed');
      setBusy(false);
      setStatus('');
    }
  };

  return (
    <ModalPortal onClose={onClose}>
    <div
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)',
        backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center',
        justifyContent: 'center', zIndex: 999, padding: 16,
      }}
      onClick={onClose}
    >
      <div
        className="card modal-pop"
        style={{ width: 520, maxWidth: '94vw', maxHeight: '90vh', overflowY: 'auto', padding: '32px', position: 'relative' }}
        onClick={e => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          style={{ position: 'absolute', top: 16, right: 16, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', display: 'flex' }}
        >
          <X size={18} />
        </button>

        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
          <Wand2 size={24} color="var(--accent-primary)" />
          <h3 style={{ margin: 0 }}>Generate Test Script with AI</h3>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: '22px' }}>
          Describe what to test. The generated script drops into the editor, where you can edit and save it.
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
          <div style={{ display: 'flex', gap: '12px' }}>
            <div style={{ flex: needsKey ? 1 : 2 }}>
              <label style={labelStyle}>Provider</label>
              <select value={provider} onChange={e => setProvider(e.target.value)} style={{ ...inputStyle, appearance: 'none', cursor: 'pointer' }}>
                <option value="ollama">Ollama (Local — no key)</option>
                <option value="claude">Claude</option>
                <option value="openai">OpenAI</option>
                <option value="gemini">Gemini</option>
              </select>
            </div>
            {needsKey && (
              <div style={{ flex: 2 }}>
                <label style={labelStyle}>API Key</label>
                <input type="password" placeholder="sk-…" value={apiKey} onChange={e => setApiKey(e.target.value)} style={inputStyle} />
              </div>
            )}
          </div>

          <div>
            <label style={labelStyle}>App name</label>
            <input type="text" placeholder="Consumer App" value={appName} onChange={e => setAppName(e.target.value)} style={inputStyle} />
          </div>

          <div>
            <label style={labelStyle}>What should the test cover?</label>
            <textarea
              placeholder="e.g. Verify the home feed loads restaurant cards from the API, then tap the first card and assert the detail screen opens."
              value={requirements}
              onChange={e => setRequirements(e.target.value)}
              rows={4}
              style={{ ...inputStyle, resize: 'vertical', fontFamily: 'inherit' }}
            />
          </div>

          {/* Optional grounding — capture real testIDs from the running app */}
          <div style={{ border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', padding: '12px 14px' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
              <input type="checkbox" checked={ground} onChange={e => setGround(e.target.checked)} />
              Ground with real testIDs from the running app <span style={{ color: 'var(--text-muted)' }}>(recommended — stops the AI inventing selectors)</span>
            </label>
            {ground && (
              <div style={{ display: 'flex', gap: '10px', marginTop: '12px' }}>
                <div style={{ flex: 1 }}>
                  <label style={labelStyle}>Device</label>
                  <select value={deviceId} onChange={e => setDeviceId(e.target.value)} style={{ ...inputStyle, appearance: 'none', cursor: 'pointer' }}>
                    {devices.length === 0 && <option value="">No devices online</option>}
                    {devices.map(d => <option key={d.id} value={d.id}>{d.name || d.id}</option>)}
                  </select>
                </div>
                <div style={{ flex: 1 }}>
                  <label style={labelStyle}>App bundle id</label>
                  <BundleIdSelect
                    value={bundleId}
                    onChange={setBundleId}
                    style={inputStyle}
                    placeholder="org.vyapy.sarls.vyaconsumer"
                  />
                </div>
              </div>
            )}
          </div>

          {busy && status && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)', fontSize: '0.82rem' }}>
              <Loader2 size={13} style={{ animation: 'se-spin 1s linear infinite' }} /> {status}
            </div>
          )}
          {error && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--danger)', fontSize: '0.82rem' }}>
              <AlertCircle size={14} /> {error}
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end', marginTop: '24px' }}>
          <button
            onClick={onClose}
            style={{ background: 'transparent', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', color: 'var(--text-secondary)', padding: '8px 20px', cursor: 'pointer', fontSize: '0.875rem', fontFamily: 'inherit' }}
          >
            Cancel
          </button>
          <button
            className="btn"
            disabled={!canGenerate}
            onClick={handleGenerate}
            style={{ padding: '8px 20px', fontSize: '0.875rem', opacity: canGenerate ? 1 : 0.5 }}
          >
            {busy ? <Loader2 size={14} style={{ animation: 'se-spin 1s linear infinite' }} /> : <Wand2 size={14} />}
            {busy ? 'Generating…' : 'Generate'}
          </button>
        </div>
      </div>
    </div>
    </ModalPortal>
  );
}

const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: '0.7rem', color: 'var(--text-muted)',
  textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '6px', fontWeight: 600,
};

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function ScriptEditorPage() {
  const navigate = useNavigate();

  // Project state
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');

  // File tree state
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [isLoadingFiles, setIsLoadingFiles] = useState(false);

  // Editor state
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [editorContent, setEditorContent] = useState<string>('');
  const [isLoadingContent, setIsLoadingContent] = useState(false);

  // Save state
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');

  // Run / device state
  const [deviceId, setDeviceId] = useState<string>('');
  const [sims, setSims] = useState<SimDevice[]>([]);
  const [showDeviceModal, setShowDeviceModal] = useState(false);
  const [isRunning, setIsRunning] = useState(false);

  // ── Load available simulators; default to a booted one ──────────────────
  useEffect(() => {
    getScenarioDevices()
      .then(({ simulators }) => {
        setSims(simulators);
        setDeviceId(prev => {
          if (prev && simulators.some(s => s.udid === prev)) return prev;
          const booted = simulators.find(s => s.state === 'Booted');
          return (booted || simulators[0])?.udid || '';
        });
      })
      .catch(() => { /* picker shows "no simulators found" */ });
  }, []);

  // AI generation
  const [showGenModal, setShowGenModal] = useState(false);
  const [showScenario, setShowScenario] = useState(false);

  // ── Load project list on mount ──────────────────────────────────────────
  useEffect(() => {
    getProjects()
      .then(list => {
        setProjects(list);
        if (list.length > 0) setSelectedProjectId(list[0].id);
      })
      .catch(err => console.error('Failed to load projects:', err));
  }, []);

  // ── Load file tree when project changes ─────────────────────────────────
  useEffect(() => {
    if (!selectedProjectId) return;
    setIsLoadingFiles(true);
    setFiles([]);
    setSelectedFile(null);
    setEditorContent('');
    setSaveStatus('idle');

    getProjectFiles(selectedProjectId)
      .then(setFiles)
      .catch(err => console.error('Failed to load files:', err))
      .finally(() => setIsLoadingFiles(false));
  }, [selectedProjectId]);

  // ── Load file content when a file is selected ────────────────────────────
  const handleFileSelect = useCallback(
    async (path: string) => {
      if (!selectedProjectId || path === selectedFile) return;
      setSelectedFile(path);
      setIsLoadingContent(true);
      setSaveStatus('idle');
      try {
        const content = await getProjectFileContent(selectedProjectId, path);
        setEditorContent(content);
      } catch (err) {
        console.error('Failed to load file content:', err);
        setEditorContent('');
      } finally {
        setIsLoadingContent(false);
      }
    },
    [selectedProjectId, selectedFile],
  );

  // ── Save ─────────────────────────────────────────────────────────────────
  const handleSave = async () => {
    if (!selectedProjectId || !selectedFile) return;
    setSaveStatus('saving');
    try {
      await saveProjectFile(selectedProjectId, selectedFile, editorContent);
      setSaveStatus('saved');
      setTimeout(() => setSaveStatus('idle'), 3000);
    } catch {
      setSaveStatus('error');
      setTimeout(() => setSaveStatus('idle'), 3000);
    }
  };

  // ── Run (called after device is confirmed) ───────────────────────────────
  const executeRun = async (targetDeviceId: string) => {
    if (!selectedProjectId || !selectedFile) return;
    setIsRunning(true);
    try {
      const { run_id } = await runProjectFile(selectedProjectId, selectedFile, targetDeviceId);
      navigate(`/run/${run_id}`);
    } catch (err) {
      console.error('Failed to start run:', err);
      setIsRunning(false);
    }
  };

  const handleRunClick = () => {
    if (!selectedFile) return;
    if (deviceId.trim()) {
      // Device already selected from a previous run — go directly.
      executeRun(deviceId.trim());
    } else {
      setShowDeviceModal(true);
    }
  };

  const handleDeviceConfirm = () => {
    setShowDeviceModal(false);
    if (deviceId.trim()) executeRun(deviceId.trim());
  };

  // ── AI generation → drop into editor ─────────────────────────────────────
  const handleGenerated = (script: string, filename: string) => {
    setShowGenModal(false);
    // Target a new file so Save has somewhere to write. saveProjectFile creates
    // it (and commits) on first save; the file tree refreshes after that.
    setSelectedFile(filename);
    setEditorContent(script);
    setSaveStatus('idle');
  };

  // ── Keyboard shortcut: Ctrl+S to save ───────────────────────────────────
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        handleSave();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId, selectedFile, editorContent]);

  // ── Derived values ───────────────────────────────────────────────────────
  const editorLanguage = detectLanguage(selectedFile);
  const canSave = Boolean(selectedFile) && saveStatus !== 'saving';
  const canRun = Boolean(selectedFile) && !isRunning;

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div
      className="animate-fade-in"
      style={{ height: 'calc(100vh - 96px)', display: 'flex', flexDirection: 'column' }}
    >
      {/* Page header */}
      <div className="page-header" style={{ marginBottom: '20px', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <FileCode size={28} color="var(--accent-primary)" />
          <div style={{ flex: 1 }}>
            <h1 className="page-title" style={{ margin: 0 }}>Scripts</h1>
            <p className="page-subtitle" style={{ marginBottom: 0 }}>
              Generate, browse, edit and run test scripts — changes are committed back to Git
            </p>
          </div>
          <button
            className="btn"
            onClick={() => setShowScenario(true)}
            style={{ padding: '8px 16px', fontSize: '0.85rem', background: 'var(--bg-elevated)', border: '1px solid var(--border-color)', color: 'var(--text-primary)' }}
          >
            <Wand2 size={14} /> Scenario
          </button>
          <button
            className="btn"
            onClick={() => setShowGenModal(true)}
            style={{ padding: '8px 16px', fontSize: '0.85rem' }}
          >
            <Wand2 size={14} /> Generate with AI
          </button>
        </div>
      </div>

      {/* Editor shell */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          minHeight: 0,
          border: '1px solid var(--border-color)',
          borderRadius: 'var(--radius-lg)',
          overflow: 'hidden',
          background: 'rgba(10,10,15,0.5)',
        }}
      >
        {/* ── Left panel — file tree ───────────────────────────────────── */}
        <div
          style={{
            width: 280,
            flexShrink: 0,
            borderRight: '1px solid var(--border-color)',
            display: 'flex',
            flexDirection: 'column',
            background: 'rgba(8,8,12,0.6)',
          }}
        >
          {/* Project selector */}
          <div
            style={{
              padding: '14px 16px',
              borderBottom: '1px solid var(--border-color)',
              flexShrink: 0,
            }}
          >
            <label
              style={{
                display: 'block',
                fontSize: '0.7rem',
                color: 'var(--text-muted)',
                textTransform: 'uppercase',
                letterSpacing: '0.08em',
                marginBottom: '8px',
                fontWeight: 600,
              }}
            >
              Project
            </label>
            <div style={{ position: 'relative' }}>
              <select
                value={selectedProjectId}
                onChange={e => setSelectedProjectId(e.target.value)}
                style={{
                  width: '100%',
                  background: 'rgba(255,255,255,0.05)',
                  border: '1px solid var(--border-color)',
                  borderRadius: 'var(--radius-sm)',
                  color: 'var(--text-primary)',
                  padding: '8px 32px 8px 12px',
                  fontSize: '0.85rem',
                  appearance: 'none',
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
              >
                {projects.length === 0 && (
                  <option value="">No projects</option>
                )}
                {projects.map(p => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              <ChevronDown
                size={13}
                style={{
                  position: 'absolute',
                  right: 10,
                  top: '50%',
                  transform: 'translateY(-50%)',
                  color: 'var(--text-muted)',
                  pointerEvents: 'none',
                }}
              />
            </div>
          </div>

          {/* File tree */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '8px 4px' }}>
            {isLoadingFiles ? (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                  padding: '16px 12px',
                  color: 'var(--text-muted)',
                  fontSize: '0.85rem',
                }}
              >
                <Loader2 size={14} style={{ animation: 'se-spin 1s linear infinite' }} />
                Loading files…
              </div>
            ) : selectedProjectId && files.length === 0 ? (
              <div style={{ padding: '20px 14px', color: 'var(--text-muted)', fontSize: '0.82rem', lineHeight: 1.6 }}>
                No test scripts in this project yet.<br />
                Use <strong>✨ Generate</strong> to create one, or record a flow in the
                <strong> Scenarios</strong> tab — new scripts are saved under <code>e2e/</code>.
              </div>
            ) : (
              <FileTree
                files={files}
                selected={selectedFile}
                onSelect={handleFileSelect}
              />
            )}
          </div>
        </div>

        {/* ── Right panel — editor + toolbar ──────────────────────────── */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          {/* Tab bar */}
          {selectedFile && (
            <div
              style={{
                height: 36,
                borderBottom: '1px solid var(--border-color)',
                display: 'flex',
                alignItems: 'center',
                padding: '0 16px',
                gap: '8px',
                background: 'rgba(0,0,0,0.3)',
                flexShrink: 0,
              }}
            >
              <FileCode size={13} color="var(--accent-primary)" />
              <span
                style={{
                  fontSize: '0.8rem',
                  color: 'var(--text-secondary)',
                  fontFamily: "'Fira Code', monospace",
                }}
              >
                {selectedFile}
              </span>
            </div>
          )}

          {/* Monaco editor area */}
          <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
            {isLoadingContent && (
              <div
                style={{
                  position: 'absolute',
                  inset: 0,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: 'rgba(10,10,15,0.85)',
                  zIndex: 10,
                }}
              >
                <Loader2
                  size={28}
                  style={{ animation: 'se-spin 1s linear infinite', color: 'var(--accent-primary)' }}
                />
              </div>
            )}

            {selectedFile ? (
              <MonacoEditor
                value={editorContent}
                onChange={setEditorContent}
                language={editorLanguage}
              />
            ) : (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  height: '100%',
                  flexDirection: 'column',
                  gap: '14px',
                  color: 'var(--text-muted)',
                }}
              >
                <FileCode size={52} style={{ opacity: 0.25 }} />
                <p style={{ fontSize: '0.95rem' }}>Select a file to start editing</p>
                <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                  Use the file tree on the left
                </p>
              </div>
            )}
          </div>

          {/* Toolbar */}
          <div
            style={{
              height: 52,
              borderTop: '1px solid var(--border-color)',
              display: 'flex',
              alignItems: 'center',
              gap: '12px',
              padding: '0 16px',
              background: 'rgba(0,0,0,0.4)',
              flexShrink: 0,
            }}
          >
            {/* Save button */}
            <button
              className="btn"
              onClick={handleSave}
              disabled={!canSave}
              title="Save (Ctrl+S)"
              style={{
                padding: '7px 16px',
                fontSize: '0.85rem',
                opacity: canSave ? 1 : 0.4,
                cursor: canSave ? 'pointer' : 'not-allowed',
              }}
            >
              {saveStatus === 'saving' ? (
                <Loader2 size={13} style={{ animation: 'se-spin 1s linear infinite' }} />
              ) : (
                <Save size={13} />
              )}
              {saveStatus === 'saving' ? 'Saving…' : 'Save'}
            </button>

            {/* Run button */}
            <button
              className="btn"
              onClick={handleRunClick}
              disabled={!canRun}
              style={{
                padding: '7px 16px',
                fontSize: '0.85rem',
                background: 'linear-gradient(135deg, #059669, #10b981)',
                boxShadow: '0 4px 12px rgba(16,185,129,0.3)',
                opacity: canRun ? 1 : 0.4,
                cursor: canRun ? 'pointer' : 'not-allowed',
              }}
            >
              {isRunning ? (
                <Loader2 size={13} style={{ animation: 'se-spin 1s linear infinite' }} />
              ) : (
                <Play size={13} />
              )}
              {isRunning ? 'Launching…' : 'Run'}
            </button>

            {/* Device badge (shows current device) */}
            {deviceId && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '4px 10px',
                  background: 'rgba(255,255,255,0.04)',
                  border: '1px solid var(--border-color)',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: '0.78rem',
                  color: 'var(--text-secondary)',
                  cursor: 'pointer',
                }}
                title="Click to change device"
                onClick={() => setShowDeviceModal(true)}
              >
                <MonitorSmartphone size={12} />
                {sims.find(s => s.udid === deviceId)?.name || deviceId || 'Select device'}
              </div>
            )}

            {/* Status messages */}
            {saveStatus === 'saved' && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  color: 'var(--success)',
                  fontSize: '0.82rem',
                }}
              >
                <CheckCircle2 size={13} />
                Saved &amp; committed
              </div>
            )}
            {saveStatus === 'error' && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  color: 'var(--danger)',
                  fontSize: '0.82rem',
                }}
              >
                <AlertCircle size={13} />
                Save failed
              </div>
            )}

            {/* Spacer + keyboard hint */}
            <div style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: '0.75rem' }}>
              Ctrl+S to save
            </div>
          </div>
        </div>
      </div>

      {/* Device selector modal */}
      {showDeviceModal && (
        <DeviceModal
          deviceId={deviceId}
          devices={sims}
          onDeviceChange={setDeviceId}
          onConfirm={handleDeviceConfirm}
          onClose={() => setShowDeviceModal(false)}
        />
      )}

      {/* AI generate modal */}
      {showGenModal && (
        <GenerateModal
          onClose={() => setShowGenModal(false)}
          onGenerated={handleGenerated}
        />
      )}

      {/* Scenario runner modal */}
      {showScenario && (
        <div
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.72)', backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'flex-start', justifyContent: 'center', zIndex: 999, padding: '40px 20px', overflowY: 'auto' }}
          onClick={() => setShowScenario(false)}
        >
          <div style={{ width: 620, maxWidth: '100%' }} onClick={e => e.stopPropagation()}>
            <ScenarioPanel
              projectId={selectedProjectId}
              onSaved={(path) => {
                if (selectedProjectId) getProjectFiles(selectedProjectId).then(setFiles).catch(() => {});
                handleFileSelect(path);
              }}
            />
          </div>
        </div>
      )}

      {/* Scoped keyframe for spinner */}
      <style>{`
        @keyframes se-spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
