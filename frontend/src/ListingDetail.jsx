import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { X, ExternalLink, Shield, Home, Train } from 'lucide-react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, BarChart, Bar } from 'recharts'
import { useStore } from './store'
import { getListingDetail, getScorecard } from './api'

function ScoreBar({ label, value, icon: Icon }) {
  if (value == null) return null
  return <div className="space-y-1">
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-1.5"><Icon size={13} className="text-gray-400" /><span className="font-body text-xs font-bold text-gray-500 uppercase tracking-wider">{label}</span></div>
      <span className="font-mono text-sm font-black text-gray-900">{value}</span>
    </div>
    <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden"><div className="h-full bg-gray-900 rounded-full transition-all duration-500" style={{ width: `${value}%` }} /></div>
  </div>
}

function MetadataGrid({ title, metadata, icon: Icon }) {
  if (!metadata || typeof metadata !== 'object') return null
  const entries = Object.entries(metadata).filter(([k, v]) => v != null && typeof v !== 'object' && !['pipeline_run_id', 'score_version'].includes(k))
  if (!entries.length) return null
  return <div className="mt-3">
    <div className="flex items-center gap-1.5 mb-1.5"><Icon size={12} className="text-gray-400" /><p className="font-body text-[11px] font-bold text-gray-500 uppercase tracking-wider">{title}</p></div>
    <div className="bg-gray-900 rounded-lg p-2.5 grid grid-cols-2 gap-x-5 gap-y-1">
      {entries.map(([k, v]) => <div key={k} className="flex justify-between gap-2">
        <span className="font-body text-[11px] text-gray-400 truncate">{k.replace(/_/g, ' ')}</span>
        <span className="font-mono text-[11px] text-gray-100 shrink-0">{typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2)) : String(v).slice(0, 20)}</span>
      </div>)}
    </div>
  </div>
}

function TagList({ title, items }) {
  const arr = typeof items === 'string' ? items.split(',').map(s => s.trim()) : Array.isArray(items) ? items.filter(i => typeof i === 'string') : []
  if (!arr.length) return null
  return <div className="mt-2">
    <p className="font-body text-[11px] font-bold text-gray-500 mb-1">{title}</p>
    <div className="flex flex-wrap gap-1">{arr.map((s, i) => <span key={i} className="px-1.5 py-0.5 bg-gray-100 border border-gray-200 rounded font-mono text-[11px] text-gray-600">{String(s).replace(/_/g, ' ')}</span>)}</div>
  </div>
}

function Charts({ data }) {
  if (!data?.length) return null
  const d = data.map(r => ({ date: r.score_date?.slice(5) || '', safety: r.safety_score, livability: r.livability_score, crimes: r.crime_count || 0, complaints: r.complaint_count || 0, citizen: r.citizen_incidents_48h || 0 }))
  const tip = { background: '#111', border: 'none', borderRadius: '6px', fontSize: '11px', fontFamily: 'DM Sans', color: '#fff', padding: '4px 8px' }
  const ax = { fontSize: 8, fill: '#999', fontFamily: 'JetBrains Mono' }
  return <div className="mt-4 space-y-3">
    <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
      <p className="font-body text-[11px] font-bold text-gray-500 uppercase tracking-wider">Historical Safety & Livability</p>
      <p className="font-body text-[11px] text-gray-400 mt-0.5 mb-2">Daily scores for the last 90 days. Safety reflects crime frequency; livability factors in transit access, amenities, and complaints.</p>
      <ResponsiveContainer width="100%" height={80}>
        <LineChart data={d}><XAxis dataKey="date" tick={ax} axisLine={false} tickLine={false} /><YAxis domain={[0, 100]} hide /><Tooltip contentStyle={tip} /><Line type="monotone" dataKey="safety" stroke="#111" strokeWidth={2} dot={false} name="Safety" /><Line type="monotone" dataKey="livability" stroke="#999" strokeWidth={1.5} dot={false} name="Livability" /></LineChart>
      </ResponsiveContainer>
    </div>
    {d.some(r => r.crimes > 0 || r.complaints > 0) && <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
      <p className="font-body text-[11px] font-bold text-gray-500 uppercase tracking-wider">Incident History</p>
      <p className="font-body text-[11px] text-gray-400 mt-0.5 mb-2">Daily police reports, 311 calls, and Citizen alerts within 500m. Bars show volume — higher means more reported incidents that day.</p>
      <ResponsiveContainer width="100%" height={70}>
        <BarChart data={d} barGap={0}><XAxis dataKey="date" tick={ax} axisLine={false} tickLine={false} /><Tooltip contentStyle={tip} /><Bar dataKey="crimes" fill="#222" name="Police" radius={[1,1,0,0]} /><Bar dataKey="complaints" fill="#777" name="311" radius={[1,1,0,0]} /><Bar dataKey="citizen" fill="#bbb" name="Citizen" radius={[1,1,0,0]} /></BarChart>
      </ResponsiveContainer>
    </div>}
  </div>
}

export default function ListingDetail() {
  const sel = useStore(s => s.selectedListing); const clear = useStore(s => s.clearSelectedListing)
  const open = useStore(s => s.detailDrawerOpen); const setOpen = useStore(s => s.setDetailDrawerOpen)
  const [detail, setDetail] = useState(null); const [sc, setSc] = useState(null); const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!sel) { setOpen(false); return }
    setOpen(true); setLoading(true); setDetail(null); setSc(null)
    Promise.all([getListingDetail(sel.listing_id).catch(() => null), getScorecard(sel.listing_id, { days: 90 }).catch(() => null)])
      .then(([dr, sr]) => { if (dr?.data?.[0]) setDetail(dr.data[0]); if (sr?.data) setSc(sr.data); setLoading(false) })
  }, [sel?.listing_id])

  const close = () => { setOpen(false); clear() }
  const d = detail || sel; if (!open || !d) return null

  return <motion.div initial={{ y: '100%' }} animate={{ y: 0 }} exit={{ y: '100%' }} transition={{ type: 'spring', damping: 30, stiffness: 300 }}
    className="absolute bottom-0 left-0 right-0 z-30 bg-white border-t border-gray-300 shadow-2xl rounded-t-xl max-h-[40vh] overflow-y-auto">
    <div className="sticky top-0 bg-white z-10 flex justify-center py-1.5 border-b border-gray-100">
      <div className="w-8 h-0.5 rounded-full bg-gray-300" />
      <button onClick={close} className="absolute right-3 top-1.5 p-1 text-gray-400 hover:text-gray-900 transition-colors"><X size={15} /></button>
    </div>
    <div className="px-4 pb-5">
      {loading ? <div className="py-8 text-center"><div className="w-5 h-5 border-2 border-gray-300 border-t-gray-900 rounded-full animate-spin inline-block" /></div> : <>
        {d.source_url && <a href={d.source_url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-2 mt-2 mb-3 px-3 py-2 bg-gray-900 text-gray-100 rounded-lg font-body text-sm hover:bg-gray-800 transition-colors"><ExternalLink size={13} />View on {d.source === 'craigslist' ? 'Craigslist' : 'Realtor.com'}</a>}
        <div className="flex gap-3 mb-3">
          {d.primary_photo_url && <img src={d.primary_photo_url} alt="" className="w-28 h-20 object-cover rounded-lg border border-gray-200 shrink-0" />}
          <div className="min-w-0">
            <h3 className="font-display text-lg font-black text-gray-900 leading-tight">{d.street || 'Listing'}{d.unit && <span className="text-gray-400 font-normal text-sm"> {d.unit}</span>}</h3>
            <p className="font-body text-sm text-gray-500">{[d.city, d.zip_code].filter(Boolean).join(' ')}</p>
            <p className="font-display text-xl font-black text-gray-900 mt-0.5">${d.price?.toLocaleString()}<span className="text-xs text-gray-400 font-normal">/mo</span></p>
          </div>
        </div>
        <div className="flex gap-1.5 mb-3 flex-wrap text-sm">
          {d.beds != null && <C v={d.beds} l="bed" />}{d.baths != null && <C v={d.baths} l="ba" />}{d.sqft && <C v={d.sqft.toLocaleString()} l="sqft" />}{d.days_on_mls != null && <C v={d.days_on_mls} l="days" />}
        </div>
        <div className="space-y-2 mb-3"><ScoreBar label="Safety" value={d.safety_score} icon={Shield} /><ScoreBar label="Livability" value={d.livability_score} icon={Home} /></div>
        {d.nearest_stops?.length > 0 && <TagList title="Transit" items={d.nearest_stops} />}
        {d.description_text && <p className="font-body text-xs text-gray-500 leading-relaxed mt-2 line-clamp-2">{d.description_text}</p>}
        <MetadataGrid title="Safety" metadata={d.safety_metadata} icon={Shield} />
        <MetadataGrid title="Livability" metadata={d.livability_metadata} icon={Home} />
        {d.livability_metadata?.essentials_present && <TagList title="Nearby" items={d.livability_metadata.essentials_present} />}
        {d.livability_metadata?.essentials_missing && <TagList title="Missing" items={d.livability_metadata.essentials_missing} />}
        <Charts data={sc} />
      </>}
    </div>
  </motion.div>
}

function C({ v, l }) { return <span className="px-2 py-1 bg-gray-50 border border-gray-200 rounded font-body"><span className="font-bold text-gray-900">{v}</span> <span className="text-gray-500 text-xs">{l}</span></span> }