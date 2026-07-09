import Editor, { type OnMount } from '@monaco-editor/react';

interface MonacoEditorProps {
  value: string;
  onChange: (value: string) => void;
  language?: string;
}

/**
 * Thin wrapper around the Monaco Editor.
 * Fills its container (height="100%") — the parent must have a defined height.
 */
export default function MonacoEditor({
  value,
  onChange,
  language = 'python',
}: MonacoEditorProps) {
  const handleMount: OnMount = (_editor, monaco) => {
    // Override the default dark background to match the platform palette.
    monaco.editor.defineTheme('platform-dark', {
      base: 'vs-dark',
      inherit: true,
      rules: [],
      colors: {
        'editor.background': '#0d0d14',
        'editor.lineHighlightBackground': '#1a1b27',
        'editorLineNumber.foreground': '#4b5563',
        'editorLineNumber.activeForeground': '#818cf8',
        'editor.selectionBackground': '#3730a380',
        'editorCursor.foreground': '#818cf8',
      },
    });
    monaco.editor.setTheme('platform-dark');
  };

  return (
    <Editor
      height="100%"
      width="100%"
      language={language}
      theme="vs-dark"
      value={value}
      onChange={(val) => onChange(val ?? '')}
      onMount={handleMount}
      options={{
        minimap: { enabled: false },
        fontSize: 14,
        lineNumbers: 'on',
        wordWrap: 'on',
        scrollBeyondLastLine: false,
        automaticLayout: true,
        tabSize: 4,
        insertSpaces: true,
        renderLineHighlight: 'gutter',
        padding: { top: 16, bottom: 16 },
        fontFamily: "'Fira Code', 'Cascadia Code', 'Consolas', monospace",
        fontLigatures: true,
        smoothScrolling: true,
        cursorBlinking: 'smooth',
        cursorSmoothCaretAnimation: 'on',
        bracketPairColorization: { enabled: true },
        guides: { bracketPairs: true },
      }}
    />
  );
}
