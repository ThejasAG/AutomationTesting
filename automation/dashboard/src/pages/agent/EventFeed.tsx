import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { CheckCircle2, ChevronDown, ChevronRight, Info, Loader2, Terminal, XCircle } from 'lucide-react';
import { agentApi, isActiveStatus } from '../../agentApi';
import type { AgentEvent } from '../../agentApi';
import { GREEN, RED, mono, pre, wrapText } from './ui';

const PAGE = 400;   // the events endpoint returns at most this many per call

/** What Claude Code is doing, step by step. `live` polls for new steps every 3 s
 *  until the session ends; otherwise the whole transcript is loaded once.
 *  Mount it with key={sessionId}. */
export default function EventFeed({ sessionId, live, maxHeight = 440 }: {
  sessionId: string; live: boolean; maxHeight?: number;
}) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(live);   // follow the newest step unless the user scrolled up

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let last = 0;
    const poll = async () => {
      let again = live;
      try {
        for (;;) {
          const p = await agentApi.events(sessionId, last);
          if (cancelled) return;
          if (p.events.length) setEvents(ev => [...ev, ...p.events]);
          last = Math.max(last, p.last);
          again = live && isActiveStatus(p.status);
          if (p.events.length < PAGE) break;
        }
        setErr('');
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      }
      if (cancelled) return;
      setLoading(false);
      if (again) timer = setTimeout(poll, 3000);
    };
    poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [sessionId, live]);

  useLayoutEffect(() => {
    const b = box.current;
    if (b && stick.current) b.scrollTop = b.scrollHeight;
  }, [events]);

  const onScroll = () => {
    const b = box.current;
    if (b) stick.current = b.scrollHeight - b.scrollTop - b.clientHeight < 40;
  };

  return (
    <div style={{ minWidth: 0, maxWidth: '100%' }}>
      {err && <div style={{ color: RED, fontSize: '0.8rem', marginBottom: 6, ...wrapText }}>{err}</div>}
      <div ref={box} onScroll={onScroll}
           style={{ maxHeight, overflowY: 'auto', overflowX: 'hidden', background: 'var(--bg-primary)', borderRadius: 8,
                    border: '1px solid var(--border-color)', padding: '10px 14px', ...wrapText }}>
        {events.map(e => <Row key={e.i} e={e} />)}
        {loading && events.length === 0 && (
          <div style={{ color: 'var(--text-muted)', fontSize: '0.82rem', display: 'flex', gap: 6, alignItems: 'center' }}>
            <Loader2 size={13} className="spin" /> Loading…
          </div>
        )}
        {!loading && events.length === 0 && (
          <div style={{ color: 'var(--text-muted)', fontSize: '0.82rem' }}>
            {live ? 'Waiting for Claude Code to start…' : 'No activity was recorded.'}
          </div>
        )}
      </div>
    </div>
  );
}

function Row({ e }: { e: AgentEvent }) {
  const [open, setOpen] = useState(false);
  const row: CSSProperties = {
    display: 'flex', gap: 8, alignItems: 'flex-start', padding: '3px 0', fontSize: '0.82rem', lineHeight: 1.55,
    marginLeft: e.sub ? 22 : 0, paddingLeft: e.sub ? 10 : 0,
    borderLeft: e.sub ? '2px solid var(--border-color)' : 'none',
  };
  const icon: CSSProperties = { flexShrink: 0, marginTop: 3 };
  const title = e.sub ? 'A helper agent\'s step' : undefined;

  if (e.kind === 'info') {
    return (
      <div style={{ ...row, color: 'var(--text-muted)' }} title={title}>
        <Info size={13} style={icon} /><span style={wrapText}>{e.text}</span>
      </div>
    );
  }
  if (e.kind === 'end') {
    return (
      <div style={{ ...row, color: e.error ? RED : GREEN, fontWeight: 600, marginTop: 4 }} title={title}>
        {e.error ? <XCircle size={13} style={icon} /> : <CheckCircle2 size={13} style={icon} />}
        <span style={{ whiteSpace: 'pre-wrap', ...wrapText }}>{e.text}</span>
      </div>
    );
  }
  if (e.kind === 'tool') {
    return (
      <div style={{ ...row, color: e.error ? RED : 'var(--text-secondary)' }} title={title}>
        <Terminal size={13} style={icon} />
        <span style={wrapText}>
          {e.tool && <span style={{ ...mono, color: 'var(--accent-primary)', marginRight: 6 }}>{e.tool}</span>}
          <span style={{ ...mono, ...wrapText }}>{e.text}</span>
        </span>
      </div>
    );
  }
  if (e.kind === 'result') {
    const text = e.text || '(no output)';
    const lines = text.split('\n');
    const long = lines.length > 1 || lines[0].length > 160;
    return (
      <div style={{ ...row, color: e.error ? RED : 'var(--text-muted)', paddingLeft: (e.sub ? 10 : 0) + 21 }} title={title}>
        <button onClick={() => long && setOpen(o => !o)}
                style={{ background: 'none', border: 'none', padding: 0, color: 'inherit', cursor: long ? 'pointer' : 'default',
                         font: 'inherit', textAlign: 'left', display: 'flex', gap: 6, alignItems: 'flex-start', minWidth: 0, flex: 1 }}>
          {long ? (open ? <ChevronDown size={12} style={icon} /> : <ChevronRight size={12} style={icon} />)
            : <span style={{ width: 12, flexShrink: 0 }} />}
          {open ? (
            <pre style={{ ...pre, flex: 1, padding: '6px 10px', color: 'inherit', background: 'rgba(0,0,0,0.25)' }}>{text}</pre>
          ) : (
            <span style={{ ...mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
              {lines[0]}
            </span>
          )}
        </button>
      </div>
    );
  }
  // text — Claude explaining what it is doing
  return (
    <div style={{ ...row, color: 'var(--text-primary)', marginTop: 6, marginBottom: 6 }} title={title}>
      <span style={{ width: 13, flexShrink: 0 }} />
      <span style={{ whiteSpace: 'pre-wrap', ...wrapText }}>{e.text}</span>
    </div>
  );
}
