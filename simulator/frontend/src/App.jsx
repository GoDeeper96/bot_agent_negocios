import { useState, useRef, useEffect, useCallback } from 'react'

const SESSION_ID = 'demo-' + Math.random().toString(36).slice(2, 8)
const API = ''  // proxied via vite to localhost:8765

const OPTION_META = {
  '1':  { emoji: '🧾', label: 'Factura',           desc: 'Emitir factura electrónica' },
  '2':  { emoji: '📄', label: 'Cotización',         desc: 'Crear cotización en PDF' },
  '3':  { emoji: '🚚', label: 'Guía de Remisión',   desc: 'Generar guía de traslado' },
  '4':  { emoji: '📚', label: 'Ver ejemplos',        desc: 'Ejemplos de cada documento' },
  '5':  { emoji: '📋', label: 'Orden de Compra',     desc: 'Emitir orden de compra' },
  '6':  { emoji: '📦', label: 'Movimiento Stock',    desc: 'Registrar ingreso o salida' },
  'sí': { emoji: '✅', label: 'Confirmar',           desc: 'Sí, proceder' },
  'no': { emoji: '❌', label: 'Cancelar',            desc: 'No, descartar' },
}

// ─── Text formatters ──────────────────────────────────────────────────────────

function WhatsAppText({ text }) {
  return (
    <span>
      {text.split('\n').map((line, li) => (
        <span key={li}>
          {li > 0 && <br />}
          <InlineFormat text={line} />
        </span>
      ))}
    </span>
  )
}

function InlineFormat({ text }) {
  const parts = []
  let remaining = text
  let key = 0
  while (remaining.length > 0) {
    const codeMatch = remaining.match(/^(.*?)```([\s\S]*?)```(.*)$/)
    if (codeMatch) {
      if (codeMatch[1]) parts.push(<span key={key++}><InlineBoldItalic text={codeMatch[1]} /></span>)
      parts.push(<code key={key++} style={styles.code}>{codeMatch[2]}</code>)
      remaining = codeMatch[3]
      continue
    }
    parts.push(<span key={key++}><InlineBoldItalic text={remaining} /></span>)
    break
  }
  return <>{parts}</>
}

function InlineBoldItalic({ text }) {
  const segments = []
  const regex = /(\*[^*]+\*|_[^_]+_)/g
  let last = 0, m, key = 0
  while ((m = regex.exec(text)) !== null) {
    if (m.index > last) segments.push(<span key={key++}>{text.slice(last, m.index)}</span>)
    const inner = m[0].slice(1, -1)
    if (m[0].startsWith('*')) segments.push(<strong key={key++}>{inner}</strong>)
    else segments.push(<em key={key++}>{inner}</em>)
    last = m.index + m[0].length
  }
  if (last < text.length) segments.push(<span key={key++}>{text.slice(last)}</span>)
  return <>{segments}</>
}

// ─── Chat bubbles ─────────────────────────────────────────────────────────────

function Bubble({ msg }) {
  if (msg.from === 'info') {
    return (
      <div style={styles.infoBubble}>
        <span>{msg.text}</span>
      </div>
    )
  }
  const isUser = msg.from === 'user'
  return (
    <div style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start', marginBottom: 2 }}>
      <div style={isUser ? styles.userBubble : styles.botBubble}>
        <WhatsAppText text={msg.text} />
        <div style={styles.time}>{msg.time}</div>
      </div>
    </div>
  )
}

function TypingIndicator() {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 4 }}>
      <div style={{ ...styles.botBubble, padding: '10px 14px' }}>
        <div style={styles.dots}>
          <span style={styles.dot} /><span style={{ ...styles.dot, animationDelay: '0.2s' }} /><span style={{ ...styles.dot, animationDelay: '0.4s' }} />
        </div>
      </div>
    </div>
  )
}

// ─── Options modal (carousel) ─────────────────────────────────────────────────

function OptionsModal({ replies, onSelect, onClose }) {
  return (
    <div style={styles.overlay} onClick={onClose}>
      <div style={styles.sheet} onClick={e => e.stopPropagation()}>
        <div style={styles.sheetHandle} />
        <p style={styles.sheetTitle}>Elige una opción</p>
        <div style={styles.carousel}>
          {replies.map(r => {
            const meta = OPTION_META[r] || { emoji: '▶️', label: r, desc: '' }
            return (
              <button key={r} style={styles.card} onClick={() => onSelect(r)}>
                <span style={styles.cardEmoji}>{meta.emoji}</span>
                <span style={styles.cardLabel}>{meta.label}</span>
                {meta.desc && <span style={styles.cardDesc}>{meta.desc}</span>}
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ─── Main app ─────────────────────────────────────────────────────────────────

export default function App() {
  const [messages, setMessages] = useState([
    { from: 'info', text: 'Simulador del bot — escribe cualquier mensaje para empezar', time: '' },
  ])
  const [input, setInput]         = useState('')
  const [loading, setLoading]     = useState(false)
  const [state, setState]         = useState('idle')
  const [showModal, setShowModal] = useState(false)
  const bottomRef = useRef(null)
  const inputRef  = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  const send = useCallback(async (text) => {
    if (!text.trim() || loading) return
    const now = new Date().toLocaleTimeString('es-PE', { hour: '2-digit', minute: '2-digit' })
    setMessages(prev => [...prev, { from: 'user', text: text.trim(), time: now }])
    setInput('')
    setLoading(true)
    setShowModal(false)
    try {
      const res  = await fetch(`${API}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: SESSION_ID, text: text.trim() }),
      })
      const data = await res.json()
      setState(data.state)
      const replyTime = new Date().toLocaleTimeString('es-PE', { hour: '2-digit', minute: '2-digit' })
      setMessages(prev => [
        ...prev,
        ...(data.messages || []).map(m => ({ from: 'bot', text: m, time: replyTime })),
      ])
    } catch {
      setMessages(prev => [...prev, {
        from: 'bot',
        text: '❌ Error conectando al servidor. ¿Está corriendo el backend?\n\ncd simulator && uvicorn api:app --reload --port 8765',
        time: now,
      }])
    } finally {
      setLoading(false)
      inputRef.current?.focus()
    }
  }, [loading])

  const handleReset = useCallback(async () => {
    await fetch(`${API}/chat/${SESSION_ID}`, { method: 'DELETE' }).catch(() => {})
    setMessages([{ from: 'info', text: 'Sesión reiniciada', time: '' }])
    setState('idle')
    setShowModal(false)
    inputRef.current?.focus()
  }, [])

  const handleKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input) }
  }

  const quickReplies =
    state === 'confirming' || state === 'email_preview' || state === 'oc_email_preview' || state === 'stock_confirming'
      ? ['sí', 'no']
      : state === 'idle' ? ['1', '2', '3', '4', '5', '6'] : []

  return (
    <div style={styles.root}>
      {/* Header */}
      <div style={styles.header}>
        <div style={styles.avatar}>L</div>
        <div>
          <div style={styles.headerName}>Bot Lichan</div>
          <div style={styles.headerSub}>
            {loading ? '⌛ escribiendo...' : `estado: ${state}`}
          </div>
        </div>
        <button onClick={handleReset} style={styles.resetBtn}>↺ Reset</button>
      </div>

      {/* Chat */}
      <div style={styles.chat}>
        {messages.map((msg, i) => <Bubble key={i} msg={msg} />)}
        {loading && <TypingIndicator />}
        <div ref={bottomRef} />
      </div>

      {/* Input bar */}
      <div style={styles.inputBar}>
        {quickReplies.length > 0 && (
          <button style={styles.optionsBtn} onClick={() => setShowModal(true)} title="Ver opciones rápidas">
            ⚡
          </button>
        )}
        <textarea
          ref={inputRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKey}
          placeholder="Escribe un mensaje..."
          rows={1}
          style={styles.textarea}
          disabled={loading}
        />
        <button
          onClick={() => send(input)}
          disabled={!input.trim() || loading}
          style={styles.sendBtn}
        >
          ➤
        </button>
      </div>

      {/* Options modal */}
      {showModal && quickReplies.length > 0 && (
        <OptionsModal
          replies={quickReplies}
          onSelect={send}
          onClose={() => setShowModal(false)}
        />
      )}

      <style>{`
        @keyframes bounce {
          0%, 80%, 100% { transform: scale(0); opacity: 0.4; }
          40% { transform: scale(1); opacity: 1; }
        }
        @keyframes slideUp {
          from { transform: translateY(100%); }
          to   { transform: translateY(0); }
        }
      `}</style>
    </div>
  )
}

// ─── Styles ───────────────────────────────────────────────────────────────────

const styles = {
  root: {
    display: 'flex', flexDirection: 'column',
    height: '100dvh', maxWidth: 680, margin: '0 auto',
    background: '#0b141a', position: 'relative',
  },
  header: {
    display: 'flex', alignItems: 'center', gap: 12,
    padding: '10px 16px', background: '#202c33',
    borderBottom: '1px solid #374045',
  },
  avatar: {
    width: 40, height: 40, borderRadius: '50%',
    background: '#00a884', display: 'flex',
    alignItems: 'center', justifyContent: 'center',
    fontWeight: 700, fontSize: 18, color: '#fff', flexShrink: 0,
  },
  headerName: { fontWeight: 600, fontSize: 15, color: '#e9edef' },
  headerSub:  { fontSize: 12, color: '#8696a0' },
  resetBtn: {
    marginLeft: 'auto', background: 'transparent',
    border: '1px solid #374045', color: '#8696a0',
    padding: '4px 10px', borderRadius: 6, cursor: 'pointer', fontSize: 13,
  },
  chat: {
    flex: 1, overflowY: 'auto', padding: '12px 16px',
    display: 'flex', flexDirection: 'column', gap: 2, background: '#0b141a',
  },
  botBubble: {
    maxWidth: '80%', background: '#202c33', color: '#e9edef',
    borderRadius: '0 8px 8px 8px', padding: '8px 12px',
    fontSize: 14, lineHeight: 1.5, wordBreak: 'break-word', whiteSpace: 'pre-wrap',
  },
  userBubble: {
    maxWidth: '80%', background: '#005c4b', color: '#e9edef',
    borderRadius: '8px 0 8px 8px', padding: '8px 12px',
    fontSize: 14, lineHeight: 1.5, wordBreak: 'break-word', whiteSpace: 'pre-wrap',
  },
  infoBubble: {
    alignSelf: 'center', background: '#182229', color: '#8696a0',
    borderRadius: 8, padding: '4px 10px', fontSize: 12, margin: '4px 0',
  },
  time: { fontSize: 10, color: '#8696a0', textAlign: 'right', marginTop: 4 },
  dots: { display: 'flex', gap: 4, alignItems: 'center' },
  dot: {
    width: 7, height: 7, borderRadius: '50%',
    background: '#8696a0', display: 'inline-block',
    animation: 'bounce 1.2s infinite',
  },
  code: {
    fontFamily: 'monospace', fontSize: 12, background: '#182229',
    padding: '1px 4px', borderRadius: 3, display: 'inline-block', whiteSpace: 'pre-wrap',
  },
  inputBar: {
    display: 'flex', alignItems: 'flex-end', gap: 8,
    padding: '8px 12px', background: '#202c33', borderTop: '1px solid #374045',
  },
  optionsBtn: {
    width: 42, height: 42, borderRadius: '50%', flexShrink: 0,
    background: '#2a3942', border: '1px solid #374045',
    color: '#e9edef', fontSize: 18, cursor: 'pointer',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    transition: 'background 0.15s',
  },
  textarea: {
    flex: 1, background: '#2a3942', border: 'none', borderRadius: 8,
    padding: '9px 12px', color: '#e9edef', fontSize: 15,
    resize: 'none', outline: 'none', fontFamily: 'inherit',
    lineHeight: 1.4, maxHeight: 120, overflowY: 'auto',
  },
  sendBtn: {
    width: 42, height: 42, borderRadius: '50%', flexShrink: 0,
    background: '#00a884', border: 'none', color: '#fff',
    fontSize: 18, cursor: 'pointer',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    transition: 'background 0.2s',
  },
  // Modal
  overlay: {
    position: 'absolute', inset: 0,
    background: 'rgba(0,0,0,0.55)', display: 'flex',
    alignItems: 'flex-end', zIndex: 10,
  },
  sheet: {
    width: '100%', background: '#202c33',
    borderRadius: '16px 16px 0 0', padding: '12px 16px 24px',
    animation: 'slideUp 0.22s ease-out',
  },
  sheetHandle: {
    width: 36, height: 4, borderRadius: 2,
    background: '#374045', margin: '0 auto 12px',
  },
  sheetTitle: {
    color: '#8696a0', fontSize: 12, textAlign: 'center',
    margin: '0 0 14px', textTransform: 'uppercase', letterSpacing: '0.06em',
  },
  carousel: {
    display: 'flex', gap: 10, overflowX: 'auto',
    paddingBottom: 4,
    scrollbarWidth: 'none',
    WebkitOverflowScrolling: 'touch',
  },
  card: {
    flex: '0 0 120px', minHeight: 100,
    background: '#2a3942', border: '1px solid #374045',
    borderRadius: 12, padding: '14px 10px',
    display: 'flex', flexDirection: 'column',
    alignItems: 'center', justifyContent: 'center', gap: 6,
    cursor: 'pointer', color: '#e9edef',
    transition: 'background 0.15s, border-color 0.15s',
  },
  cardEmoji: { fontSize: 26, lineHeight: 1 },
  cardLabel: { fontSize: 13, fontWeight: 600, textAlign: 'center', color: '#e9edef' },
  cardDesc:  { fontSize: 11, color: '#8696a0', textAlign: 'center', lineHeight: 1.3 },
}
