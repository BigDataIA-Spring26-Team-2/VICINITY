import { useEffect, useState, useMemo, useRef } from 'react'
import { APIProvider, Map as GoogleMap, AdvancedMarker, useMap } from '@vis.gl/react-google-maps'
import { AnimatePresence, motion } from 'framer-motion'
import { Layers, MapPin, Train, Shield, Coffee, X } from 'lucide-react'
import { useStore } from './store'

const MAPS_KEY = import.meta.env.VITE_GOOGLE_MAPS_KEY || ''
const MAP_ID = import.meta.env.VITE_GOOGLE_MAP_ID || 'DEMO_MAP_ID'

const STYLES = [
  { elementType: 'geometry', stylers: [{ color: '#f5f5f5' }] },
  { elementType: 'labels.icon', stylers: [{ visibility: 'off' }] },
  { elementType: 'labels.text.fill', stylers: [{ color: '#999' }] },
  { elementType: 'labels.text.stroke', stylers: [{ color: '#fefefe' }] },
  { featureType: 'poi', stylers: [{ visibility: 'off' }] },
  { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#e8e8e8' }] },
  { featureType: 'road.highway', elementType: 'geometry', stylers: [{ color: '#ddd' }] },
  { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#cdd' }] },
]

function ListingPin({ listing, isFocused, dimmed, onClick }) {
  const score = listing.safety_score
  const shade = score != null ? Math.round(30 + (1 - score / 100) * 180) : 120
  if (isFocused) {
    return (
      <AdvancedMarker position={{ lat: listing.lat, lng: listing.lon }} onClick={() => onClick(listing)} zIndex={100}>
        <div className="relative cursor-pointer">
          <span className="absolute inset-0 -m-4 rounded-full border-2 border-gray-900 animate-ping opacity-20" />
          <span className="absolute inset-0 -m-2 rounded-full border-2 border-gray-900 opacity-50" />
          <div className="w-9 h-9 rounded-full bg-gray-900 border-2 border-white shadow-lg flex items-center justify-center">
            <span className="font-mono text-[8px] text-white font-bold">${listing.price ? Math.round(listing.price / 100) : '?'}</span>
          </div>
        </div>
      </AdvancedMarker>
    )
  }
  return (
    <AdvancedMarker position={{ lat: listing.lat, lng: listing.lon }} onClick={() => onClick(listing)}>
      <div className={`cursor-pointer group transition-opacity ${dimmed ? 'opacity-20' : 'opacity-100'}`}>
        <div className="w-6 h-6 rounded-full border-2 border-white shadow flex items-center justify-center group-hover:scale-110 transition-transform"
             style={{ background: `rgb(${shade},${shade},${shade})` }}>
          {listing.price && <span className="font-mono text-[7px] text-white font-bold">{Math.round(listing.price / 100)}</span>}
        </div>
      </div>
    </AdvancedMarker>
  )
}

function TransitPin({ stop }) {
  return <AdvancedMarker position={{ lat: stop.lat, lng: stop.lon }}>
    <div className="w-3.5 h-3.5 rounded-sm bg-gray-600 border border-white shadow-sm flex items-center justify-center">
      <span className="text-white text-[6px] font-bold">T</span>
    </div>
  </AdvancedMarker>
}

function AmenityPin({ amenity }) {
  return <AdvancedMarker position={{ lat: amenity.lat, lng: amenity.lon }}>
    <div className="w-2.5 h-2.5 rounded-full bg-gray-400 border border-white" />
  </AdvancedMarker>
}

// Crime dots — ACTUALLY VISIBLE. Even non_crime gets a visible dot.
function CrimeDot({ crime }) {
  const cfg = {
    violent:   { s: 12, o: 0.9, ring: true },
    property:  { s: 8,  o: 0.6, ring: false },
    minor:     { s: 6,  o: 0.4, ring: false },
    non_crime: { s: 5,  o: 0.3, ring: false },
  }
  const c = cfg[crime.severity] || cfg.minor
  return <AdvancedMarker position={{ lat: crime.lat, lng: crime.lon }}>
    <div className="relative">
      {c.ring && <span className="absolute inset-0 -m-1 rounded-full border border-gray-900 opacity-30" />}
      <div className="rounded-full" style={{ width: c.s, height: c.s, background: `rgba(30,30,30,${c.o})` }} />
    </div>
  </AdvancedMarker>
}

// Route graph — polylines + small hot spot dots + destination labels
function RouteGraph() {
  const map = useMap()
  const routes = useStore(s => s.focusedRoutes)
  const listing = useStore(s => s.selectedListing)
  const refs = useRef([])

  useEffect(() => {
    refs.current.forEach(r => { if (r?.setMap) r.setMap(null) }); refs.current = []
    if (!map || !routes.length || !window.google) return
    const bounds = new window.google.maps.LatLngBounds()
    if (listing?.lat && listing?.lon) bounds.extend({ lat: listing.lat, lng: listing.lon })

    for (const route of routes) {
      let wps = route.waypoints; if (typeof wps === 'string') try { wps = JSON.parse(wps) } catch { continue }
      if (!Array.isArray(wps) || wps.length < 2) continue
      const path = wps.map(w => ({ lat: w.lat, lng: w.lon || w.lng }))
      path.forEach(p => bounds.extend(p))

      // Polyline
      refs.current.push(new window.google.maps.Polyline({ path, geodesic: true, strokeColor: '#333', strokeOpacity: 0.6, strokeWeight: 3, map }))

      // Hot spots — SMALL dots, max 8px
      let scores = route.waypoint_scores; if (typeof scores === 'string') try { scores = JSON.parse(scores) } catch { scores = [] }
      if (Array.isArray(scores)) {
        for (const hs of scores) {
          const intensity = Math.min((hs.crimes || 0) + (hs.violent || 0) * 2, 10)
          refs.current.push(new window.google.maps.Marker({
            position: { lat: hs.lat, lng: hs.lon || hs.lng }, map,
            icon: { path: window.google.maps.SymbolPath.CIRCLE, scale: 3 + intensity * 0.5, fillColor: '#222', fillOpacity: 0.4 + intensity * 0.04, strokeColor: '#fff', strokeWeight: 1 },
            title: `Crimes: ${hs.crimes || 0}`,
          }))
        }
      }

      // Destination
      if (route.dest_lat && route.dest_lon) {
        bounds.extend({ lat: route.dest_lat, lng: route.dest_lon })
        refs.current.push(new window.google.maps.Marker({
          position: { lat: route.dest_lat, lng: route.dest_lon }, map,
          label: { text: (route.dest_label || 'D').slice(0, 3), color: '#fff', fontSize: '9px', fontWeight: '700' },
          icon: { path: window.google.maps.SymbolPath.CIRCLE, scale: 12, fillColor: '#111', fillOpacity: 1, strokeColor: '#fff', strokeWeight: 2 },
        }))
      }
    }
    if (refs.current.length > 0) map.fitBounds(bounds, 60)
    return () => { refs.current.forEach(r => { if (r?.setMap) r.setMap(null) }); refs.current = [] }
  }, [map, routes, listing])
  return null
}

function FocusBanner() {
  const fid = useStore(s => s.focusedListingId)
  const listing = useStore(s => s.selectedListing)
  const routes = useStore(s => s.focusedRoutes)
  const clear = useStore(s => s.clearFocus)
  if (!fid) return null
  return (
    <div className="absolute top-4 right-4 z-20 bg-white border border-gray-200 rounded-lg shadow-lg px-4 py-3 max-w-[280px] animate-fade-in">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="font-body text-sm font-bold text-gray-900">{listing?.street || 'Focused Listing'}</p>
          {listing?.price && <p className="font-mono text-xs text-gray-500">${listing.price.toLocaleString()}/mo</p>}
          {routes.length > 0 && <div className="mt-2 space-y-0.5">{routes.map(r => <p key={r.route_id} className="font-mono text-xs text-gray-600">→ {r.dest_label} · {r.duration_min?.toFixed(0)}min</p>)}</div>}
          {routes.length === 0 && <p className="font-body text-xs text-gray-400 mt-1 italic">No routes configured</p>}
        </div>
        <button onClick={clear} className="p-1 text-gray-400 hover:text-gray-900 transition-colors shrink-0"><X size={14} /></button>
      </div>
    </div>
  )
}

function LayerControls() {
  const layers = useStore(s => s.layers); const toggle = useStore(s => s.toggleLayer); const [open, setOpen] = useState(false)
  const defs = [{ k: 'listings', l: 'Listings', i: MapPin }, { k: 'transit', l: 'Transit', i: Train }, { k: 'crimes', l: 'Crime', i: Shield }, { k: 'amenities', l: 'Amenities', i: Coffee }]
  return (
    <div className="absolute top-4 left-4 z-20">
      <button onClick={() => setOpen(!open)} className="w-10 h-10 bg-white border border-gray-200 rounded-lg shadow-md flex items-center justify-center hover:bg-gray-50 transition-colors"><Layers size={16} className="text-gray-600" /></button>
      <AnimatePresence>{open && (
        <motion.div initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.1 }}
          className="absolute top-12 left-0 bg-white border border-gray-200 rounded-lg shadow-lg p-1.5 min-w-[160px] space-y-0.5">
          {defs.map(({ k, l, i: I }) => (
            <button key={k} onClick={() => toggle(k)} className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-md font-body text-sm transition-colors ${layers[k] ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-100'}`}><I size={14} /> {l}</button>
          ))}
        </motion.div>
      )}</AnimatePresence>
    </div>
  )
}

function MapContent({ onListingClick }) {
  const map = useMap()
  const listings = useStore(s => s.listings)
  const transit = useStore(s => s.transitStops)
  const crimes = useStore(s => s.crimePoints)
  const amenities = useStore(s => s.amenities)
  const layers = useStore(s => s.layers)
  const fid = useStore(s => s.focusedListingId)
  const fetchListings = useStore(s => s.fetchListings)
  const fetchTransit = useStore(s => s.fetchTransit)
  const fetchCrimes = useStore(s => s.fetchCrimes)
  const fetchAmenities = useStore(s => s.fetchAmenities)
  const center = useStore(s => s.mapCenter)

  useEffect(() => { fetchListings(); fetchTransit() }, [])
  useEffect(() => { if (map && center) map.panTo(center) }, [map, center.lat, center.lng])
  useEffect(() => {
    if (layers.crimes) fetchCrimes(center.lat, center.lng)
    if (layers.amenities) fetchAmenities(center.lat, center.lng)
  }, [layers.crimes, layers.amenities, center.lat, center.lng])

  return <>
    {/* Listings always show */}
    {layers.listings && listings.map(l => <ListingPin key={l.listing_id} listing={l} isFocused={l.listing_id === fid} dimmed={!!fid && l.listing_id !== fid} onClick={onListingClick} />)}
    {/* Crime/transit/amenities show when toggled on — EVEN when focused */}
    {layers.transit && transit.map(s => <TransitPin key={s.stop_id} stop={s} />)}
    {layers.crimes && crimes.map((c, i) => <CrimeDot key={c.incident_id || i} crime={c} />)}
    {layers.amenities && amenities.map((a, i) => <AmenityPin key={a.osm_id || i} amenity={a} />)}
    <RouteGraph />
  </>
}

export default function MapView({ onListingClick }) {
  const center = useStore(s => s.mapCenter); const zoom = useStore(s => s.mapZoom)
  if (!MAPS_KEY) return <div className="h-full flex items-center justify-center bg-gray-50"><p className="font-body text-lg text-gray-300">Set VITE_GOOGLE_MAPS_KEY in .env</p></div>
  return (
    <div className="relative h-full w-full">
      <APIProvider apiKey={MAPS_KEY}>
        <GoogleMap defaultCenter={center} defaultZoom={zoom} mapId={MAP_ID} gestureHandling="greedy" disableDefaultUI styles={STYLES} className="h-full w-full">
          <MapContent onListingClick={onListingClick} />
        </GoogleMap>
      </APIProvider>
      <LayerControls />
      <FocusBanner />
    </div>
  )
}