import { useRef, useEffect, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Terminal, ChevronDown, ChevronRight, Copy, Check } from 'lucide-react'
import { useStore } from './store'

/* ── Event type styling ─────────────────────────────────────── */

const TYPE_STYLES = {
  route:       { tag: 'ROUTE', color: 'text-blue-300',    bg: 'bg-blue-950/30'    },
  node_start:  { tag: 'NODE',  color: 'text-cyan-300',    bg: 'bg-cyan-950/30'    },
  start:       { tag: 'CALL',  color: 'text-amber-300',   bg: 'bg-amber-950/30'   },
  end:         { tag: 'OK',    color: 'text-emerald-300', bg: 'bg-emerald-950/30' },
  err:         { tag: 'ERR',   color: 'text-red-300',     bg: 'bg-red-950/30'     },
  done:        { tag: 'DONE',  color: 'text-gray-200',    bg: 'bg-gray-800/40'    },
  log_info:    { tag: 'INFO',  color: 'text-gray-400',    bg: 'bg-transparent'    },
  log_warning: { tag: 'WARN',  color: 'text-amber-400',   bg: 'bg-amber-950/20'   },
  log_error:   { tag: 'ERR',   color: 'text-red-400',     bg: 'bg-red-950/20'     },
  log_debug:   { tag: 'DEBUG', color: 'text-gray-500',    bg: 'bg-transparent'    },
  default:     { tag: 'EVT',   color: 'text-gray-400',    bg: 'bg-gray-800/20'    },
}

const SKIP_KEYS = new Set(['event', '_level', 'ts', 'type', 'timestamp', 'logger'])

function fmtTime(ts) {
  const d = new Date(ts)
  return d.toLocaleTimeString('en-US', {
    hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
  }) + '.' + String(d.getMilliseconds()).padStart(3, '0')
}

function fmtValue(v) {
  if (v == null) return ''
  if (typeof v === 'string') return v
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}


/* ── Single event row — expandable ──────────────────────────── */

function EventRow({ evt }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)

  const isErr = evt.type === 'end' && evt.error

  // Log events get level-based styling
  let styleKey
  if (evt.type === 'log') {
    styleKey = `log_${evt._level || 'info'}`
    if (!TYPE_STYLES[styleKey]) styleKey = 'log_info'
  } else {
    styleKey = isErr ? 'err' : evt.type
  }
  const style = TYPE_STYLES[styleKey] || TYPE_STYLES.default

  // Build a one-line summary
  let summary = ''
  if (evt.type === 'log') {
    const eventName = evt.event || evt.message || '—'
    const kvs = Object.entries(evt)
      .filter(([k]) => !SKIP_KEYS.has(k))
      .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join(' · ')
    summary = kvs ? `${eventName}  ${kvs}` : eventName
  }
  else if (evt.type === 'route')       summary = `→ ${evt.route}`
  else if (evt.type === 'node_start')  summary = evt.node
  else if (evt.type === 'start')       summary = `${evt.tool}(${evt.args ?? ''})`
  else if (evt.type === 'end')         summary = `${evt.tool} ${evt.error ? '✗' : '✓'} ${evt.size ? `${evt.size}b` : ''}`
  else if (evt.type === 'done')        summary = `${evt.elapsed_ms}ms · ${evt.tool_calls} calls · ${evt.message_length} chars`
  else                                 summary = evt.summary || evt.content || ''

  const details = fmtValue(evt)
  const hasDetails = details && details.length > 0

  const copy = (e) => {
    e.stopPropagation()
    navigator.clipboard.writeText(details)
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

  return (
    <div className={`rounded-sm ${style.bg} border-l-2 border-l-transparent
                     hover:border-l-gray-500 transition-colors`}>
      <button
        onClick={() => hasDetails && setOpen(!open)}
        className={`w-full flex items-baseline gap-2 px-2 py-1 text-left
                    ${hasDetails ? 'cursor-pointer' : 'cursor-default'}`}>
        <span className="text-gray-500 shrink-0 select-none tabular-nums text-[10.5px]">
          {fmtTime(evt.ts)}
        </span>
        <span className={`shrink-0 w-12 text-right font-bold ${style.color}`}>
          {style.tag}
        </span>
        <span className="text-gray-100 flex-1 break-all whitespace-pre-wrap">
          {summary}
        </span>
        {hasDetails && (
          <ChevronRight
            size={11}
            className={`shrink-0 text-gray-500 transition-transform ${open ? 'rotate-90' : ''}`} />
        )}
      </button>

      <AnimatePresence>
        {open && hasDetails && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden">
            <div className="relative ml-14 mr-2 mb-2 mt-0.5 p-2 pr-8
                            bg-black/40 rounded border border-white/5">
              <pre className="text-gray-300 text-[10.5px] leading-[1.5]
                              whitespace-pre-wrap break-all font-mono">
                {details}
              </pre>
              <button onClick={copy}
                className="absolute top-1.5 right-1.5 p-1 text-gray-500 hover:text-gray-200
                           transition-colors">
                {copied ? <Check size={11} /> : <Copy size={11} />}
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}


/* ── Tool Log panel ─────────────────────────────────────────── */

export default function ToolLog() {
  const evts = useStore(s => s.toolEvents)
  const streaming = useStore(s => s.isStreaming)
  const open = useStore(s => s.toolLogOpen)
  const setOpen = useStore(s => s.setToolLogOpen)
  const ref = useRef(null)

  useEffect(() => {
    if (ref.current && open) ref.current.scrollTop = ref.current.scrollHeight
  }, [evts, open])

  const stats = {
    total: evts.length,
    calls: evts.filter(e => e.type === 'start').length,
    errs:  evts.filter(e => (e.type === 'end' && e.error) ||
                             (e.type === 'log' && e._level === 'error')).length,
    logs:  evts.filter(e => e.type === 'log').length,
  }

  return (
    <div className="border-t border-vicinity-150 bg-vicinity-white">
      <button onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-2.5 hover:bg-vicinity-50 transition-colors">
        <Terminal size={13} className="text-vicinity-500" />
        <span className="font-mono text-[11px] text-vicinity-700 tracking-wider uppercase font-semibold">
          Agent Log
        </span>
        {stats.total > 0 && (
          <span className="font-mono text-[11px] text-vicinity-500">
            ({stats.calls} calls{stats.logs > 0 ? ` · ${stats.logs} logs` : ''}{stats.errs > 0 ? ` · ${stats.errs} err` : ''})
          </span>
        )}
        {streaming && <span className="w-1.5 h-1.5 rounded-full bg-vicinity-black animate-pulse ml-1" />}
        <span className="ml-auto">
          {open
            ? <ChevronDown size={13} className="text-vicinity-400" />
            : <ChevronRight size={13} className="text-vicinity-400" />}
        </span>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0 }}
            animate={{ height: 280 }}
            exit={{ height: 0 }}
            transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden">
            <div ref={ref} className="h-[280px] overflow-y-auto bg-gray-950 px-2 py-2
                                      font-mono text-[11px] leading-[1.55] space-y-0.5">
              {!evts.length
                ? <p className="text-gray-500 px-2 py-1 italic">Waiting for agent activity…</p>
                : evts.map((e, i) => <EventRow key={i} evt={e} />)}
              {streaming && (
                <div className="px-2 py-1">
                  <span className="inline-block w-1.5 h-3 bg-gray-400 animate-pulse" />
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}