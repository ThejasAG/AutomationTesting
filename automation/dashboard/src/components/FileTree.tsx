import { useState } from 'react';
import { File, Folder, FolderOpen, ChevronRight, ChevronDown } from 'lucide-react';

export interface FileEntry {
  path: string;
  name: string;
  type: 'file' | 'directory';
}

interface FileTreeProps {
  files: FileEntry[];
  selected: string | null;
  onSelect: (path: string) => void;
}

/** Group file entries by their top-level directory segment. */
function groupByDirectory(files: FileEntry[]): Record<string, FileEntry[]> {
  const groups: Record<string, FileEntry[]> = { __root__: [] };

  for (const entry of files) {
    if (entry.type !== 'file') continue;
    // Normalise Windows back-slashes that the API might return.
    const normPath = entry.path.replace(/\\/g, '/');
    const slashIdx = normPath.indexOf('/');
    if (slashIdx === -1) {
      groups['__root__'].push({ ...entry, path: normPath });
    } else {
      const dir = normPath.slice(0, slashIdx);
      if (!groups[dir]) groups[dir] = [];
      groups[dir].push({ ...entry, path: normPath });
    }
  }

  return groups;
}

const ROW_BASE: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: '7px',
  padding: '5px 10px',
  cursor: 'pointer',
  borderRadius: 'var(--radius-sm)',
  transition: 'background 0.15s, color 0.15s',
  userSelect: 'none',
  fontSize: '0.84rem',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

export default function FileTree({ files, selected, onSelect }: FileTreeProps) {
  const groups = groupByDirectory(files);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const toggleDir = (dir: string) => {
    setCollapsed(prev => {
      const next = new Set(prev);
      next.has(dir) ? next.delete(dir) : next.add(dir);
      return next;
    });
  };

  if (files.length === 0) {
    return (
      <p style={{ color: 'var(--text-muted)', fontSize: '0.82rem', padding: '12px 10px' }}>
        No files found
      </p>
    );
  }

  return (
    <div style={{ fontFamily: "'Fira Code', 'Consolas', monospace", overflowY: 'auto', flex: 1 }}>
      {/* Root-level files (no directory prefix) */}
      {groups['__root__'] && groups['__root__'].length > 0 && (
        <div style={{ marginBottom: '4px' }}>
          {groups['__root__'].map(file => (
            <FileRow
              key={file.path}
              file={file}
              selected={selected}
              onSelect={onSelect}
              indent={0}
            />
          ))}
        </div>
      )}

      {/* Directory groups */}
      {Object.entries(groups)
        .filter(([dir]) => dir !== '__root__')
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([dir, dirFiles]) => {
          if (dirFiles.length === 0) return null;
          const isOpen = !collapsed.has(dir);

          return (
            <div key={dir} style={{ marginBottom: '2px' }}>
              {/* Folder header */}
              <div
                style={{
                  ...ROW_BASE,
                  color: 'var(--text-secondary)',
                  fontWeight: 500,
                }}
                onClick={() => toggleDir(dir)}
                onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.05)')}
                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
              >
                {isOpen
                  ? <ChevronDown size={13} style={{ flexShrink: 0 }} />
                  : <ChevronRight size={13} style={{ flexShrink: 0 }} />}
                {isOpen
                  ? <FolderOpen size={14} color="var(--warning)" style={{ flexShrink: 0 }} />
                  : <Folder size={14} color="var(--warning)" style={{ flexShrink: 0 }} />}
                <span>{dir}/</span>
              </div>

              {/* Children */}
              {isOpen && (
                <div style={{ paddingLeft: '16px' }}>
                  {dirFiles
                    .sort((a, b) => a.name.localeCompare(b.name))
                    .map(file => (
                      <FileRow
                        key={file.path}
                        file={file}
                        selected={selected}
                        onSelect={onSelect}
                        indent={0}
                      />
                    ))}
                </div>
              )}
            </div>
          );
        })}
    </div>
  );
}

// ── Internal row component ──────────────────────────────────────────────────

interface FileRowProps {
  file: FileEntry;
  selected: string | null;
  onSelect: (path: string) => void;
  indent: number;
}

function FileRow({ file, selected, onSelect }: FileRowProps) {
  const isSelected = selected === file.path;

  return (
    <div
      style={{
        ...ROW_BASE,
        paddingLeft: '10px',
        color: isSelected ? 'var(--accent-primary)' : 'var(--text-secondary)',
        background: isSelected ? 'rgba(129, 140, 248, 0.14)' : 'transparent',
        borderLeft: isSelected
          ? '2px solid var(--accent-primary)'
          : '2px solid transparent',
        fontWeight: isSelected ? 500 : 400,
      }}
      onClick={() => onSelect(file.path)}
      onMouseEnter={e => {
        if (!isSelected) e.currentTarget.style.background = 'rgba(255,255,255,0.04)';
      }}
      onMouseLeave={e => {
        if (!isSelected) e.currentTarget.style.background = 'transparent';
      }}
    >
      <File size={13} style={{ flexShrink: 0, opacity: 0.7 }} />
      <span title={file.path} style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {file.name}
      </span>
    </div>
  );
}
