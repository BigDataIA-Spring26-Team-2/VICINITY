import { useState, useRef, useEffect, useMemo } from 'react'
import { Send, X, MessageSquare, Check, Pencil, ArrowDown } from 'lucide-react'
import { useStore } from './store'
import ToolLog from './ToolLog'


/* ─── Markdown renderer ─────────────────────────────────────── */

function renderMarkdown(text) {
  if (!text) return null
  const out = []
  const lines = text.split('\n')

  let i = 0
  let list = null
  let codeBuf = null

  const flushList = () => {
    if (!list) return
    const Tag = list.ordered ? 'ol' : 'ul'
    out.push(
      <Tag key={`l-${out.length}`}
        className={`${list.ordered ? 'list-decimal' : 'list-disc'} pl-5 my-2 space-y-1 marker:text-vicinity-300`}>
        {list.items.map((li, k) => (
          <li key={k} className="text-[13px] leading-[1.55] text-vicinity-800">
            {fmtInline(li)}
          </li>
        ))}
      </Tag>
    )
    list = null
  }

  const flushCode = () => {
    if (!codeBuf) return
    out.push(
      <pre key={`c-${out.length}`}
        className="my-2 px-3 py-2.5 rounded-md bg-vicinity-950 text-vicinity-100
                   font-mono text-[11.5px] leading-relaxed overflow-x-auto">
        {codeBuf.lang && (
          <div className="text-[10px] text-vicinity-400 mb-1 uppercase tracking-wider">
            {codeBuf.lang}
          </div>
        )}
        <code>{codeBuf.lines.join('\n')}</code>
      </pre>
    )
    codeBuf = null
  }

  while (i < lines.length) {
    const line = lines[i]

    if (/^```/.test(line)) {
      flushList()
      if (codeBuf) { flushCode() }
      else { codeBuf = { lang: line.slice(3).trim(), lines: [] } }
      i++; continue
    }
    if (codeBuf) { codeBuf.lines.push(line); i++; continue }

    if (/^\s*\|/.test(line) && /^\s*\|?[\s:-]+\|[\s:|-]*$/.test(lines[i + 1] || '')) {
      flushList()
      const header = splitRow(line)
      const aligns = splitRow(lines[i + 1]).map(c => {
        const s = c.trim()
        if (s.startsWith(':') && s.endsWith(':')) return 'center'
        if (s.endsWith(':')) return 'right'
        return 'left'
      })
      const rows = []
      let j = i + 2
      while (j < lines.length && /^\s*\|/.test(lines[j])) {
        rows.push(splitRow(lines[j])); j++
      }
      out.push(
        <div key={`t-${out.length}`} className="my-2 overflow-x-auto -mx-1 px-1">
          <table className="min-w-full border-collapse">
            <thead>
              <tr>
                {header.map((h, k) => (
                  <th key={k}
                    className="border-b border-vicinity-300 pb-1 pr-3 last:pr-0
                               font-body text-[11px] font-semibold text-vicinity-600
                               uppercase tracking-wider"
                    style={{ textAlign: aligns[k] || 'left' }}>
                    {fmtInline(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri} className="border-b border-vicinity-100 last:border-0">
                  {r.map((c, ci) => (
                    <td key={ci}
                      className="py-1.5 pr-3 last:pr-0 font-body text-[12px] text-vicinity-800 align-top"
                      style={{ textAlign: aligns[ci] || 'left' }}>
                      {fmtInline(c)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
      i = j
      continue
    }

    if (/^###\s/.test(line)) {
      flushList()
      out.push(<h4 key={i} className="font-display text-[14px] font-semibold mt-3 mb-1 text-vicinity-black">{fmtInline(line.replace(/^###\s/, ''))}</h4>)
      i++; continue
    }
    if (/^##\s/.test(line)) {
      flushList()
      out.push(<h3 key={i} className="font-display text-[15px] font-semibold mt-3 mb-1.5 text-vicinity-black">{fmtInline(line.replace(/^##\s/, ''))}</h3>)
      i++; continue
    }
    if (/^#\s/.test(line)) {
      flushList()
      out.push(<h2 key={i} className="font-display text-[16px] font-semibold mt-3 mb-1.5 text-vicinity-black">{fmtInline(line.replace(/^#\s/, ''))}</h2>)
      i++; continue
    }

    const ol = line.match(/^\s*\d+\.\s(.+)$/)
    if (ol) {
      if (!list || !list.ordered) { flushList(); list = { ordered: true, items: [] } }
      list.items.push(ol[1])
      i++; continue
    }

    const ul = line.match(/^\s*[-–•*]\s(.+)$/)
    if (ul) {
      if (!list || list.ordered) { flushList(); list = { ordered: false, items: [] } }
      list.items.push(ul[1])
      i++; continue
    }

    if (line.trim() === '') { flushList(); i++; continue }

    flushList()
    out.push(
      <p key={i} className="text-[13px] leading-[1.6] my-1 text-vicinity-800">
        {fmtInline(line)}
      </p>
    )
    i++
  }

  flushList()
  flushCode()
  return out
}

function splitRow(line) {
  const s = line.trim().replace(/^\|/, '').replace(/\|$/, '')
  return s.split('|').map(c => c.trim())
}

function fmtInline(text) {
  if (text == null) return null
  const parts = []
  let last = 0
  const rx = /(\*\*([^*]+)\*\*)|(`([^`]+)`)|(\[([^\]]+)\]\(([^)]+)\))|(https?:\/\/[^\s\])<]+)/g
  let m
  while ((m = rx.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    if (m[1]) parts.push(<strong key={m.index} className="font-semibold text-vicinity-black">{m[2]}</strong>)
    else if (m[3]) parts.push(<code key={m.index} className="px-1.5 py-0.5 rounded bg-vicinity-100 font-mono text-[11.5px] text-vicinity-900">{m[4]}</code>)
    else if (m[5]) parts.push(<a key={m.index} href={m[7]} target="_blank" rel="noopener noreferrer" className="underline decoration-vicinity-300 underline-offset-2 hover:decoration-vicinity-black transition-colors">{m[6]}</a>)
    else if (m[8]) {
      const url = m[8]
      const short = url.length > 42 ? url.slice(0, 42) + '…' : url
      parts.push(<a key={m.index} href={url} target="_blank" rel="noopener noreferrer" className="underline decoration-vicinity-300 underline-offset-2 hover:decoration-vicinity-black transition-colors font-mono text-[11.5px] break-all">{short}</a>)
    }
    last = m.index + m[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts.length ? parts : text
}


/* ─── Bubble ─────────────────────────────────────────────────── */

function Bubble({ message, streaming }) {
  const isUser = message.role === 'user'

  // Memoize the markdown parse per-content. If content hasn't changed
  // between two renders (e.g. this is a completed bubble and some OTHER
  // bubble is streaming), the tree is reused — zero parse cost.
  //
  // For the actively streaming bubble, content changes every flush and
  // useMemo re-parses. A full parse on a few hundred words is ~2-3ms,
  // well under our 16ms frame budget, so formatting resolves live as
  // the tokens arrive. Unclosed markers (mid-word **, partial tables)
  // just render as plain text until their closing delimiter streams in
  // — same behavior as Claude.ai / ChatGPT, feels natural.
  const tree = useMemo(() => renderMarkdown(message.content), [message.content])

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] px-4 py-2.5 bg-vicinity-black text-vicinity-white
                        rounded-2xl rounded-br-md">
          <p className="font-body text-[13px] leading-[1.5]">{message.content}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex justify-start">
      <div className="max-w-[94%] px-4 py-3 bg-vicinity-white text-vicinity-800
                      rounded-2xl rounded-bl-md border border-vicinity-150
                      border-l-[3px] border-l-vicinity-black">
        <div className="font-body break-words">
          {tree}
          {streaming && (
            <span className="inline-block w-[2px] h-[1em] ml-[1px] align-middle
                             bg-vicinity-400 animate-pulse -mb-[2px]" />
          )}
        </div>
      </div>
    </div>
  )
}


function InterruptBar() {
  const interrupt = useStore(s => s.interrupt)
  const resume = useStore(s => s.resumeInterrupt)
  const [mod, setMod] = useState('')
  const [showMod, setShowMod] = useState(false)
  if (!interrupt) return null
  return (
    <div className="mx-4 mb-3 p-3 bg-vicinity-50 border border-vicinity-200 rounded-lg">
      <p className="font-body text-[13px] text-vicinity-700 mb-3">{interrupt.summary || 'Action pending'}</p>
      {showMod ? (
        <div className="flex gap-2">
          <input value={mod} onChange={e => setMod(e.target.value)}
            placeholder="What should change?" autoFocus
            onKeyDown={e => { if (e.key === 'Enter' && mod.trim()) { resume(mod.trim()); setShowMod(false); setMod('') } }}
            className="flex-1 px-3 py-2 font-body text-[13px] border border-vicinity-200 rounded-md
                       bg-vicinity-white focus:outline-none focus:border-vicinity-black transition-colors" />
          <button onClick={() => { setShowMod(false); setMod('') }}
            className="p-1.5 text-vicinity-400 hover:text-vicinity-black"><X size={14} /></button>
        </div>
      ) : (
        <div className="flex gap-2">
          <button onClick={() => resume('yes')}
            className="flex items-center gap-1.5 px-4 py-2 bg-vicinity-black text-vicinity-white
                       font-body text-[12.5px] font-medium rounded-md hover:bg-vicinity-800 transition-colors">
            <Check size={13} /> Approve
          </button>
          <button onClick={() => resume('no')}
            className="flex items-center gap-1.5 px-4 py-2 border border-vicinity-300
                       font-body text-[12.5px] rounded-md hover:bg-vicinity-50 transition-colors">
            <X size={13} /> Reject
          </button>
          <button onClick={() => setShowMod(true)}
            className="flex items-center gap-1.5 px-4 py-2 border border-vicinity-300
                       font-body text-[12.5px] rounded-md hover:bg-vicinity-50 transition-colors">
            <Pencil size={13} /> Modify
          </button>
        </div>
      )}
    </div>
  )
}


export default function Chat() {
  const messages = useStore(s => s.messages)
  const isStreaming = useStore(s => s.isStreaming)
  const interrupt = useStore(s => s.interrupt)
  const sendMessage = useStore(s => s.sendMessage)
  const chatOpen = useStore(s => s.chatOpen)
  const setChatOpen = useStore(s => s.setChatOpen)
  const [input, setInput] = useState('')

  /* Scroll management — decoupled from React commit.
   *
   * Previously we used useLayoutEffect([messages]) to scroll-to-bottom
   * on every render. The Performance profile flagged this as a
   * "forced reflow" source: React commits content → effect reads
   * scrollHeight → effect writes scrollTop. Chrome has to compute
   * layout synchronously because reading scrollHeight must return
   * accurate numbers.
   *
   * New approach: a single rAF loop runs while `isStreaming`. Every
   * frame, it reads scrollHeight + clientHeight + scrollTop ONCE,
   * decides if we need to scroll, and writes scrollTop at most once.
   * Browser does layout at its natural timing, not on demand.
   *
   * When not streaming, scroll-on-new-message uses a one-shot rAF.
   * Same no-reflow pattern.
   */

  const containerRef = useRef(null)
  const pinnedRef = useRef(true)
  const rafRef = useRef(null)
  const [showJumpBtn, setShowJumpBtn] = useState(false)

  // Scroll detection runs on the container's own scroll event, throttled
  // via passive listener. Only reads dimensions, no writes.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onScroll = () => {
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
      pinnedRef.current = atBottom
      // Only show jump button if we're away from bottom AND streaming
      const shouldShow = !atBottom && isStreaming
      setShowJumpBtn(prev => prev === shouldShow ? prev : shouldShow)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [isStreaming])

  // rAF-driven auto-scroll. Runs ONLY while streaming. Reads dimensions
  // once per frame, never inside React commit. No forced reflows.
  useEffect(() => {
    if (!isStreaming) return

    const tick = () => {
      const el = containerRef.current
      if (el && pinnedRef.current) {
        const bottom = el.scrollHeight - el.clientHeight
        if (Math.abs(el.scrollTop - bottom) > 1) {
          el.scrollTop = bottom
        }
      }
      rafRef.current = requestAnimationFrame(tick)
    }

    rafRef.current = requestAnimationFrame(tick)
    return () => {
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
    }
  }, [isStreaming])

  // One-shot scroll when user submits a new message (outside streaming)
  useEffect(() => {
    if (isStreaming) return
    if (!pinnedRef.current) return
    const id = requestAnimationFrame(() => {
      const el = containerRef.current
      if (el) el.scrollTop = el.scrollHeight
    })
    return () => cancelAnimationFrame(id)
  }, [messages.length, isStreaming])

  const jumpToLatest = () => {
    pinnedRef.current = true
    const el = containerRef.current
    if (el) el.scrollTop = el.scrollHeight
    setShowJumpBtn(false)
  }

  const send = () => {
    const t = input.trim()
    if (!t || isStreaming) return
    setInput('')
    pinnedRef.current = true
    sendMessage(t)
  }

  if (!chatOpen) {
    return (
      <button onClick={() => setChatOpen(true)}
        className="fixed bottom-6 right-6 z-40 w-12 h-12 bg-vicinity-black text-vicinity-white
                   rounded-full flex items-center justify-center shadow-lg
                   hover:bg-vicinity-800 active:scale-95 transition-all">
        <MessageSquare size={18} />
      </button>
    )
  }

  const lastIdx = messages.length - 1
  const streamingIdx = (
    isStreaming && lastIdx >= 0 && messages[lastIdx].role === 'assistant'
  ) ? lastIdx : -1

  return (
    <div className="h-full flex flex-col bg-vicinity-50 border-l border-vicinity-150 relative">

      <div className="flex items-center justify-between px-4 py-3 bg-vicinity-white
                      border-b border-vicinity-150">
        <div>
          <h2 className="font-display text-[18px] leading-none text-vicinity-black">Chat</h2>
          <p className="font-body text-[11px] text-vicinity-400 mt-1">
            {isStreaming ? 'Thinking…' : 'Ask about listings, safety, neighborhoods'}
          </p>
        </div>
        <button onClick={() => setChatOpen(false)}
          className="p-1.5 text-vicinity-400 hover:text-vicinity-black transition-colors">
          <X size={16} />
        </button>
      </div>

      {/*
        Scroll container. CSS `contain: layout paint` isolates layout
        and paint work to this subtree. Repainting a bubble won't
        trigger reflow in the parent or sibling (map, sidebar).
        `overscroll-behavior: contain` stops scroll chaining.
      */}
      <div ref={containerRef}
           className="flex-1 overflow-y-auto px-4 py-4 space-y-3"
           style={{
             contain: 'layout paint',
             overscrollBehavior: 'contain',
             willChange: 'scroll-position',
           }}>

        {messages.length === 0 && (
          <div className="h-full flex flex-col items-center justify-center text-center px-6">
            <p className="font-display text-[28px] leading-tight text-vicinity-200 mb-2">
              Where should you live?
            </p>
            <p className="font-body text-[13px] text-vicinity-400 max-w-[280px] leading-relaxed">
              Ask about apartments, neighborhood safety, commute routes, or anything about living in Boston.
            </p>
          </div>
        )}

        {messages.map((msg, i) => (
          <Bubble
            key={`${msg.ts}-${i}`}
            message={msg}
            streaming={i === streamingIdx} />
        ))}

        {isStreaming && (messages.length === 0 || messages[messages.length - 1].role !== 'assistant') && (
          <div className="flex justify-start">
            <div className="flex items-center gap-1.5 px-4 py-3 bg-vicinity-white
                            border border-vicinity-150 rounded-2xl rounded-bl-md">
              {[0, 1, 2].map(k => (
                <span key={k} className="w-1.5 h-1.5 rounded-full bg-vicinity-400 animate-pulse"
                  style={{ animationDelay: `${k * 200}ms` }} />
              ))}
            </div>
          </div>
        )}
      </div>

      {showJumpBtn && (
        <button onClick={jumpToLatest}
          className="absolute left-1/2 -translate-x-1/2 z-20
                     inline-flex items-center gap-1.5 px-3 py-1.5
                     bg-vicinity-black text-vicinity-white
                     rounded-full shadow-lg
                     font-body text-[11.5px] font-medium
                     hover:bg-vicinity-800 active:scale-95 transition-all"
          style={{ bottom: interrupt ? 180 : 120 }}>
          <ArrowDown size={12} />
          Streaming below
        </button>
      )}

      {interrupt && <InterruptBar />}
      <ToolLog />

      <div className="px-4 py-3 bg-vicinity-white border-t border-vicinity-150">
        <div className="flex items-end gap-2">
          <textarea value={input}
            onChange={e => {
              setInput(e.target.value)
              e.target.style.height = 'auto'
              e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px'
            }}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
            }}
            placeholder="Type a message…"
            rows={1}
            disabled={isStreaming}
            className="flex-1 resize-none px-3 py-2.5 font-body text-[13px]
                       bg-vicinity-50 border border-vicinity-200 rounded-lg
                       placeholder:text-vicinity-400 focus:outline-none focus:ring-2
                       focus:ring-vicinity-black focus:border-transparent
                       disabled:opacity-40 transition-all"
            style={{ minHeight: '42px', maxHeight: '120px' }} />
          <button onClick={send} disabled={!input.trim() || isStreaming}
            className="p-2.5 bg-vicinity-black text-vicinity-white rounded-lg
                       hover:bg-vicinity-800 active:scale-95
                       disabled:opacity-30 transition-all shrink-0">
            <Send size={15} />
          </button>
        </div>
      </div>
    </div>
  )
}