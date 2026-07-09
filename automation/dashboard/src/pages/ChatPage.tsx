import { useState, useEffect, useRef, useCallback } from 'react';
import { MessageSquare, Plus, Trash2, Send, Bot, Loader2, AlertTriangle } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import ChatMessage from '../components/ChatMessage';
import type { StructuredPayload } from '../components/ChatMessage';
import { getSessions, getSession, deleteSession, getApiBase, getHeaders } from '../api';

// ── Types ─────────────────────────────────────────────────────────────────────

interface Message {
  role: 'user' | 'assistant';
  content: string;
  messageType: 'text' | 'structured';
  isStreaming?: boolean;
}

interface SessionSummary {
  session_id: string;
  title: string;
  created_at: string;
  message_count: number;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const SUGGESTIONS = [
  'Why did my last test run fail?',
  'Which tests are flaky right now?',
  'Show me the most common failure pattern',
  'Generate a test for the login screen',
  'What changed in the last 5 runs?',
];

function genSessionId(): string {
  return crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2);
}

// ── Session sidebar item ──────────────────────────────────────────────────────

function SessionItem({
  session,
  isActive,
  onSelect,
  onDelete,
}: {
  session: SessionSummary;
  isActive: boolean;
  onSelect: () => void;
  onDelete: () => void;
}) {
  const [hovered, setHovered] = useState(false);

  const relTime = session.created_at
    ? formatDistanceToNow(new Date(session.created_at), { addSuffix: true })
    : '';

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onClick={onSelect}
      style={{
        padding: '10px 12px',
        borderRadius: 'var(--radius-sm)',
        cursor: 'pointer',
        background: isActive ? 'rgba(129,140,248,0.12)' : 'transparent',
        borderLeft: isActive ? '2px solid var(--accent-primary)' : '2px solid transparent',
        display: 'flex',
        flexDirection: 'column',
        gap: '3px',
        position: 'relative',
        transition: 'background 0.15s',
        marginBottom: '2px',
      }}
    >
      <div
        style={{
          fontSize: '0.82rem',
          color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)',
          fontWeight: isActive ? 500 : 400,
          overflow: 'hidden',
          whiteSpace: 'nowrap',
          textOverflow: 'ellipsis',
          paddingRight: hovered ? '20px' : '0',
        }}
      >
        {session.title || 'Untitled chat'}
      </div>
      <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>{relTime}</div>

      {/* Trash icon on hover */}
      {hovered && (
        <button
          onClick={e => { e.stopPropagation(); onDelete(); }}
          title="Delete session"
          style={{
            position: 'absolute',
            right: 8,
            top: '50%',
            transform: 'translateY(-50%)',
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
            color: 'var(--danger)',
            padding: '2px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <Trash2 size={13} />
        </button>
      )}
    </div>
  );
}

// ── Suggestion chips ──────────────────────────────────────────────────────────

function SuggestionChips({
  visible,
  onSelect,
}: {
  visible: boolean;
  onSelect: (text: string) => void;
}) {
  if (!visible) return null;
  return (
    <div
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: '8px',
        padding: '0 0 16px',
        justifyContent: 'center',
        animation: 'fadeIn 0.4s ease forwards',
      }}
    >
      {SUGGESTIONS.map(s => (
        <button
          key={s}
          onClick={() => onSelect(s)}
          style={{
            background: 'rgba(129,140,248,0.08)',
            border: '1px solid rgba(129,140,248,0.25)',
            borderRadius: '99px',
            padding: '6px 14px',
            fontSize: '0.8rem',
            color: 'var(--accent-primary)',
            cursor: 'pointer',
            transition: 'all 0.2s',
            fontFamily: 'inherit',
            whiteSpace: 'nowrap',
          }}
          onMouseEnter={e => {
            e.currentTarget.style.background = 'rgba(129,140,248,0.18)';
            e.currentTarget.style.transform = 'translateY(-1px)';
          }}
          onMouseLeave={e => {
            e.currentTarget.style.background = 'rgba(129,140,248,0.08)';
            e.currentTarget.style.transform = 'translateY(0)';
          }}
        >
          {s}
        </button>
      ))}
    </div>
  );
}

// ── Main ChatPage ─────────────────────────────────────────────────────────────

export default function ChatPage() {
  // Session management
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>(() => {
    // Session ID is not chat content — sessionStorage is acceptable here.
    return sessionStorage.getItem('chat_session_id') || genSessionId();
  });

  // Messages for the current session
  const [messages, setMessages] = useState<Message[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const [error, setError] = useState<string | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const esRef = useRef<EventSource | null>(null);

  // Persist active session id
  useEffect(() => {
    sessionStorage.setItem('chat_session_id', activeSessionId);
  }, [activeSessionId]);

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Load sessions list
  const loadSessions = useCallback(async () => {
    try {
      const data = await getSessions();
      setSessions(data);
    } catch {
      // Non-critical — sidebar just stays empty
    }
  }, []);

  useEffect(() => { loadSessions(); }, [loadSessions]);

  // Load history when switching sessions
  useEffect(() => {
    setMessages([]);
    setError(null);
    if (!activeSessionId) return;

    getSession(activeSessionId)
      .then(data => {
        if (data?.messages) {
          setMessages(
            data.messages.map((m: any) => ({
              role: m.role,
              content: m.content,
              messageType: m.message_type || 'text',
              isStreaming: false,
            })),
          );
        }
      })
      .catch(() => {
        // New session with no history yet — that's fine
      });
  }, [activeSessionId]);

  // ── New chat ────────────────────────────────────────────────────────────────
  const handleNewChat = () => {
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
    const id = genSessionId();
    setActiveSessionId(id);
    setMessages([]);
    setInputValue('');
    setError(null);
    setIsStreaming(false);
  };

  // ── Delete session ──────────────────────────────────────────────────────────
  const handleDeleteSession = async (sessionId: string) => {
    try {
      await deleteSession(sessionId);
      if (sessionId === activeSessionId) handleNewChat();
      loadSessions();
    } catch {
      // Silently ignore
    }
  };

  // ── Send message ────────────────────────────────────────────────────────────
  const sendMessage = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || isStreaming) return;

      setError(null);
      setInputValue('');
      setIsStreaming(true);

      // Add user message immediately
      setMessages(prev => [
        ...prev,
        { role: 'user', content: trimmed, messageType: 'text' },
      ]);

      // Add placeholder assistant message
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: '', messageType: 'text', isStreaming: true },
      ]);

      // Close any previous SSE connection
      if (esRef.current) { esRef.current.close(); esRef.current = null; }

      // Build the SSE URL (EventSource requires GET)
      const params = new URLSearchParams({
        session_id: activeSessionId,
        message: trimmed,
      });

      // Attach JWT — EventSource doesn't support custom headers.
      // We pass via a custom query param that the browser includes in the request.
      // The backend validates via the standard Authorization mechanism, but since
      // EventSource can't set headers, we use a workaround: send the token as
      // a query param and the backend reads it there for SSE routes only.
      const token = (window as any).__authToken__ || localStorage.getItem('access_token') || '';
      if (token) params.set('token', token);

      const sseUrl = `${getApiBase()}/intelligence/chat/stream?${params.toString()}`;

      let accumulated = '';
      let msgType: 'text' | 'structured' = 'text';

      const trySSE = () => {
        const es = new EventSource(sseUrl);
        esRef.current = es;

        es.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);

            // Structured response (one-shot event)
            if (data.type === 'structured' && data.done) {
              msgType = 'structured';
              const payloadStr = JSON.stringify(data.payload);
              setMessages(prev => {
                const updated = [...prev];
                const lastIdx = updated.length - 1;
                updated[lastIdx] = {
                  ...updated[lastIdx],
                  content: payloadStr,
                  messageType: 'structured',
                  isStreaming: false,
                };
                return updated;
              });
              es.close();
              setIsStreaming(false);
              loadSessions();
              return;
            }

            // Normal streaming token
            if (typeof data.token === 'string') {
              accumulated += data.token;
              setMessages(prev => {
                const updated = [...prev];
                const lastIdx = updated.length - 1;
                updated[lastIdx] = {
                  ...updated[lastIdx],
                  content: accumulated,
                  isStreaming: !data.done,
                };
                return updated;
              });

              if (data.done) {
                es.close();
                setIsStreaming(false);
                loadSessions();
              }
            }
          } catch {
            // Ignore malformed SSE events
          }
        };

        es.onerror = () => {
          es.close();
          esRef.current = null;

          if (accumulated) {
            // Already received some content — just mark streaming done
            setMessages(prev => {
              const updated = [...prev];
              const lastIdx = updated.length - 1;
              updated[lastIdx] = { ...updated[lastIdx], isStreaming: false };
              return updated;
            });
            setIsStreaming(false);
            loadSessions();
          } else {
            // No content received — fall back to regular POST
            fallbackPost(trimmed);
          }
        };
      };

      const fallbackPost = async (userMsg: string) => {
        try {
          const res = await fetch(`${getApiBase()}/intelligence/chat`, {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify({ query: userMsg }),
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const data = await res.json();
          const reply = data.reply || 'No response.';
          setMessages(prev => {
            const updated = [...prev];
            const lastIdx = updated.length - 1;
            updated[lastIdx] = {
              ...updated[lastIdx],
              content: reply,
              isStreaming: false,
            };
            return updated;
          });
          loadSessions();
        } catch (e) {
          setError('Failed to get a response. Please check that the backend is running.');
          setMessages(prev => {
            const updated = [...prev];
            const lastIdx = updated.length - 1;
            updated[lastIdx] = {
              ...updated[lastIdx],
              content: '⚠️ Failed to get a response.',
              isStreaming: false,
            };
            return updated;
          });
        } finally {
          setIsStreaming(false);
        }
      };

      trySSE();
    },
    [activeSessionId, isStreaming, loadSessions],
  );

  // ── Suggestion click ────────────────────────────────────────────────────────
  const handleSuggestion = (text: string) => {
    sendMessage(text);
  };

  // ── Keyboard handler ────────────────────────────────────────────────────────
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage(inputValue);
    }
  };

  // Show suggestions when: no messages, OR last message is a completed assistant reply
  const lastMsg = messages[messages.length - 1];
  const showSuggestions =
    !isStreaming &&
    (messages.length === 0 ||
      (lastMsg?.role === 'assistant' && !lastMsg?.isStreaming));

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <div
      className="animate-fade-in"
      style={{ height: 'calc(100vh - 96px)', display: 'flex', gap: '0', overflow: 'hidden' }}
    >
      {/* ── LEFT: Session sidebar ─────────────────────────────────────── */}
      <div
        style={{
          width: 240,
          flexShrink: 0,
          borderRight: '1px solid var(--border-color)',
          display: 'flex',
          flexDirection: 'column',
          background: 'rgba(8,8,12,0.5)',
        }}
      >
        {/* Sidebar header */}
        <div
          style={{
            padding: '16px',
            borderBottom: '1px solid var(--border-color)',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          <MessageSquare size={16} color="var(--accent-primary)" />
          <span
            style={{ fontWeight: 600, fontSize: '0.875rem', color: 'var(--text-primary)' }}
          >
            Chat History
          </span>
        </div>

        {/* New Chat button */}
        <div style={{ padding: '12px' }}>
          <button
            onClick={handleNewChat}
            style={{
              width: '100%',
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              padding: '8px 12px',
              background: 'linear-gradient(135deg, var(--accent-primary), var(--accent-hover))',
              border: 'none',
              borderRadius: 'var(--radius-sm)',
              color: '#fff',
              fontSize: '0.82rem',
              fontWeight: 600,
              cursor: 'pointer',
              fontFamily: 'inherit',
              boxShadow: '0 2px 8px var(--accent-glow)',
              transition: 'all 0.2s',
            }}
            onMouseEnter={e => (e.currentTarget.style.transform = 'translateY(-1px)')}
            onMouseLeave={e => (e.currentTarget.style.transform = 'translateY(0)')}
          >
            <Plus size={14} /> New Chat
          </button>
        </div>

        {/* Session list */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '0 8px 8px' }}>
          {sessions.length === 0 ? (
            <p
              style={{
                color: 'var(--text-muted)',
                fontSize: '0.75rem',
                textAlign: 'center',
                padding: '24px 8px',
              }}
            >
              No previous chats
            </p>
          ) : (
            sessions.map(s => (
              <SessionItem
                key={s.session_id}
                session={s}
                isActive={s.session_id === activeSessionId}
                onSelect={() => setActiveSessionId(s.session_id)}
                onDelete={() => handleDeleteSession(s.session_id)}
              />
            ))
          )}
        </div>
      </div>

      {/* ── RIGHT: Chat area ──────────────────────────────────────────── */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          minWidth: 0,
          background: 'rgba(10,10,15,0.3)',
        }}
      >
        {/* Chat header */}
        <div
          style={{
            padding: '16px 24px',
            borderBottom: '1px solid var(--border-color)',
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
            flexShrink: 0,
          }}
        >
          <div
            style={{
              width: 32,
              height: 32,
              borderRadius: '50%',
              background: 'linear-gradient(135deg, var(--accent-primary), #6366f1)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              boxShadow: '0 0 12px var(--accent-glow)',
            }}
          >
            <Bot size={16} color="#fff" />
          </div>
          <div>
            <div style={{ fontWeight: 600, fontSize: '0.95rem' }}>AI Test Assistant</div>
            <div style={{ fontSize: '0.72rem', color: isStreaming ? 'var(--success)' : 'var(--text-muted)' }}>
              {isStreaming ? '● Responding…' : '● Ready'}
            </div>
          </div>
        </div>

        {/* Error banner */}
        {error && (
          <div
            style={{
              background: 'var(--danger-bg)',
              border: '1px solid rgba(248,113,113,0.2)',
              color: 'var(--danger)',
              padding: '10px 24px',
              fontSize: '0.85rem',
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              flexShrink: 0,
            }}
          >
            <AlertTriangle size={14} />
            {error}
          </div>
        )}

        {/* Messages */}
        <div
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: '16px 24px',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          {/* Welcome state */}
          {messages.length === 0 && (
            <div
              style={{
                flex: 1,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '12px',
                paddingBottom: '32px',
              }}
            >
              <div
                style={{
                  width: 64,
                  height: 64,
                  borderRadius: '50%',
                  background: 'linear-gradient(135deg, var(--accent-primary), #6366f1)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  boxShadow: '0 0 32px var(--accent-glow)',
                  marginBottom: '8px',
                }}
              >
                <Bot size={28} color="#fff" />
              </div>
              <h2 style={{ fontFamily: "'Outfit', sans-serif", fontWeight: 700, margin: 0 }}>
                AI Test Intelligence
              </h2>
              <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', margin: 0, textAlign: 'center' }}>
                Ask me about test failures, flaky tests, or request an analysis.
              </p>
            </div>
          )}

          {/* Message list */}
          {messages.map((msg, i) => (
            <ChatMessage
              key={i}
              role={msg.role}
              content={msg.content}
              messageType={msg.messageType}
              isStreaming={msg.isStreaming}
            />
          ))}

          <div ref={messagesEndRef} />
        </div>

        {/* Suggestion chips */}
        <div style={{ padding: '0 24px', flexShrink: 0 }}>
          <SuggestionChips visible={showSuggestions} onSelect={handleSuggestion} />
        </div>

        {/* Input bar */}
        <div
          style={{
            padding: '12px 24px 16px',
            borderTop: '1px solid var(--border-color)',
            flexShrink: 0,
          }}
        >
          <div
            style={{
              display: 'flex',
              gap: '10px',
              alignItems: 'flex-end',
              background: 'rgba(28,29,43,0.6)',
              border: `1px solid ${isStreaming ? 'rgba(129,140,248,0.3)' : 'var(--border-highlight)'}`,
              borderRadius: 'var(--radius-md)',
              padding: '8px 12px',
              backdropFilter: 'blur(8px)',
              transition: 'border-color 0.2s',
            }}
          >
            <textarea
              ref={inputRef}
              value={inputValue}
              onChange={e => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={isStreaming}
              placeholder="Ask about your tests… (Enter to send, Shift+Enter for newline)"
              rows={1}
              style={{
                flex: 1,
                background: 'transparent',
                border: 'none',
                outline: 'none',
                color: 'var(--text-primary)',
                fontSize: '0.9rem',
                fontFamily: 'inherit',
                resize: 'none',
                lineHeight: 1.5,
                maxHeight: '120px',
                overflowY: 'auto',
                padding: '4px 0',
              }}
              onInput={e => {
                const el = e.currentTarget;
                el.style.height = 'auto';
                el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
              }}
            />
            <button
              onClick={() => sendMessage(inputValue)}
              disabled={!inputValue.trim() || isStreaming}
              style={{
                width: 36,
                height: 36,
                flexShrink: 0,
                borderRadius: '50%',
                background:
                  inputValue.trim() && !isStreaming
                    ? 'linear-gradient(135deg, var(--accent-primary), var(--accent-hover))'
                    : 'rgba(255,255,255,0.06)',
                border: 'none',
                cursor: inputValue.trim() && !isStreaming ? 'pointer' : 'not-allowed',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: inputValue.trim() && !isStreaming ? '#fff' : 'var(--text-muted)',
                transition: 'all 0.2s',
                boxShadow:
                  inputValue.trim() && !isStreaming ? '0 2px 8px var(--accent-glow)' : 'none',
              }}
            >
              {isStreaming ? (
                <Loader2 size={16} style={{ animation: 'cp-spin 1s linear infinite' }} />
              ) : (
                <Send size={16} />
              )}
            </button>
          </div>
          <p
            style={{
              fontSize: '0.7rem',
              color: 'var(--text-muted)',
              marginTop: '6px',
              textAlign: 'center',
            }}
          >
            AI responses may contain errors. Verify critical information independently.
          </p>
        </div>
      </div>

      <style>{`
        @keyframes cp-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
      `}</style>
    </div>
  );
}
