import { useState, useRef, useEffect } from 'react'
import { Send, X, MessageSquare, Check, XIcon, Pencil } from 'lucide-react'
import { useStore } from './store'
import ToolLog from './ToolLog'

function renderMarkdown(text) {
  if (!text) return null
  const lines = text.split('\n'); const out = []; let list = []
  const flush = () => { if (list.length) { out.push(<ul key={`ul-${out.length}`} className="space-y-1.5 my-2">{list.map((li, i) => <li key={i} className="flex gap-2 text-sm leading-relaxed"><span className="text-gray-400 shrink-0">&bull;</span><span>{fmt(li)}</span></li>)}</ul>); list = [] } }
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]
    if (l.startsWith('### ')) { flush(); out.push(<h4 key={i} className="font-body font-bold text-sm mt-3 mb-1">{fmt(l.slice(4))}</h4>) }
    else if (l.startsWith('## ')) { flush(); out.push(<h3 key={i} className="font-body font-bold text-base mt-3 mb-1">{fmt(l.slice(3))}</h3>) }
    else if (/^[-–•]\s/.test(l)) list.push(l.replace(/^[-–•]\s/, ''))
    else if (l.trim() === '') flush()
    else { flush(); out.push(<p key={i} className="text-sm leading-relaxed my-0.5">{fmt(l)}</p>) }
  }
  flush(); return out
}

function fmt(t) {
  if (!t) return t; const p = []; let last = 0
  const rx = /(\*\*(.+?)\*\*)|(\[([^\]]+)\]\(([^)]+)\))|(https?:\/\/[^\s\])<]+)/g; let m
  while ((m = rx.exec(t)) !== null) {
    if (m.index > last) p.push(t.slice(last, m.index))
    if (m[1]) p.push(<strong key={m.index} className="font-bold">{m[2]}</strong>)
    else if (m[3]) p.push(<a key={m.index} href={m[5]} target="_blank" rel="noopener noreferrer" className="underline decoration-gray-400 hover:decoration-gray-900 underline-offset-2 transition-colors">{m[4]}</a>)
    else if (m[6]) p.push(<a key={m.index} href={m[6]} target="_blank" rel="noopener noreferrer" className="underline decoration-gray-400 hover:decoration-gray-900 underline-offset-2 transition-colors break-all text-xs">{m[6].length > 50 ? m[6].slice(0, 50) + '…' : m[6]}</a>)
    last = m.index + m[0].length
  }
  if (last < t.length) p.push(t.slice(last)); return p.length ? p : t
}

function Bubble({ message }) {
  const isUser = message.role === 'user'
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} animate-fade-in`}>
      {isUser ? (
        <div className="max-w-[85%] px-4 py-3 bg-gray-900 text-white rounded-2xl rounded-br-sm shadow-md">
          <p className="font-body text-sm leading-relaxed">{message.content}</p>
        </div>
      ) : (
        <div className="max-w-[88%] px-4 py-3 bg-white text-gray-900 rounded-2xl rounded-bl-sm border-l-4 border-l-gray-900 border border-gray-200 shadow-sm">
          <div className="font-body break-words">{renderMarkdown(message.content)}</div>
        </div>
      )}
    </div>
  )
}

function InterruptBar() {
  const interrupt = useStore(s => s.interrupt)
  const resume = useStore(s => s.resumeInterrupt)
  const [mod, setMod] = useState(''); const [showMod, setShowMod] = useState(false)
  if (!interrupt) return null
  return (
    <div className="mx-4 mb-3 p-3 bg-gray-50 border border-gray-200 rounded-lg animate-fade-in">
      <p className="font-body text-sm text-gray-700 mb-3">{interrupt.summary || 'Action pending'}</p>
      {showMod ? (
        <div className="flex gap-2">
          <input value={mod} onChange={e => setMod(e.target.value)} placeholder="What should change?" autoFocus
            onKeyDown={e => { if (e.key === 'Enter' && mod.trim()) { resume(mod.trim()); setShowMod(false); setMod('') } }}
            className="flex-1 px-3 py-2 font-body text-sm border border-gray-200 rounded-md focus:outline-none focus:border-gray-900 transition-colors" />
          <button onClick={() => { setShowMod(false); setMod('') }} className="p-1.5 text-gray-400 hover:text-gray-900"><X size={14} /></button>
        </div>
      ) : (
        <div className="flex gap-2">
          <button onClick={() => resume('yes')} className="flex items-center gap-1.5 px-4 py-2 bg-gray-900 text-white font-body text-sm font-medium rounded-md hover:bg-gray-800 transition-colors"><Check size={13} /> Approve</button>
          <button onClick={() => resume('no')} className="flex items-center gap-1.5 px-4 py-2 border border-gray-300 font-body text-sm rounded-md hover:bg-gray-50 transition-colors"><XIcon size={13} /> Reject</button>
          <button onClick={() => setShowMod(true)} className="flex items-center gap-1.5 px-4 py-2 border border-gray-300 font-body text-sm rounded-md hover:bg-gray-50 transition-colors"><Pencil size={13} /> Modify</button>
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
  const endRef = useRef(null)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, isStreaming])
  const send = () => { const t = input.trim(); if (!t || isStreaming) return; setInput(''); sendMessage(t) }

  if (!chatOpen) return (
    <button onClick={() => setChatOpen(true)} className="fixed bottom-6 right-6 z-40 w-12 h-12 bg-gray-900 text-white rounded-full flex items-center justify-center shadow-lg hover:bg-gray-800 active:scale-95 transition-all">
      <MessageSquare size={18} />
    </button>
  )

  return (
    <div className="h-full flex flex-col bg-gray-50 border-l border-gray-200">
      <div className="flex items-center justify-between px-4 py-3 bg-white border-b border-gray-200">
        <div>
          <h2 className="font-display text-lg font-bold text-gray-900 leading-none">Chat</h2>
          <p className="font-body text-xs text-gray-400 mt-0.5">{isStreaming ? 'Thinking…' : 'Ask about listings, safety, neighborhoods'}</p>
        </div>
        <button onClick={() => setChatOpen(false)} className="p-1.5 text-gray-400 hover:text-gray-900 transition-colors"><X size={16} /></button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        {messages.length === 0 && (
          <div className="h-full flex flex-col items-center justify-center text-center px-6">
            <p className="font-display text-2xl font-bold text-gray-200 mb-2">Where should you live?</p>
            <p className="font-body text-sm text-gray-400 max-w-[280px] leading-relaxed">Ask about apartments, neighborhood safety, commute routes, or anything about living in Boston.</p>
          </div>
        )}
        {messages.map((msg, i) => <Bubble key={`${msg.ts}-${i}`} message={msg} />)}
        {isStreaming && messages[messages.length - 1]?.role !== 'assistant' && (
          <div className="flex justify-start"><div className="flex items-center gap-1.5 px-4 py-3 bg-white border border-gray-200 rounded-2xl rounded-bl-sm shadow-sm">
            {[0, 1, 2].map(i => <span key={i} className="w-1.5 h-1.5 rounded-full bg-gray-400 animate-pulse" style={{ animationDelay: `${i * 200}ms` }} />)}
          </div></div>
        )}
        <div ref={endRef} />
      </div>

      {interrupt && <InterruptBar />}
      <ToolLog />

      <div className="px-4 py-3 bg-white border-t border-gray-200">
        <div className="flex items-end gap-2">
          <textarea value={input}
            onChange={e => { setInput(e.target.value); e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px' }}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            placeholder="Type a message…" rows={1} disabled={isStreaming}
            className="flex-1 resize-none px-3 py-2.5 font-body text-sm bg-gray-50 border border-gray-200 rounded-lg placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-gray-900 focus:border-transparent disabled:opacity-40 transition-all"
            style={{ minHeight: '42px', maxHeight: '120px' }} />
          <button onClick={send} disabled={!input.trim() || isStreaming}
            className="p-3 bg-gray-900 text-white rounded-lg hover:bg-gray-800 active:scale-95 disabled:opacity-30 transition-all shrink-0">
            <Send size={15} />
          </button>
        </div>
      </div>
    </div>
  )
}