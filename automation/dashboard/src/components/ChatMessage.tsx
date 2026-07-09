import { useState } from 'react';
import { Copy, Check, TrendingUp, TrendingDown, Minus, Bot, User } from 'lucide-react';

// ── Types ─────────────────────────────────────────────────────────────────────

export interface StructuredPayload {
  type: 'analysis';
  cards: Array<{
    title: string;
    value: string;
    trend: 'up' | 'down' | 'stable';
    detail: string;
  }>;
  recommendation: string;
}

export interface ChatMessageProps {
  role: 'user' | 'assistant';
  content: string;
  messageType?: 'text' | 'structured';
  /** True while this message is still being streamed */
  isStreaming?: boolean;
}

// ── Minimal in-house markdown renderer ───────────────────────────────────────
// Handles: **bold**, `code`, and newlines → <br>. No external library needed.

function renderMarkdown(text: string): React.ReactNode[] {
  // Split on **bold** and `code` spans while preserving delimiters for matching
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);

  const nodes: React.ReactNode[] = [];
  let key = 0;

  for (const part of parts) {
    if (part.startsWith('**') && part.endsWith('**')) {
      nodes.push(<strong key={key++}>{part.slice(2, -2)}</strong>);
    } else if (part.startsWith('`') && part.endsWith('`')) {
      nodes.push(
        <code
          key={key++}
          style={{
            background: 'rgba(0,0,0,0.4)',
            padding: '2px 6px',
            borderRadius: '4px',
            fontFamily: "'Fira Code', monospace",
            fontSize: '0.88em',
            color: '#fbbf24',
            border: '1px solid rgba(255,255,255,0.06)',
          }}
        >
          {part.slice(1, -1)}
        </code>,
      );
    } else {
      // Render newlines as <br> elements
      const lines = part.split('\n');
      lines.forEach((line, i) => {
        nodes.push(<span key={key++}>{line}</span>);
        if (i < lines.length - 1) nodes.push(<br key={key++} />);
      });
    }
  }
  return nodes;
}

// ── Structured card renderer ──────────────────────────────────────────────────

function TrendIcon({ trend }: { trend: 'up' | 'down' | 'stable' }) {
  if (trend === 'up')
    return <TrendingUp size={14} style={{ color: 'var(--danger)' }} />;
  if (trend === 'down')
    return <TrendingDown size={14} style={{ color: 'var(--success)' }} />;
  return <Minus size={14} style={{ color: 'var(--text-muted)' }} />;
}

function StructuredCards({ payload }: { payload: StructuredPayload }) {
  return (
    <div>
      {/* KPI grid */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(2, 1fr)',
          gap: '12px',
          marginBottom: '16px',
        }}
      >
        {payload.cards.map((card, i) => (
          <div
            key={i}
            style={{
              background: 'rgba(28,29,43,0.6)',
              border: '1px solid var(--border-color)',
              borderRadius: 'var(--radius-md)',
              padding: '16px',
              backdropFilter: 'blur(8px)',
            }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: '8px',
              }}
            >
              <span
                style={{
                  fontSize: '0.75rem',
                  color: 'var(--text-muted)',
                  textTransform: 'uppercase',
                  letterSpacing: '0.06em',
                  fontWeight: 600,
                }}
              >
                {card.title}
              </span>
              <TrendIcon trend={card.trend} />
            </div>
            <div
              className="stat-value"
              style={{ fontSize: '1.5rem', marginTop: 0, marginBottom: '6px' }}
            >
              {card.value}
            </div>
            <p
              style={{
                fontSize: '0.8rem',
                color: 'var(--text-secondary)',
                margin: 0,
                lineHeight: 1.4,
              }}
            >
              {card.detail}
            </p>
          </div>
        ))}
      </div>

      {/* Recommendation blockquote */}
      {payload.recommendation && (
        <div
          style={{
            borderLeft: '3px solid var(--accent-primary)',
            paddingLeft: '16px',
            paddingTop: '8px',
            paddingBottom: '8px',
            background: 'rgba(129,140,248,0.06)',
            borderRadius: '0 var(--radius-sm) var(--radius-sm) 0',
          }}
        >
          <p
            style={{
              fontSize: '0.85rem',
              color: 'var(--text-secondary)',
              margin: 0,
              lineHeight: 1.6,
            }}
          >
            <strong style={{ color: 'var(--accent-primary)' }}>
              Recommendation:{' '}
            </strong>
            {payload.recommendation}
          </p>
        </div>
      )}
    </div>
  );
}

// ── Main ChatMessage component ────────────────────────────────────────────────

export default function ChatMessage({
  role,
  content,
  messageType = 'text',
  isStreaming = false,
}: ChatMessageProps) {
  const [copied, setCopied] = useState(false);

  const isAssistant = role === 'assistant';

  const handleCopy = async () => {
    const plain =
      messageType === 'structured'
        ? (() => {
            try {
              const p = JSON.parse(content) as StructuredPayload;
              const cardLines = p.cards
                .map(c => `${c.title}: ${c.value} — ${c.detail}`)
                .join('\n');
              return `${cardLines}\n\nRecommendation: ${p.recommendation}`;
            } catch {
              return content;
            }
          })()
        : content;

    try {
      await navigator.clipboard.writeText(plain);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API not available — silently ignore
    }
  };

  // Parse structured payload if applicable
  let structuredPayload: StructuredPayload | null = null;
  if (messageType === 'structured') {
    try {
      structuredPayload = JSON.parse(content) as StructuredPayload;
    } catch {
      // Fall back to plain text render if JSON is malformed
    }
  }

  return (
    <div
      style={{
        display: 'flex',
        gap: '12px',
        padding: '12px 0',
        alignItems: 'flex-start',
        flexDirection: isAssistant ? 'row' : 'row-reverse',
        animation: 'fadeIn 0.3s ease forwards',
      }}
    >
      {/* Avatar */}
      <div
        style={{
          width: 34,
          height: 34,
          borderRadius: '50%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
          background: isAssistant
            ? 'linear-gradient(135deg, var(--accent-primary), #6366f1)'
            : 'rgba(255,255,255,0.08)',
          border: '1px solid var(--border-color)',
          boxShadow: isAssistant ? '0 0 12px var(--accent-glow)' : 'none',
        }}
      >
        {isAssistant ? (
          <Bot size={16} color="#fff" />
        ) : (
          <User size={16} color="var(--text-secondary)" />
        )}
      </div>

      {/* Bubble */}
      <div
        style={{
          maxWidth: '80%',
          minWidth: '60px',
          position: 'relative',
        }}
      >
        <div
          style={{
            background: isAssistant
              ? 'rgba(28,29,43,0.6)'
              : 'rgba(129,140,248,0.12)',
            border: `1px solid ${isAssistant ? 'var(--border-color)' : 'rgba(129,140,248,0.25)'}`,
            borderRadius: isAssistant
              ? '4px var(--radius-md) var(--radius-md) var(--radius-md)'
              : 'var(--radius-md) 4px var(--radius-md) var(--radius-md)',
            padding: '12px 16px',
            backdropFilter: 'blur(8px)',
            fontSize: '0.9rem',
            lineHeight: 1.65,
            color: 'var(--text-primary)',
            wordBreak: 'break-word',
          }}
        >
          {/* Content */}
          {structuredPayload ? (
            <StructuredCards payload={structuredPayload} />
          ) : (
            <span>
              {renderMarkdown(content)}
              {/* Blinking cursor while streaming */}
              {isStreaming && (
                <span
                  style={{
                    display: 'inline-block',
                    width: '2px',
                    height: '1em',
                    background: 'var(--accent-primary)',
                    marginLeft: '2px',
                    verticalAlign: 'text-bottom',
                    animation: 'cm-blink 1s step-end infinite',
                  }}
                />
              )}
            </span>
          )}
        </div>

        {/* Copy button — only for completed assistant messages */}
        {isAssistant && !isStreaming && (
          <button
            onClick={handleCopy}
            title={copied ? 'Copied!' : 'Copy to clipboard'}
            style={{
              position: 'absolute',
              top: 8,
              right: 8,
              background: 'transparent',
              border: 'none',
              cursor: 'pointer',
              color: copied ? 'var(--success)' : 'var(--text-muted)',
              display: 'flex',
              alignItems: 'center',
              padding: '2px',
              borderRadius: '4px',
              transition: 'color 0.2s',
              opacity: 0.7,
            }}
            onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
            onMouseLeave={e => (e.currentTarget.style.opacity = '0.7')}
          >
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
        )}
      </div>

      {/* Keyframe definitions (scoped to component, injected once) */}
      <style>{`
        @keyframes cm-blink {
          0%, 100% { opacity: 1; }
          50%       { opacity: 0; }
        }
      `}</style>
    </div>
  );
}
