import { useEffect, useMemo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  X, ExternalLink, Shield, Home, MapPin, Users, Route,
  TrendingUp, TrendingDown, AlertCircle, Check, Train, ChevronDown,
  MessageSquare, Send, Edit2,
} from 'lucide-react'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
} from 'recharts'
import { useStore } from './store'


/* ─── Atoms ────────────────────────────────────────────────── */

function Label({ children }) {
  return <p className="lbl">{children}</p>
}

function Chip({ children, tone = 'neutral' }) {
  const map = {
    neutral:  'bg-vicinity-100 text-vicinity-700 border-vicinity-200',
    positive: 'bg-vicinity-50 text-vicinity-900 border-vicinity-300',
    negative: 'bg-[#faecec] text-[#7a1f1f] border-[#e9c4c4]',
    mixed:    'bg-[#f8f2e4] text-[#7a5420] border-[#e6d4a8]',
    dark:     'bg-vicinity-black text-vicinity-white border-vicinity-black',
  }
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full
                      font-body text-[11px] border ${map[tone]}`}>
      {children}
    </span>
  )
}

function ConfidenceBadge({ value }) {
  if (value == null) return null
  const level = value >= 0.7 ? 'High' : value >= 0.4 ? 'Medium' : 'Low'
  const tone = value >= 0.7 ? 'dark' : value >= 0.4 ? 'neutral' : 'mixed'
  return <Chip tone={tone}>Confidence · {level}</Chip>
}

function Tile({ label, value, sub, tone = 'neutral' }) {
  const bg = tone === 'alert' ? 'bg-[#faecec]' : 'bg-vicinity-50'
  return (
    <div className={`${bg} rounded-lg p-2.5 border border-vicinity-100`}>
      <p className="font-body text-[10px] uppercase tracking-wider text-vicinity-500 font-semibold leading-none">
        {label}
      </p>
      <p className="font-display text-[22px] leading-none font-bold text-vicinity-black mt-1.5 tabular-nums">
        {value ?? '—'}
      </p>
      {sub && <p className="font-mono text-[10px] text-vicinity-500 mt-1">{sub}</p>}
    </div>
  )
}

function ScoreGauge({ label, value, icon: Icon }) {
  const v = value ?? 0
  const circ = 2 * Math.PI * 30
  const offset = circ - (v / 100) * circ
  return (
    <div className="flex items-center gap-3">
      <div className="relative w-[76px] h-[76px] shrink-0">
        <svg viewBox="0 0 76 76" className="w-full h-full -rotate-90">
          <circle cx="38" cy="38" r="30" fill="none" stroke="#e5e5e5" strokeWidth="5" />
          <motion.circle
            cx="38" cy="38" r="30" fill="none"
            stroke="#0a0a0a" strokeWidth="5" strokeLinecap="round"
            strokeDasharray={circ}
            initial={{ strokeDashoffset: circ }}
            animate={{ strokeDashoffset: offset }}
            transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }} />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="font-display text-[22px] leading-none font-bold text-vicinity-black tabular-nums">
            {value != null ? v : '—'}
          </span>
        </div>
      </div>
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          <Icon size={12} className="text-vicinity-500" />
          <p className="lbl">{label}</p>
        </div>
        <p className="font-mono text-[11px] text-vicinity-500 mt-1">
          {value != null ? `${v}th percentile` : 'Unscored'}
        </p>
      </div>
    </div>
  )
}

function HBar({ label, count, max }) {
  const pct = max > 0 ? Math.min(100, (count / max) * 100) : 0
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="font-body text-[12px] text-vicinity-700 capitalize">{label}</span>
        <span className="font-mono text-[11px] text-vicinity-500 tabular-nums">{count}</span>
      </div>
      <div className="h-1.5 bg-vicinity-100 rounded-full overflow-hidden">
        <motion.div
          className="h-full bg-vicinity-700 rounded-full"
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }} />
      </div>
    </div>
  )
}

function SparkBars({ data, height = 48, ariaLabel }) {
  if (!data?.length) return null
  const max = Math.max(...data.map(d => d.value), 1)
  return (
    <div
      aria-label={ariaLabel}
      className="flex items-end gap-[2px] w-full"
      style={{ height: `${height}px` }}>
      {data.map((d, i) => {
        const pct = (d.value / max) * 100
        return (
          <div
            key={i}
            className="flex-1 h-full flex items-end"
            title={`${d.label}: ${d.value}`}>
            <motion.div
              initial={{ height: '0%' }}
              animate={{ height: `${pct}%` }}
              transition={{ duration: 0.5, delay: i * 0.012, ease: 'easeOut' }}
              className="w-full bg-vicinity-800 rounded-sm min-h-[1px]"
              style={{ opacity: 0.35 + (d.value / max) * 0.65 }} />
          </div>
        )
      })}
    </div>
  )
}

function ExpandableText({ text, preview = 180 }) {
  const [open, setOpen] = useState(false)
  if (!text) return null
  const isLong = text.length > preview
  return (
    <div>
      <p className="font-body text-[13px] text-vicinity-600 leading-relaxed whitespace-pre-wrap">
        {open || !isLong ? text : text.slice(0, preview) + '…'}
      </p>
      {isLong && (
        <button onClick={() => setOpen(!open)}
          className="mt-1.5 font-body text-[11px] text-vicinity-500 hover:text-vicinity-black
                     transition-colors uppercase tracking-wider">
          {open ? 'Show less' : 'Read more'}
        </button>
      )}
    </div>
  )
}


/* ─── Overview Tab ────────────────────────────────────────── */

function OverviewTab({ d }) {
  const interp = d.safety_metadata?.interpretation
  const safetyConf = d.safety_metadata?.confidence
  const livConf = d.livability_metadata?.confidence

  return (
    <div className="space-y-4">
      {d.primary_photo_url && (
        <motion.img
          src={d.primary_photo_url} alt=""
          initial={{ opacity: 0, scale: 1.01 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.4 }}
          className="w-full h-[140px] object-cover rounded-lg border border-vicinity-150" />
      )}

      <div>
        <h3 className="font-display text-[20px] font-bold text-vicinity-black leading-tight">
          {d.street || 'Listing'}
          {d.unit && <span className="text-vicinity-400 font-normal text-[16px]"> {d.unit}</span>}
        </h3>
        <p className="font-body text-[13px] text-vicinity-500">
          {[d.neighborhood, d.city, d.zip_code].filter(Boolean).join(' · ')}
        </p>
        <p className="font-display text-[28px] leading-none font-bold text-vicinity-black
                      mt-1.5 tabular-nums">
          ${d.price?.toLocaleString()}
          <span className="font-body text-[12px] text-vicinity-400 font-normal ml-1">/mo</span>
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        {d.beds != null && <Chip>{d.beds} bed</Chip>}
        {d.baths != null && <Chip>{d.baths} ba</Chip>}
        {d.sqft && <Chip>{d.sqft.toLocaleString()} sqft</Chip>}
        {d.days_on_mls != null && <Chip>{d.days_on_mls}d listed</Chip>}
        {d.style && <Chip>{d.style}</Chip>}
      </div>

      <div className="panel-lifted p-3.5 grid grid-cols-2 gap-4">
        <ScoreGauge label="Safety" value={d.safety_score} icon={Shield} />
        <ScoreGauge label="Livability" value={d.livability_score} icon={Home} />
      </div>

      {(safetyConf != null || livConf != null) && (
        <div className="flex flex-wrap gap-2">
          {safetyConf != null && <ConfidenceBadge value={safetyConf} />}
          {livConf != null && <ConfidenceBadge value={livConf} />}
        </div>
      )}

      {interp && (
        <div className="border-l-2 border-vicinity-black pl-3 py-0.5">
          <p className="font-body text-[13px] text-vicinity-800 leading-relaxed">{interp}</p>
        </div>
      )}

      {d.description_text && (
        <div>
          <Label>Description</Label>
          <div className="mt-1.5">
            <ExpandableText text={d.description_text} preview={220} />
          </div>
        </div>
      )}

      {d.source_url && (
        <a href={d.source_url} target="_blank" rel="noopener noreferrer"
           className="flex items-center justify-center gap-2 w-full px-3 py-2.5
                      bg-vicinity-black text-vicinity-white rounded-lg
                      font-body text-[13px] hover:bg-vicinity-800 transition-colors">
          <ExternalLink size={13} />
          View on {d.source === 'craigslist' ? 'Craigslist' : 'Realtor.com'}
        </a>
      )}
    </div>
  )
}


/* ─── Safety Tab ──────────────────────────────────────────── */

function SafetyTab({ d, scorecard, distribution, crimeTypes }) {
  const sm = d.safety_metadata || {}
  const dist = distribution || {}
  const ct = crimeTypes || {}

  // Historical hour-of-day — sourced from the full crime table (not 30d)
  const hourly = useMemo(() => {
    const h = dist.hourly
    if (!h || typeof h !== 'object') return []
    return Array.from({ length: 24 }, (_, i) => {
      const raw = h[String(i)] ?? h[i] ?? 0
      return { label: `${i}h`, value: Number(raw) || 0 }
    })
  }, [dist.hourly])

  // Historical day-of-week
  const dow = useMemo(() => {
    const d_ = dist.dow
    if (!d_ || typeof d_ !== 'object') return []
    const order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    return order.map(name => {
      // Snowflake DAYNAME returns 'Mon', 'Tue', etc. — try short form first, fall back to full
      const raw = d_[name.slice(0, 3)] ?? d_[name] ?? d_[name.toUpperCase()] ?? 0
      return { label: name.slice(0, 3), value: Number(raw) || 0 }
    })
  }, [dist.dow])

  // Yearly breakdown — how crime evolved over the years on record
  const yearly = useMemo(() => {
    const y = dist.yearly
    if (!y || typeof y !== 'object') return []
    return Object.entries(y)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([year, cnt]) => ({ year, count: Number(cnt) || 0 }))
  }, [dist.yearly])
  const yearMax = Math.max(1, ...yearly.map(y => y.count))

  // Monthly trend from safety_metadata — useful for 2-year monthly pattern
  const monthly = useMemo(() => {
    const m = sm.monthly_series
    if (!m || typeof m !== 'object') return []
    return Object.entries(m)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([month, vals]) => ({
        month: month.slice(5),
        crimes: typeof vals === 'object' ? vals.count || vals.total || 0 : Number(vals) || 0,
        violent: typeof vals === 'object' ? vals.violent || 0 : 0,
      }))
  }, [sm.monthly_series])

  const cp = sm.community_perception
  const yoy = sm.yoy_change_pct
  const histTotal = dist.total || 0
  const histStartYear = dist.earliest_date ? dist.earliest_date.slice(0, 4) : null

  return (
    <div className="space-y-5">
      {sm.interpretation && (
        <p className="font-body text-[13px] text-vicinity-800 leading-relaxed">
          {sm.interpretation}
        </p>
      )}

      <div className="grid grid-cols-4 gap-2">
        <Tile label="Total" value={sm.crime_count ?? 0} sub="30d" />
        <Tile label="Violent" value={sm.violent_count ?? 0} tone={(sm.violent_count || 0) > 0 ? 'alert' : 'neutral'} />
        <Tile label="Shootings" value={sm.shooting_count ?? 0} tone={(sm.shooting_count || 0) > 0 ? 'alert' : 'neutral'} />
        <Tile label="Types" value={sm.offense_types ?? 0} />
      </div>

      {yoy != null && (
        <div className="flex items-center gap-2 p-3 bg-vicinity-50 rounded-lg border border-vicinity-150">
          {yoy < 0 ? <TrendingDown size={16} className="text-vicinity-800" />
                   : <TrendingUp   size={16} className="text-[#a83838]" />}
          <div>
            <p className="font-body text-[12.5px] text-vicinity-800">
              {yoy < 0 ? 'Down' : 'Up'} <strong className="tabular-nums">{Math.abs(yoy).toFixed(1)}%</strong> year-over-year
            </p>
            <p className="font-mono text-[10px] text-vicinity-500 mt-0.5">Crime trend</p>
          </div>
        </div>
      )}

      {ct.offenses?.length > 0 && (
        <div>
          <Label>What kind of incidents</Label>
          <p className="font-body text-[11px] text-vicinity-400 mt-0.5 mb-2">
            Top offense types across {ct.total_scoped?.toLocaleString() ?? 0} historical incidents
            {ct.radius_m ? ` · ${ct.radius_m}m radius` : ''}
          </p>
          <div className="space-y-2 mt-2">
            {ct.offenses.slice(0, 8).map(o => {
              const sevTone = o.severity === 'violent' ? 'negative'
                           : o.severity === 'property' ? 'mixed'
                           : o.severity === 'minor'    ? 'neutral'
                           : 'neutral'
              return (
                <div key={o.offense}>
                  <div className="flex items-center justify-between gap-2 mb-0.5">
                    <div className="min-w-0 flex items-center gap-1.5">
                      <span className="font-body text-[12px] text-vicinity-800 truncate">
                        {o.offense}
                      </span>
                      {o.severity && (
                        <span className={`shrink-0 w-1.5 h-1.5 rounded-full
                          ${o.severity === 'violent' ? 'bg-[#a83838]'
                          : o.severity === 'property' ? 'bg-[#c48a2c]'
                          : 'bg-vicinity-400'}`} />
                      )}
                    </div>
                    <div className="shrink-0 flex items-baseline gap-2">
                      <span className="font-mono text-[11.5px] font-medium text-vicinity-900 tabular-nums">
                        {o.count.toLocaleString()}
                      </span>
                      <span className="font-mono text-[10px] text-vicinity-400 tabular-nums w-10 text-right">
                        {o.pct.toFixed(1)}%
                      </span>
                    </div>
                  </div>
                  <div className="h-1 bg-vicinity-100 rounded-full overflow-hidden">
                    <motion.div
                      className={`h-full rounded-full
                        ${o.severity === 'violent' ? 'bg-[#a83838]'
                        : o.severity === 'property' ? 'bg-vicinity-700'
                        : 'bg-vicinity-500'}`}
                      initial={{ width: 0 }}
                      animate={{ width: `${Math.min(100, o.pct)}%` }}
                      transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }} />
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      <div>
        <Label>Citizen App · Last 48h</Label>
        <p className="font-body text-[11px] text-vicinity-400 mt-0.5 mb-2">
          Real-time incident reports from residents within 500m
        </p>
        <div className="grid grid-cols-3 gap-2">
          <Tile label="Reports" value={sm.citizen_48h ?? 0} />
          <Tile label="Nighttime" value={sm.citizen_nighttime_48h ?? 0} />
          <Tile label="Critical" value={sm.citizen_critical_48h ?? 0}
                tone={(sm.citizen_critical_48h || 0) > 0 ? 'alert' : 'neutral'} />
        </div>
        {(sm.citizen_48h ?? 0) === 0 && (
          <p className="font-body text-[11px] text-vicinity-400 mt-1.5 italic">
            No Citizen reports in last 48h near this location
          </p>
        )}
      </div>

      {dist.total === 0 && (
        <div className="p-3 bg-vicinity-50 rounded-lg border border-vicinity-150">
          <p className="font-body text-[12.5px] text-vicinity-700">
            No historical crime data within {dist.radius_m || 1000}m of this listing.
          </p>
          <p className="font-body text-[11px] text-vicinity-500 mt-1 leading-relaxed">
            This can mean a genuinely quiet spot, or that the listing's coordinates
            fall outside Boston PD's jurisdiction (Cambridge, Somerville, Brookline,
            and Chelsea have separate data sources not covered here).
          </p>
        </div>
      )}

      {hourly.length > 0 && dist.total > 0 && (
        <div>
          <Label>Time of day · when incidents occur</Label>
          <p className="font-body text-[11px] text-vicinity-400 mt-0.5 mb-2">
            Historical distribution
            {histTotal > 0 && (
              <> · {histTotal.toLocaleString()} incidents
                {histStartYear && <> since {histStartYear}</>}
              </>
            )}
          </p>
          <div>
            <SparkBars data={hourly} height={56} ariaLabel="Hourly crime distribution" />
            <div className="flex justify-between font-mono text-[9px] text-vicinity-400 mt-1">
              <span>12a</span><span>6a</span><span>12p</span><span>6p</span><span>11p</span>
            </div>
          </div>
        </div>
      )}

      {dow.length > 0 && dist.total > 0 && (
        <div>
          <Label>Day of week · when incidents cluster</Label>
          <p className="font-body text-[11px] text-vicinity-400 mt-0.5 mb-2">
            Historical weekly pattern
          </p>
          <div>
            <SparkBars data={dow} height={44} ariaLabel="Day-of-week crime distribution" />
            <div className="flex justify-between font-mono text-[9px] text-vicinity-400 mt-1">
              {dow.map(d => <span key={d.label} className="flex-1 text-center">{d.label}</span>)}
            </div>
          </div>
        </div>
      )}

      {yearly.length > 0 && dist.total > 0 && (
        <div>
          <Label>Yearly totals</Label>
          <p className="font-body text-[11px] text-vicinity-400 mt-0.5 mb-2">
            How local crime counts have shifted year to year
          </p>
          <div className="space-y-2 mt-2">
            {yearly.map(y => <HBar key={y.year} label={y.year} count={y.count} max={yearMax} />)}
          </div>
        </div>
      )}

      {monthly.length > 1 && (
        <div>
          <Label>Monthly trend</Label>
          <p className="font-body text-[11px] text-vicinity-400 mt-0.5 mb-2">
            Seasonal pattern by month
          </p>
          <div className="bg-vicinity-50 rounded-lg p-2 border border-vicinity-100">
            <ResponsiveContainer width="100%" height={80}>
              <LineChart data={monthly}>
                <XAxis dataKey="month" tick={{ fontSize: 9, fill: '#8a8a8a' }}
                       axisLine={false} tickLine={false} />
                <YAxis hide />
                <Tooltip contentStyle={{
                  background: '#0a0a0a', border: 'none', borderRadius: 6,
                  fontSize: 11, fontFamily: 'JetBrains Mono', color: '#fff', padding: '4px 8px',
                }} />
                <Line type="monotone" dataKey="crimes" stroke="#0a0a0a" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="violent" stroke="#a83838" strokeWidth={1.5} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {cp && cp.total > 0 && (
        <div>
          <Label>Community perception · Reddit</Label>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {cp.positive > 0 && <Chip tone="positive">{cp.positive} positive</Chip>}
            {cp.mixed    > 0 && <Chip tone="mixed">{cp.mixed} mixed</Chip>}
            {cp.negative > 0 && <Chip tone="negative">{cp.negative} negative</Chip>}
          </div>
          {cp.sample_titles?.length > 0 && (
            <ul className="mt-2.5 space-y-1">
              {cp.sample_titles.slice(0, 3).map((t, i) => (
                <li key={i} className="font-body text-[12px] text-vicinity-600 leading-snug
                                       border-l border-vicinity-200 pl-2 italic">
                  "{t}"
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {scorecard?.length >= 14 && (
        <div>
          <Label>90-day score trend</Label>
          <div className="mt-2 bg-vicinity-50 rounded-lg p-2 border border-vicinity-100">
            <ResponsiveContainer width="100%" height={80}>
              <LineChart data={scorecard.map(r => ({
                date: r.score_date?.slice(5), safety: r.safety_score, livability: r.livability_score,
              }))}>
                <XAxis dataKey="date" tick={{ fontSize: 9, fill: '#8a8a8a' }}
                       axisLine={false} tickLine={false} />
                <YAxis domain={[0, 100]} hide />
                <Tooltip contentStyle={{
                  background: '#0a0a0a', border: 'none', borderRadius: 6,
                  fontSize: 11, fontFamily: 'JetBrains Mono', color: '#fff', padding: '4px 8px',
                }} />
                <Line type="monotone" dataKey="safety" stroke="#0a0a0a" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="livability" stroke="#8a8a8a" strokeWidth={1.5} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  )
}


/* ─── Livability Tab ─────────────────────────────────────── */

function LivabilityTab({ d }) {
  const lm = d.livability_metadata || {}

  const complaints = useMemo(() => ([
    { label: 'noise',   count: lm.noise_count   || 0 },
    { label: 'pest',    count: lm.pest_count    || 0 },
    { label: 'heat',    count: lm.heat_count    || 0 },
    { label: 'housing', count: lm.housing_count || 0 },
    { label: 'infrastructure', count: lm.infra_count || 0 },
  ]), [lm])
  const complaintMax = Math.max(1, ...complaints.map(c => c.count))

  const essPresent = Array.isArray(lm.essentials_present) ? lm.essentials_present : []
  const essMissing = Array.isArray(lm.essentials_missing) ? lm.essentials_missing : []
  const allEss = [...essPresent.map(e => ({ name: e, present: true })),
                  ...essMissing.map(e => ({ name: e, present: false }))]

  const np = lm.noise_perception

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-2">
        <Tile label="311 Complaints" value={lm.complaint_count_total ?? 0} sub="30d · 500m" />
        <Tile label="Weighted Score"
              value={lm.effective_complaint_score != null ? lm.effective_complaint_score.toFixed(1) : '—'}
              sub="QoL × 2 + infra × 1" />
      </div>

      {complaints.some(c => c.count > 0) && (
        <div>
          <Label>Complaint categories</Label>
          <div className="space-y-2 mt-2">
            {complaints.map(c => <HBar key={c.label} {...c} max={complaintMax} />)}
          </div>
        </div>
      )}

      {allEss.length > 0 && (
        <div>
          <Label>Essentials within 1km · {essPresent.length}/{allEss.length}</Label>
          <div className="grid grid-cols-2 gap-1.5 mt-2">
            {allEss.map(e => (
              <div key={e.name}
                   className={`flex items-center gap-2 px-2.5 py-1.5 rounded-md border
                               ${e.present
                                 ? 'bg-vicinity-50 border-vicinity-200'
                                 : 'bg-vicinity-white border-vicinity-100 opacity-60'}`}>
                {e.present
                  ? <Check size={12} className="text-vicinity-black" />
                  : <X size={12} className="text-vicinity-400" />}
                <span className={`font-body text-[12px] capitalize
                                  ${e.present ? 'text-vicinity-800' : 'text-vicinity-400'}`}>
                  {e.name.replace(/_/g, ' ')}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {lm.total_amenities != null && (
        <div>
          <Label>Amenity density</Label>
          <p className="font-display text-[22px] font-bold text-vicinity-black mt-1 tabular-nums">
            {lm.total_amenities.toLocaleString()}
            <span className="font-body text-[12px] text-vicinity-400 font-normal ml-1.5">
              within scoring radius
            </span>
          </p>
        </div>
      )}

      {np && np.total > 0 && (
        <div>
          <Label>Noise perception · Reddit</Label>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {np.positive > 0 && <Chip tone="positive">{np.positive} quiet</Chip>}
            {np.mixed    > 0 && <Chip tone="mixed">{np.mixed} mixed</Chip>}
            {np.negative > 0 && <Chip tone="negative">{np.negative} noisy</Chip>}
          </div>
          {np.sample_titles?.length > 0 && (
            <ul className="mt-2.5 space-y-1">
              {np.sample_titles.slice(0, 3).map((t, i) => (
                <li key={i} className="font-body text-[12px] text-vicinity-600 leading-snug
                                       border-l border-vicinity-200 pl-2 italic">
                  "{t}"
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}


/* ─── Neighborhood Tab ───────────────────────────────────── */

function NarrativeCard({ n }) {
  const [open, setOpen] = useState(false)
  const hasBody = !!n.raw_thread_text && n.raw_thread_text.length > 40
  return (
    <article className="card-editorial px-3.5 py-3">
      <div className="flex items-start justify-between gap-2 mb-1.5">
        <div className="min-w-0 flex-1">
          {n.url ? (
            <a href={n.url} target="_blank" rel="noopener noreferrer"
               className="font-body text-[13px] font-semibold text-vicinity-black
                          hover:underline decoration-vicinity-300 underline-offset-2 line-clamp-2">
              {n.title || 'Untitled'}
            </a>
          ) : (
            <p className="font-body text-[13px] font-semibold text-vicinity-black line-clamp-2">
              {n.title || 'Untitled'}
            </p>
          )}
        </div>
        {n.sentiment && <Chip tone={n.sentiment}>{n.sentiment}</Chip>}
      </div>

      <div className="flex items-center gap-2 font-mono text-[10.5px] text-vicinity-500">
        {n.signal_source && <span className="uppercase tracking-wider">{n.signal_source}</span>}
        {n.subreddit && <span>· r/{n.subreddit}</span>}
        {n.discussion_date && <span>· {n.discussion_date}</span>}
        {typeof n.post_score === 'number' && <span>· ↑{n.post_score}</span>}
      </div>

      {n.snippet_text && (
        <p className="font-body text-[12px] text-vicinity-600 leading-[1.55] line-clamp-3 mt-2">
          {n.snippet_text}
        </p>
      )}

      {hasBody && (
        <>
          <button onClick={() => setOpen(!open)}
            className="mt-2 font-body text-[11px] text-vicinity-500 hover:text-vicinity-black
                       transition-colors inline-flex items-center gap-1">
            {open ? 'Hide thread' : 'Read thread'}
            <ChevronDown size={11} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
          </button>
          <AnimatePresence>
            {open && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden">
                <p className="mt-2 pt-2 border-t border-vicinity-100
                              font-body text-[11.5px] text-vicinity-600 leading-[1.55]
                              whitespace-pre-wrap">
                  {n.raw_thread_text}
                </p>
              </motion.div>
            )}
          </AnimatePresence>
        </>
      )}
    </article>
  )
}

function NeighborhoodTab({ d, narratives }) {
  const overlay = d.lifestyle_overlay

  const tags = useMemo(() => {
    if (!overlay || typeof overlay !== 'object') return []
    return Object.entries(overlay)
      .map(([tag, data]) => ({ tag, ...data }))
      .filter(t => t.total > 0)
      .sort((a, b) => b.total - a.total)
  }, [overlay])

  if (!tags.length && !narratives?.length) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <Users size={22} className="text-vicinity-300 mb-3" />
        <p className="font-body text-[13px] text-vicinity-500 max-w-[240px]">
          No community discussions tagged to this neighborhood yet.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      {tags.length > 0 && (
        <div>
          <Label>Discussion topics</Label>
          <div className="mt-2 space-y-2">
            {tags.slice(0, 6).map(t => (
              <div key={t.tag} className="flex items-center gap-3">
                <span className="font-body text-[12px] text-vicinity-700 capitalize w-24 shrink-0">
                  {t.tag.replace(/_/g, ' ')}
                </span>
                <div className="flex-1 flex gap-0.5 h-2 rounded-full overflow-hidden bg-vicinity-100">
                  {t.positive > 0 && <div className="bg-vicinity-700" style={{ flex: t.positive }} />}
                  {t.mixed    > 0 && <div className="bg-vicinity-400" style={{ flex: t.mixed    }} />}
                  {t.negative > 0 && <div className="bg-[#a83838]" style={{ flex: t.negative }} />}
                </div>
                <span className="font-mono text-[11px] text-vicinity-500 tabular-nums w-8 text-right">
                  {t.total}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {narratives?.length > 0 && (
        <div>
          <Label>Recent discussions · {narratives.length}</Label>
          <div className="mt-2 space-y-2.5">
            {narratives.slice(0, 8).map(n => <NarrativeCard key={n.signal_id} n={n} />)}
          </div>
        </div>
      )}
    </div>
  )
}


/* ─── Commute Tab ────────────────────────────────────────── */

function CommuteTab({ d }) {
  const stops = Array.isArray(d.nearest_stops) ? d.nearest_stops : []
  const routes = useStore(s => s.focusedRoutes)

  if (!stops.length && !routes?.length) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <Route size={22} className="text-vicinity-300 mb-3" />
        <p className="font-body text-[13px] text-vicinity-500 max-w-[240px]">
          Ask the chat to add a commute destination. Routes track safety daily
          along the path you take.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      {stops.length > 0 && (
        <div>
          <Label>Nearest transit · {stops.length}</Label>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {stops.map((s, i) =>
              <Chip key={i}><Train size={10} /> {typeof s === 'string' ? s : s.name || s}</Chip>
            )}
          </div>
        </div>
      )}

      {routes?.length > 0 && (
        <div>
          <Label>Your routes · {routes.length}</Label>
          <div className="mt-2 space-y-2">
            {routes.map(r => (
              <div key={r.route_id} className="p-3 rounded-lg border border-vicinity-150">
                <div className="flex items-center justify-between">
                  <p className="font-body text-[13px] font-medium text-vicinity-black">
                    → {r.dest_label}
                  </p>
                  <span className="font-mono text-[12px] text-vicinity-700 font-medium tabular-nums">
                    {r.duration_min?.toFixed(0)}min
                  </span>
                </div>
                <p className="font-body text-[11px] text-vicinity-400 truncate">{r.dest_address}</p>
                <div className="flex gap-3 mt-1.5">
                  <span className="font-mono text-[10px] text-vicinity-500">{r.travel_mode}</span>
                  {r.distance_text && (
                    <span className="font-mono text-[10px] text-vicinity-400">{r.distance_text}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}


/* ─── Main panel ─────────────────────────────────────────── */

const TABS = [
  { k: 'overview',     label: 'Overview' },
  { k: 'safety',       label: 'Safety' },
  { k: 'livability',   label: 'Livability' },
  { k: 'neighborhood', label: 'Area' },
  { k: 'commute',      label: 'Commute' },
]

export default function ListingDetail() {
  const sel = useStore(s => s.selectedListing)
  const detail = useStore(s => s.detail)
  const scorecard = useStore(s => s.scorecard)
  const narratives = useStore(s => s.narratives)
  const distribution = useStore(s => s.distribution)
  const crimeTypes = useStore(s => s.crimeTypes)
  const loading = useStore(s => s.detailLoading)
  const clear = useStore(s => s.clearSelectedListing)
  const tab = useStore(s => s.detailTab)
  const setTab = useStore(s => s.setDetailTab)
  const fetchDetail = useStore(s => s.fetchDetail)
  const focusedRoutes = useStore(s => s.focusedRoutes)
  const composeListingSeed = useStore(s => s.composeListingSeed)
  const askVicinityAboutListing = useStore(s => s.askVicinityAboutListing)

  const d = detail || sel
  const visible = !!sel

  // Ask Vicinity modal state
  const [askOpen, setAskOpen] = useState(false)
  const [askEditing, setAskEditing] = useState(false)
  const [askDraft, setAskDraft] = useState('')

  const openAskModal = () => {
    const seed = composeListingSeed(d, focusedRoutes)
    setAskDraft(seed)
    setAskEditing(false)
    setAskOpen(true)
  }

  const sendAsk = async () => {
    const text = askDraft.trim()
    if (!text) return
    setAskOpen(false)
    setAskEditing(false)
    await askVicinityAboutListing(text)
  }

  useEffect(() => {
    if (sel?.listing_id) {
      setTab('overview')
      fetchDetail(sel.listing_id)
    }
  }, [sel?.listing_id])

  return (
    <AnimatePresence>
      {visible && (
        <motion.aside
          initial={{ opacity: 0, x: -16 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: -16 }}
          transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
          className="absolute top-4 left-4 bottom-4 w-[420px] z-30
                     panel-lifted flex flex-col overflow-hidden">
          <div className="shrink-0 px-4 pt-3.5 pb-0 border-b border-vicinity-100">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="lbl">Listing</p>
                <h2 className="font-display text-[17px] font-bold text-vicinity-black
                               leading-tight truncate mt-0.5">
                  {d?.street || 'Loading…'}
                </h2>
              </div>
              <div className="shrink-0 flex items-center gap-1">
                <button onClick={openAskModal}
                  disabled={!d?.listing_id}
                  title="Ask Vicinity about this listing"
                  className="inline-flex items-center gap-1.5 px-2.5 py-1.5
                             font-body text-[11.5px] font-medium
                             bg-vicinity-black text-vicinity-white rounded-md
                             hover:bg-vicinity-800 active:scale-95
                             disabled:opacity-30 disabled:cursor-not-allowed
                             transition-all">
                  <MessageSquare size={12} />
                  Ask Vicinity
                </button>
                <button onClick={clear}
                  className="p-1.5 -mr-1 text-vicinity-400 hover:text-vicinity-black
                             transition-colors rounded-md hover:bg-vicinity-50">
                  <X size={15} />
                </button>
              </div>
            </div>

            <div className="flex items-center gap-0.5 mt-2.5 -mb-px overflow-x-auto">
              {TABS.map(t => (
                <button key={t.k} onClick={() => setTab(t.k)}
                  data-active={tab === t.k}
                  className="tab-btn shrink-0">
                  {t.label}
                </button>
              ))}
            </div>
          </div>

          <div className="flex-1 overflow-y-auto px-4 py-4">
            {loading && !detail ? (
              <div className="flex flex-col items-center justify-center py-16">
                <div className="w-5 h-5 border-2 border-vicinity-200 border-t-vicinity-black
                                rounded-full animate-spin" />
              </div>
            ) : !d ? (
              <div className="flex flex-col items-center justify-center py-12 text-center">
                <AlertCircle size={22} className="text-vicinity-300 mb-2" />
                <p className="font-body text-[13px] text-vicinity-500">
                  Could not load this listing.
                </p>
              </div>
            ) : (
              <motion.div
                key={tab}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.18 }}>
                {tab === 'overview'     && <OverviewTab     d={d} />}
                {tab === 'safety'       && <SafetyTab       d={d} scorecard={scorecard} distribution={distribution} crimeTypes={crimeTypes} />}
                {tab === 'livability'   && <LivabilityTab   d={d} />}
                {tab === 'neighborhood' && <NeighborhoodTab d={d} narratives={narratives} />}
                {tab === 'commute'      && <CommuteTab      d={d} />}
              </motion.div>
            )}
          </div>

          <AnimatePresence>
            {askOpen && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.15 }}
                className="absolute inset-0 bg-vicinity-black/45 backdrop-blur-sm z-40
                           flex items-center justify-center p-4"
                onClick={() => setAskOpen(false)}>
                <motion.div
                  initial={{ opacity: 0, y: 8, scale: 0.98 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: 4, scale: 0.98 }}
                  transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
                  onClick={e => e.stopPropagation()}
                  className="w-full max-w-[380px] bg-vicinity-white rounded-xl
                             shadow-2xl border border-vicinity-200 overflow-hidden">

                  <div className="px-4 pt-3.5 pb-3 border-b border-vicinity-100">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="lbl">Ask Vicinity</p>
                        <h3 className="font-display text-[15px] font-bold text-vicinity-black mt-0.5">
                          Send this to the agent?
                        </h3>
                      </div>
                      <button onClick={() => setAskOpen(false)}
                        className="p-1 text-vicinity-400 hover:text-vicinity-black
                                   transition-colors rounded-md hover:bg-vicinity-50">
                        <X size={13} />
                      </button>
                    </div>
                  </div>

                  <div className="p-4">
                    {askEditing ? (
                      <textarea
                        value={askDraft}
                        onChange={e => setAskDraft(e.target.value)}
                        rows={5}
                        autoFocus
                        className="w-full resize-none px-3 py-2.5 font-body text-[13px]
                                   leading-[1.55] text-vicinity-800
                                   bg-vicinity-50 border border-vicinity-200 rounded-md
                                   focus:outline-none focus:ring-2 focus:ring-vicinity-black
                                   focus:border-transparent transition-all"
                        style={{ minHeight: 110 }} />
                    ) : (
                      <div className="px-3 py-2.5 bg-vicinity-50 border border-vicinity-150
                                      rounded-md font-body text-[13px] leading-[1.55]
                                      text-vicinity-800 whitespace-pre-wrap break-words">
                        {askDraft}
                      </div>
                    )}

                    <p className="font-mono text-[10px] text-vicinity-400 mt-2">
                      This opens a chat with the Vicinity agent and sends the message above.
                      {focusedRoutes?.length > 0 && ' Your configured routes are included.'}
                    </p>
                  </div>

                  <div className="px-4 pb-3.5 pt-1 flex items-center justify-between gap-2">
                    <button
                      onClick={() => setAskEditing(v => !v)}
                      className="inline-flex items-center gap-1.5 px-3 py-2
                                 font-body text-[12px] text-vicinity-600
                                 border border-vicinity-200 rounded-md
                                 hover:bg-vicinity-50 hover:text-vicinity-black
                                 transition-colors">
                      <Edit2 size={11} />
                      {askEditing ? 'Preview' : 'Edit'}
                    </button>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => setAskOpen(false)}
                        className="px-3 py-2 font-body text-[12px] text-vicinity-500
                                   hover:text-vicinity-black transition-colors">
                        Cancel
                      </button>
                      <button
                        onClick={sendAsk}
                        disabled={!askDraft.trim()}
                        className="inline-flex items-center gap-1.5 px-4 py-2
                                   bg-vicinity-black text-vicinity-white
                                   font-body text-[12.5px] font-medium rounded-md
                                   hover:bg-vicinity-800 active:scale-95
                                   disabled:opacity-30 disabled:cursor-not-allowed
                                   transition-all">
                        <Send size={12} />
                        Send to chat
                      </button>
                    </div>
                  </div>
                </motion.div>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.aside>
      )}
    </AnimatePresence>
  )
}