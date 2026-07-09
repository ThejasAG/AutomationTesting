import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Save,
  Play,
  FileCode,
  ChevronDown,
  Loader2,
  CheckCircle2,
  AlertCircle,
  MonitorSmartphone,
  X,
} from 'lucide-react';
import FileTree from '../components/FileTree';
import type { FileEntry } from '../components/FileTree';
import MonacoEditor from '../components/MonacoEditor';
import {
  getProjects,
  getProjectFiles,
  getProjectFileContent,
  saveProjectFile,
  runProjectFile,
} from '../api';
import type { Project } from '../api';

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
  onDeviceChange: (val: string) => void;
  onConfirm: () => void;
  onClose: () => void;
}

function DeviceModal({ deviceId, onDeviceChange, onConfirm, onClose }: DeviceModalProps) {
  return (
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
      }}
      onClick={onClose}
    >
      <div
        className="card animate-fade-in"
        style={{ width: 420, padding: '32px', position: 'relative' }}
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
          Enter the ADB device ID to run the test against (e.g. <code style={{ color: 'var(--warning)' }}>emulator-5554</code>).
        </p>

        <input
          type="text"
          placeholder="emulator-5554"
          value={deviceId}
          onChange={e => onDeviceChange(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && deviceId.trim()) onConfirm(); }}
          autoFocus
          style={{
            width: '100%',
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid var(--border-highlight)',
            borderRadius: 'var(--radius-sm)',
            color: 'var(--text-primary)',
            padding: '10px 14px',
            fontSize: '0.95rem',
            marginBottom: '24px',
            outline: 'none',
            fontFamily: "'Fira Code', monospace",
          }}
        />

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
  );
}

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
  const [showDeviceModal, setShowDeviceModal] = useState(false);
  const [isRunning, setIsRunning] = useState(false);

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
          <div>
            <h1 className="page-title" style={{ margin: 0 }}>Script Editor</h1>
            <p className="page-subtitle" style={{ marginBottom: 0 }}>
              Browse, edit and run test scripts — changes are committed back to Git
            </p>
          </div>
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
                {deviceId}
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
          onDeviceChange={setDeviceId}
          onConfirm={handleDeviceConfirm}
          onClose={() => setShowDeviceModal(false)}
        />
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
