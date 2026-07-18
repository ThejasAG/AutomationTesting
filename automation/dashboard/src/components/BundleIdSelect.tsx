import { useEffect, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { getProjects } from '../api';

const CUSTOM = '__custom__';

interface BundleOption {
  bundleId: string;
  projectName: string;
  projectId: string;
}

/** App bundle id picker — lists the bundle ids already known from registered
 *  projects, with a "Custom…" escape hatch for apps that aren't registered
 *  (or whose project hasn't been built yet, so no bundle id was detected). */
export default function BundleIdSelect({
  value,
  onChange,
  style,
  projectId,
  placeholder = 'org.example.app',
}: {
  value: string;
  onChange: (v: string) => void;
  style: React.CSSProperties;
  /** When set, this project's own bundle id is preselected. */
  projectId?: string;
  placeholder?: string;
}) {
  const [options, setOptions] = useState<BundleOption[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [custom, setCustom] = useState(false);
  const settled = useRef(false);

  useEffect(() => {
    let cancelled = false;
    getProjects()
      .then(projects => {
        if (cancelled) return;
        const seen = new Set<string>();
        const list: BundleOption[] = [];
        for (const p of projects) {
          const bid = p.app_bundle_id?.trim();
          if (!bid || seen.has(bid)) continue;
          seen.add(bid);
          list.push({ bundleId: bid, projectName: p.name, projectId: p.id });
        }
        setOptions(list);
        setLoaded(true);
      })
      .catch(() => {
        // Projects unreachable — fall back to free text rather than blocking.
        if (!cancelled) setLoaded(true);
      });
    return () => { cancelled = true; };
  }, []);

  // Once the list arrives: preselect this project's app, or switch to custom
  // when the incoming value isn't one of the known ids. Runs once.
  useEffect(() => {
    if (!loaded || settled.current) return;
    settled.current = true;

    if (value) {
      if (!options.some(o => o.bundleId === value)) setCustom(true);
      return;
    }
    const mine = options.find(o => o.projectId === projectId);
    if (mine) onChange(mine.bundleId);
  }, [loaded, options, projectId, value, onChange]);

  // No known bundle ids means the dropdown would be empty — a text field is
  // the only control that still lets the user proceed.
  if (custom || (loaded && options.length === 0)) {
    return (
      <div>
        <input
          type="text"
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={placeholder}
          style={style}
        />
        {options.length > 0 && (
          <button
            type="button"
            onClick={() => { setCustom(false); onChange(''); }}
            style={{
              marginTop: 4, background: 'transparent', border: 'none', padding: 0,
              color: 'var(--accent-primary)', fontSize: '0.7rem', cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            Choose from my projects
          </button>
        )}
      </div>
    );
  }

  return (
    <div style={{ position: 'relative' }}>
      <select
        value={value}
        onChange={e => {
          const next = e.target.value;
          if (next === CUSTOM) {
            setCustom(true);
            onChange('');
          } else {
            onChange(next);
          }
        }}
        style={{ ...style, appearance: 'none', cursor: 'pointer' }}
      >
        {!loaded && <option value="">Loading apps…</option>}
        {loaded && !value && <option value="">Select an app…</option>}
        {options.map(o => (
          <option key={o.bundleId} value={o.bundleId}>
            {o.bundleId} — {o.projectName}
          </option>
        ))}
        {loaded && <option value={CUSTOM}>Custom…</option>}
      </select>
      <ChevronDown
        size={13}
        style={{
          position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)',
          color: 'var(--text-muted)', pointerEvents: 'none',
        }}
      />
    </div>
  );
}
