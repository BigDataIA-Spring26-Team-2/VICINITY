import { useRef, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Terminal, ChevronDown, ChevronRight } from 'lucide-react'
import { useStore } from './store'

function fmt(e) {
  const t = new Date(e.ts).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  if (e.type === 'route') return { t, p: 'ROUTE', x: e.route, c: 'text-blue-300' }
  if (e.type === 'node_start') return { t, p: 'NODE', x: e.node, c: 'text-cyan-300' }
  if (e.type === 'start') return { t, p: 'CALL', x: `${e.tool}(${e.args || ''})`, c: 'text-yellow-200' }
  if (e.type === 'end') return { t, p: e.error ? 'ERR' : 'OK', x: `${e.tool} ${e.size ? e.size + 'b' : ''}`, c: e.error ? 'text-red-300' : 'text-green-300' }
  if (e.type === 'done') return { t, p: 'DONE', x: `${e.elapsed_ms}ms / ${e.tool_calls} calls / ${e.message_length} chars`, c: 'text-gray-200' }
  return { t, p: 'EVT', x: JSON.stringify(e).slice(0, 60), c: 'text-gray-400' }
}

export default function ToolLog() {
  const evts = useStore(s => s.toolEvents)
  const streaming = useStore(s => s.isStreaming)
  const open = useStore(s => s.toolLogOpen)
  const setOpen = useStore(s => s.setToolLogOpen)
  const ref = useRef(null)
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight }, [evts])

  return (
    <div className="border-t border-gray-200 bg-white">
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-4 py-2.5 hover:bg-gray-50 transition-colors">
        <Terminal size={14} className="text-gray-500" />
        <span className="font-mono text-xs text-gray-700 tracking-wider uppercase font-bold">Agent Log</span>
        {evts.length > 0 && <span className="font-mono text-xs text-gray-400 font-medium">({evts.length})</span>}
        {streaming && <span className="w-2 h-2 rounded-full bg-gray-900 animate-pulse ml-1" />}
        <span className="ml-auto">{open ? <ChevronDown size={14} className="text-gray-400" /> : <ChevronRight size={14} className="text-gray-400" />}</span>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div initial={{ height: 0 }} animate={{ height: 200 }} exit={{ height: 0 }} transition={{ duration: 0.15, ease: 'easeOut' }} className="overflow-hidden">
            <div ref={ref} className="h-[200px] overflow-y-auto bg-gray-950 px-4 py-3 font-mono text-xs leading-6">
              {!evts.length ? <p className="text-gray-500">Waiting for agent activity…</p>
                : evts.map((e, i) => { const { t, p, x, c } = fmt(e); return (
                  <div key={i} className="flex items-baseline gap-3 animate-fade-in">
                    <span className="text-gray-500 shrink-0 select-none tabular-nums">{t}</span>
                    <span className={`shrink-0 w-14 text-right font-bold ${c}`}>{p}</span>
                    <span className="text-gray-200 truncate">{x}</span>
                  </div>
                )})}
              {streaming && <span className="inline-block w-2 h-3.5 bg-gray-400 animate-pulse mt-1" />}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}