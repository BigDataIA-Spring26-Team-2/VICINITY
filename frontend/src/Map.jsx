import { useEffect, useState, useRef, useMemo } from 'react'
import {
  APIProvider, Map as GoogleMap, AdvancedMarker, useMap, useMapsLibrary,
} from '@vis.gl/react-google-maps'
import { MarkerClusterer } from '@googlemaps/markerclusterer'
import { AnimatePresence, motion } from 'framer-motion'
import {
  Layers, MapPin, Train, Shield, Coffee, X, Bookmark,
  Utensils, Dumbbell, BookOpen, ShoppingCart, Pill, WashingMachine,
  Store, Trees,
} from 'lucide-react'
import { useStore } from './store'

const MAPS_KEY = import.meta.env.VITE_GOOGLE_MAPS_KEY || ''
const MAP_ID = import.meta.env.VITE_GOOGLE_MAP_ID || 'DEMO_MAP_ID'

// Editorial palette
const BOOKMARK_GOLD = '#c48a2c'
const BOOKMARK_GOLD_DEEP = '#8f6218'
const FOCUS_BLACK = '#0a0a0a'

// Route palette — vivid teal, distinct from grayscale/gold/black.
// Signals "active navigation path" so routes dominate visually when
// a listing is focused with commute setup. Waypoint-score hotspots
// use a separate amber → red gradient to represent crime intensity
// along the path without competing with the polyline color.
const ROUTE_TEAL          = '#0d9488'   // polyline + endpoint fill
const ROUTE_TEAL_DEEP     = '#115e59'   // endpoint border
const ROUTE_HOTSPOT_LOW   = '#f59e0b'   // amber — few crimes at waypoint
const ROUTE_HOTSPOT_MID   = '#ea580c'   // orange
const ROUTE_HOTSPOT_HIGH  = '#b91c1c'   // crimson — high intensity

// Dimming levels when a listing is focused. Numbers tuned so the
// focused pin visually dominates without the rest of the map
// becoming invisible — spatial context matters.
const DIM_DEFAULT     = 0.18   // cluster + individual default pins
const DIM_BOOKMARKED  = 0.55   // bookmarks stay more visible (user cares)
const DIM_TRANSIT     = 0.25
const DIM_AMENITY     = 0.30
const DIM_HEATMAP     = 0.35

const STYLES = [
  { elementType: 'geometry',        stylers: [{ color: '#f0f0f0' }] },
  { elementType: 'labels.icon',     stylers: [{ visibility: 'off' }] },
  { elementType: 'labels.text.fill',   stylers: [{ color: '#8a8a8a' }] },
  { elementType: 'labels.text.stroke', stylers: [{ color: '#fefefe' }] },
  { featureType: 'poi',             stylers: [{ visibility: 'off' }] },
  { featureType: 'road',            elementType: 'geometry', stylers: [{ color: '#e5e5e5' }] },
  { featureType: 'road.highway',    elementType: 'geometry', stylers: [{ color: '#d4d4d4' }] },
  { featureType: 'water',           elementType: 'geometry', stylers: [{ color: '#cdd5d5' }] },
  { featureType: 'transit',         elementType: 'geometry', stylers: [{ color: '#e5e5e5' }] },
]


/* ─── Helpers ──────────────────────────────────────────────── */

/** Map safety score 0-100 to grayscale RGB. Lower score (less safe)
 *  = darker. Higher score = lighter. Same curve individual pins used. */
function scoreToShade(score) {
  if (score == null) return 120
  return Math.round(30 + (1 - score / 100) * 180)
}


/* ─── Focused + Bookmarked pins (React-managed) ───────────── */

function FocusedPin({ listing, isBookmarked, onClick }) {
  return (
    <AdvancedMarker position={{ lat: listing.lat, lng: listing.lon }}
      onClick={() => onClick(listing)} zIndex={1000}>
      <div className="relative cursor-pointer">
        <span className="absolute inset-0 -m-5 rounded-full border-2
                         animate-pulse-ring opacity-30"
              style={{ borderColor: FOCUS_BLACK }} />
        <span className="absolute inset-0 -m-2.5 rounded-full border opacity-60"
              style={{ borderColor: FOCUS_BLACK }} />
        <div className="w-10 h-10 rounded-full border-2 border-vicinity-white
                        shadow-lg flex items-center justify-center relative"
             style={{ background: FOCUS_BLACK }}>
          <span className="font-mono text-[9px] text-vicinity-white font-bold tabular-nums">
            {listing.price ? `$${Math.round(listing.price / 100) / 10}k` : '—'}
          </span>
          {isBookmarked && (
            <span
              className="absolute -top-1 -right-1 w-3 h-3 rounded-full
                         border-2 border-vicinity-white shadow"
              style={{ background: BOOKMARK_GOLD }}
              title="Bookmarked" />
          )}
        </div>
      </div>
    </AdvancedMarker>
  )
}

function BookmarkedPin({ listing, dimmed, onClick }) {
  return (
    <AdvancedMarker position={{ lat: listing.lat, lng: listing.lon }}
      onClick={() => onClick(listing)} zIndex={500}>
      <div className="cursor-pointer group transition-opacity duration-200"
           style={{ opacity: dimmed ? DIM_BOOKMARKED : 1 }}>
        <div className="w-7 h-7 rounded-full border-2 shadow-md
                        flex items-center justify-center
                        group-hover:scale-110 transition-transform duration-150"
             style={{ background: BOOKMARK_GOLD, borderColor: BOOKMARK_GOLD_DEEP }}>
          <Bookmark size={10} className="text-vicinity-white"
                    fill="currentColor" strokeWidth={0} />
        </div>
        {listing.price && (
          <div className="absolute -bottom-4 left-1/2 -translate-x-1/2
                          font-mono text-[9px] font-semibold tabular-nums
                          px-1 rounded bg-vicinity-white/95
                          whitespace-nowrap shadow-sm"
               style={{ color: BOOKMARK_GOLD_DEEP }}>
            ${Math.round(listing.price / 100) / 10}k
          </div>
        )}
      </div>
    </AdvancedMarker>
  )
}


/* ─── ClusteredListings ────────────────────────────────────────
 *
 * Default listings (not focused, not bookmarked) rendered via
 * MarkerClusterer. Each marker carries its listing on `_listing`
 * so the cluster renderer can compute an average safety score
 * and color itself accordingly.
 *
 * Dimming: when `dimmed` is true, every marker AND cluster gets
 * opacity DIM_DEFAULT. Handled at the DOM-element level, not
 * through CSS descendant selectors (because Google Maps markers
 * are siblings of our React tree, not children).
 *
 * Cluster color: average of member listings' safety_score,
 * mapped through the same shade curve used for individual pins.
 * Sparse clusters in safe neighborhoods render near-white;
 * dense clusters in high-crime areas render near-black. Same
 * information design as individual pins, preserved at cluster
 * level — Zillow / Redfin pattern.
 *
 * Cluster size: log-scaled on count (sqrt) so the difference
 * between 10 and 100 members is visible without 500+ becoming
 * dominant.
 */

function ClusteredListings({ listings, onListingClick, visible, dimmed }) {
  const map = useMap()
  const markerLib = useMapsLibrary('marker')
  const clustererRef = useRef(null)
  const markersRef = useRef([])
  const dimmedRef = useRef(dimmed)

  // Keep a live ref to `dimmed` so the cluster renderer callback
  // (which is captured at clusterer creation) can read the current
  // value without us recreating the whole clusterer on every focus.
  dimmedRef.current = dimmed

  // Build + mount the clusterer once per listings list change.
  useEffect(() => {
    if (!map || !markerLib) return
    if (!visible) {
      if (clustererRef.current) {
        clustererRef.current.clearMarkers()
      }
      markersRef.current = []
      return
    }
    if (!listings?.length) return

    const markers = listings.map(l => {
      const shade = scoreToShade(l.safety_score)

      const pin = document.createElement('div')
      pin.className = 'cluster-pin'
      pin.dataset.role = 'listing'
      // Double-ring treatment: dark outer ring (#2a2a2a) around white
      // inner border around the grayscale shade. Ensures the pin stays
      // visible against the light #f0f0f0 map background even when the
      // listing itself has a light score-based fill. Matches how Zillow/
      // Redfin draw their pins — always has a dark silhouette so the
      // pin reads as a discrete object regardless of its interior color.
      pin.style.cssText = `
        width: 22px; height: 22px; border-radius: 50%;
        background: rgb(${shade},${shade},${shade});
        border: 1.5px solid #fefefe;
        box-shadow:
          0 0 0 1.5px #2a2a2a,
          0 2px 4px rgba(0,0,0,0.25);
        display: flex; align-items: center; justify-content: center;
        cursor: pointer;
        transition: transform 140ms ease, opacity 200ms ease;
      `
      if (l.price) {
        const label = document.createElement('span')
        label.textContent = String(Math.round(l.price / 100))
        label.style.cssText = `
          font-family: 'JetBrains Mono', ui-monospace, monospace;
          font-size: 7px; color: #fefefe; font-weight: 700;
          font-variant-numeric: tabular-nums;
          pointer-events: none;
        `
        pin.appendChild(label)
      }
      pin.addEventListener('mouseenter', () => { pin.style.transform = 'scale(1.15)' })
      pin.addEventListener('mouseleave', () => { pin.style.transform = 'scale(1)' })

      const marker = new markerLib.AdvancedMarkerElement({
        position: { lat: l.lat, lng: l.lon },
        content: pin,
      })

      marker.addListener('click', () => onListingClick(l))
      marker._listing = l
      marker._pinEl = pin     // stash for dimming updates
      return marker
    })

    markersRef.current = markers

    if (clustererRef.current) {
      clustererRef.current.clearMarkers()
    }

    clustererRef.current = new MarkerClusterer({
      map,
      markers,
      renderer: {
        render: ({ count, position, markers: clusterMarkers }) => {
          // Aggregate safety score across all listings in this cluster.
          // Use the same shade curve as individual pins — information
          // continuity: color means the same thing at every zoom.
          let sum = 0, n = 0
          for (const m of clusterMarkers) {
            const s = m._listing?.safety_score
            if (typeof s === 'number') { sum += s; n++ }
          }
          const avgScore = n > 0 ? sum / n : null
          const shade = scoreToShade(avgScore)

          // Size: log-scaled on count. 28px min, 52px max.
          const size = Math.round(Math.min(52, 28 + Math.sqrt(count) * 2.2))

          // Text color: contrast against the shade. Dark shades need
          // white text, light shades need dark text.
          const textColor = shade < 130 ? '#fefefe' : '#1a1a1a'

          // Font size shrinks slightly for high-digit counts so "1234"
          // doesn't overflow the circle.
          const fontSize = count < 100 ? 11 : count < 1000 ? 10 : 9

          const el = document.createElement('div')
          el.className = 'cluster-bubble'
          el.dataset.role = 'cluster'
          // Dark outer ring on clusters too — same rationale as individual
          // pins. Light-shaded clusters (safe areas) would otherwise
          // vanish into the map background.
          el.style.cssText = `
            width: ${size}px; height: ${size}px;
            border-radius: 50%;
            background: rgb(${shade},${shade},${shade});
            border: 2px solid #fefefe;
            box-shadow:
              0 0 0 1.5px #2a2a2a,
              0 3px 10px rgba(0,0,0,0.22);
            display: flex; align-items: center; justify-content: center;
            color: ${textColor};
            font-family: 'JetBrains Mono', ui-monospace, monospace;
            font-size: ${fontSize}px;
            font-weight: 700;
            font-variant-numeric: tabular-nums;
            cursor: pointer;
            transition: transform 140ms ease, opacity 200ms ease;
            opacity: ${dimmedRef.current ? DIM_DEFAULT : 1};
          `
          el.textContent = String(count)
          el.addEventListener('mouseenter', () => { el.style.transform = 'scale(1.08)' })
          el.addEventListener('mouseleave', () => { el.style.transform = 'scale(1)' })

          return new markerLib.AdvancedMarkerElement({
            position, content: el,
            zIndex: 50 + count,
          })
        },
      },
    })

    return () => {
      if (clustererRef.current) {
        clustererRef.current.clearMarkers()
      }
      markersRef.current = []
    }
  }, [map, markerLib, listings, visible, onListingClick])

  // Dimming is a separate effect — updates just the opacity on
  // already-rendered elements. Avoids tearing down the clusterer
  // every time the user focuses/unfocuses a listing.
  useEffect(() => {
    if (!clustererRef.current) return

    // Apply to individual pins
    for (const m of markersRef.current) {
      if (m._pinEl) {
        m._pinEl.style.opacity = dimmed ? DIM_DEFAULT : '1'
      }
    }

    // Apply to currently-rendered clusters. MarkerClusterer doesn't
    // expose clusters directly, but their DOM elements carry our
    // data-role="cluster" attribute. Select them from the map div
    // and update opacity directly.
    const mapDiv = map?.getDiv?.()
    if (mapDiv) {
      const clusterEls = mapDiv.querySelectorAll('[data-role="cluster"]')
      clusterEls.forEach(el => {
        el.style.opacity = dimmed ? DIM_DEFAULT : '1'
      })
    }
  }, [dimmed, map])

  return null
}


function TransitPin({ stop, dimmed }) {
  return (
    <AdvancedMarker position={{ lat: stop.lat, lng: stop.lon }}>
      <div className="w-3.5 h-3.5 rounded-sm bg-vicinity-600 border border-vicinity-white
                      shadow flex items-center justify-center transition-opacity duration-200"
           style={{ opacity: dimmed ? DIM_TRANSIT : 1 }}>
        <span className="text-vicinity-white text-[6px] font-bold">T</span>
      </div>
    </AdvancedMarker>
  )
}


const AMENITY_ICONS = {
  grocery: ShoppingCart, supermarket: ShoppingCart, convenience: Store,
  cafe: Coffee, restaurant: Utensils, fast_food: Utensils, bar: Utensils, pub: Utensils,
  pharmacy: Pill, hospital: Pill, clinic: Pill,
  library: BookOpen, bookstore: BookOpen,
  fitness_centre: Dumbbell, gym: Dumbbell, sports_centre: Dumbbell,
  laundry: WashingMachine,
  park: Trees, garden: Trees, playground: Trees,
}

function AmenityPin({ amenity, dimmed }) {
  const [hover, setHover] = useState(false)
  const Icon = AMENITY_ICONS[amenity.subcategory] || MapPin
  return (
    <AdvancedMarker position={{ lat: amenity.lat, lng: amenity.lon }}>
      <div
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        className="relative cursor-pointer transition-opacity duration-200"
        style={{ opacity: dimmed ? DIM_AMENITY : 1 }}>
        <div className="w-6 h-6 rounded-full bg-vicinity-white border border-vicinity-300
                        shadow flex items-center justify-center
                        hover:scale-110 hover:border-vicinity-black
                        transition-all duration-150 ease-editorial">
          <Icon size={11} className="text-vicinity-700" />
        </div>
        <AnimatePresence>
          {hover && (
            <motion.div
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 2 }}
              transition={{ duration: 0.12 }}
              className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2
                         pointer-events-none z-50">
              <div className="panel-lifted px-3 py-2 min-w-[160px] max-w-[240px]">
                <p className="font-body text-[12px] font-semibold text-vicinity-black truncate">
                  {amenity.name || amenity.subcategory}
                </p>
                <p className="font-mono text-[10px] text-vicinity-500 mt-0.5">
                  {amenity.subcategory?.replace(/_/g, ' ')}
                  {amenity.distance_m != null && ` · ${amenity.distance_m}m`}
                </p>
                {amenity.opening_hours && (
                  <p className="font-body text-[10.5px] text-vicinity-500 mt-1 line-clamp-2">
                    {amenity.opening_hours}
                  </p>
                )}
                {amenity.brand && (
                  <p className="font-body text-[10.5px] text-vicinity-400 mt-0.5 truncate">
                    {amenity.brand}
                  </p>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </AdvancedMarker>
  )
}


function CrimeHeatmap({ points, visible, dimmed }) {
  const map = useMap()
  const viz = useMapsLibrary('visualization')
  const heatmapRef = useRef(null)

  useEffect(() => {
    if (!map || !viz) return
    if (heatmapRef.current) {
      heatmapRef.current.setMap(null)
      heatmapRef.current = null
    }
    if (!visible || !points?.length) return

    const data = points.map(p => ({
      location: new window.google.maps.LatLng(p.lat, p.lon),
      weight: p.weight || 1,
    }))

    const layer = new viz.HeatmapLayer({
      data, map,
      radius: 14,
      opacity: dimmed ? DIM_HEATMAP : 0.82,
      maxIntensity: 45, dissipating: true,
      gradient: [
        'rgba(0, 0, 0, 0)',
        'rgba(56, 120, 180, 0.40)',
        'rgba(103, 169, 207, 0.55)',
        'rgba(247, 209, 140, 0.72)',
        'rgba(241, 141, 91, 0.86)',
        'rgba(215, 48, 39, 0.95)',
        'rgba(138, 15, 30, 1.0)',
      ],
    })
    heatmapRef.current = layer

    return () => {
      if (heatmapRef.current) {
        heatmapRef.current.setMap(null)
        heatmapRef.current = null
      }
    }
  }, [map, viz, points, visible, dimmed])

  return null
}


function FocusRing() {
  const map = useMap()
  const listing = useStore(s => s.selectedListing)
  const focusedId = useStore(s => s.focusedListingId)
  const circleRef = useRef(null)

  useEffect(() => {
    if (!map || !window.google) return
    if (circleRef.current) { circleRef.current.setMap(null); circleRef.current = null }
    if (!listing || !listing.lat || !listing.lon || !focusedId) return

    circleRef.current = new window.google.maps.Circle({
      map,
      center: { lat: listing.lat, lng: listing.lon },
      radius: 500,
      strokeColor: FOCUS_BLACK,
      strokeOpacity: 0.35,
      strokeWeight: 1,
      fillColor: FOCUS_BLACK,
      fillOpacity: 0.04,
      clickable: false,
    })

    return () => {
      if (circleRef.current) { circleRef.current.setMap(null); circleRef.current = null }
    }
  }, [map, listing?.lat, listing?.lon, focusedId])

  return null
}


function RouteGraph() {
  const map = useMap()
  const routes = useStore(s => s.focusedRoutes)
  const listing = useStore(s => s.selectedListing)
  const refs = useRef([])

  useEffect(() => {
    refs.current.forEach(r => { if (r?.setMap) r.setMap(null) })
    refs.current = []
    if (!map || !routes.length || !window.google) return

    const bounds = new window.google.maps.LatLngBounds()
    if (listing?.lat && listing?.lon) bounds.extend({ lat: listing.lat, lng: listing.lon })

    for (const route of routes) {
      let wps = route.waypoints
      if (typeof wps === 'string') { try { wps = JSON.parse(wps) } catch { continue } }
      if (!Array.isArray(wps) || wps.length < 2) continue

      const path = wps.map(w => ({ lat: w.lat, lng: w.lon || w.lng }))
      path.forEach(p => bounds.extend(p))

      // Main polyline — vivid teal, thick stroke, high opacity. A
      // subtle white halo underneath via a second lower-z polyline
      // makes the route pop against any map background.
      refs.current.push(new window.google.maps.Polyline({
        path, geodesic: true,
        strokeColor: '#ffffff',
        strokeOpacity: 0.9,
        strokeWeight: 7,
        zIndex: 90,
        map,
      }))
      refs.current.push(new window.google.maps.Polyline({
        path, geodesic: true,
        strokeColor: ROUTE_TEAL,
        strokeOpacity: 0.95,
        strokeWeight: 4,
        zIndex: 91,
        map,
      }))

      // Waypoint hotspots — crime intensity along the corridor.
      // Colored by severity, sized larger than before so they're
      // visible at city-wide zoom. White border gives a clear
      // silhouette against both the map and the polyline.
      let scores = route.waypoint_scores
      if (typeof scores === 'string') { try { scores = JSON.parse(scores) } catch { scores = [] } }
      if (Array.isArray(scores)) {
        for (const hs of scores) {
          const intensity = Math.min((hs.crimes || 0) + (hs.violent || 0) * 2, 10)
          if (intensity < 1) continue   // skip low-noise waypoints, they just clutter the path

          const hotspotColor =
            intensity >= 7 ? ROUTE_HOTSPOT_HIGH :
            intensity >= 3 ? ROUTE_HOTSPOT_MID  :
                             ROUTE_HOTSPOT_LOW

          refs.current.push(new window.google.maps.Marker({
            position: { lat: hs.lat, lng: hs.lon || hs.lng }, map,
            icon: {
              path: window.google.maps.SymbolPath.CIRCLE,
              scale: 6 + intensity * 0.8,          // up from 2.5-6.5 → now 6-14
              fillColor: hotspotColor,
              fillOpacity: 0.95,
              strokeColor: '#ffffff',
              strokeWeight: 2,
            },
            title: `${hs.crimes || 0} crimes · ${hs.violent || 0} violent`,
            zIndex: 92,
          }))
        }
      }

      // Destination endpoint — upgraded to a clearly-labeled
      // "you-arrive-here" marker. Larger, teal fill matching the
      // polyline, white border, readable label outside the circle.
      if (route.dest_lat && route.dest_lon) {
        bounds.extend({ lat: route.dest_lat, lng: route.dest_lon })

        // Pin body
        refs.current.push(new window.google.maps.Marker({
          position: { lat: route.dest_lat, lng: route.dest_lon }, map,
          icon: {
            path: window.google.maps.SymbolPath.CIRCLE,
            scale: 14,
            fillColor: ROUTE_TEAL,
            fillOpacity: 1,
            strokeColor: '#ffffff',
            strokeWeight: 3,
          },
          title: `${route.dest_label || 'Destination'} · ${route.duration_min?.toFixed(0) || '?'}min`,
          zIndex: 95,
        }))

        // Label overlaid inside the pin — abbreviated to fit
        refs.current.push(new window.google.maps.Marker({
          position: { lat: route.dest_lat, lng: route.dest_lon }, map,
          label: {
            text: (route.dest_label || 'D').slice(0, 3).toUpperCase(),
            color: '#ffffff',
            fontSize: '10px',
            fontWeight: '700',
            fontFamily: "'JetBrains Mono', ui-monospace, monospace",
          },
          icon: {
            path: window.google.maps.SymbolPath.CIRCLE,
            scale: 0,        // invisible — we only need the label
            fillOpacity: 0,
            strokeOpacity: 0,
          },
          zIndex: 96,
        }))
      }
    }

    // NOTE: Deliberately no fitBounds here. Previously the route graph
    // auto-fit to its own bounds, which overrode the zoom level
    // focusListing had set (zoom 13 for clear listing visibility with
    // neighborhood context). fitBounds would zoom to whatever framed
    // the entire route — sometimes zoom 12 for long routes, sometimes
    // zoom 15+ for short ones. Unpredictable and disorienting.
    //
    // Now the listing stays centered at zoom 13. Route endpoints that
    // extend beyond the current viewport are reachable via manual pan.
    // User intent preserved: "show me this listing in context" doesn't
    // get hijacked by "show me this whole commute corridor."

    return () => {
      refs.current.forEach(r => { if (r?.setMap) r.setMap(null) })
      refs.current = []
    }
  }, [map, routes, listing])

  return null
}


function LayerControls() {
  const layers = useStore(s => s.layers)
  const toggle = useStore(s => s.toggleLayer)
  const [open, setOpen] = useState(false)

  const defs = [
    { k: 'listings',  l: 'Listings',  i: MapPin },
    { k: 'crimes',    l: 'Crime',     i: Shield },
    { k: 'transit',   l: 'Transit',   i: Train },
    { k: 'amenities', l: 'Amenities', i: Coffee },
  ]
  const activeCount = Object.values(layers).filter(Boolean).length

  return (
    <div className="absolute top-4 left-4 z-20">
      <button onClick={() => setOpen(!open)}
        className="h-10 px-3 bg-vicinity-white border border-vicinity-200 rounded-lg
                   shadow flex items-center gap-2
                   hover:border-vicinity-400 transition-colors">
        <Layers size={15} className="text-vicinity-600" />
        <span className="font-mono text-[11px] tabular-nums text-vicinity-700">
          {activeCount}
        </span>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.1 }}
            className="absolute top-12 left-0 panel-lifted p-1.5 min-w-[168px] space-y-0.5">
            {defs.map(({ k, l, i: I }) => (
              <button key={k} onClick={() => toggle(k)}
                className={`w-full flex items-center gap-3 px-3 py-2 rounded-md font-body text-[13px]
                            transition-colors ease-editorial duration-150
                            ${layers[k]
                              ? 'bg-vicinity-black text-vicinity-white'
                              : 'text-vicinity-600 hover:bg-vicinity-50'}`}>
                <I size={14} /> {l}
              </button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}


function FocusBanner() {
  const fid = useStore(s => s.focusedListingId)
  const listing = useStore(s => s.selectedListing)
  const routes = useStore(s => s.focusedRoutes)
  const clear = useStore(s => s.clearFocus)
  if (!fid) return null
  return (
    <motion.div
      initial={{ opacity: 0, x: 8 }}
      animate={{ opacity: 1, x: 0 }}
      className="absolute top-4 right-4 z-20 panel-lifted px-4 py-3 max-w-[300px]">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-body text-[13px] font-semibold text-vicinity-black truncate">
            {listing?.street || 'Focused'}
          </p>
          {(listing?.beds != null || listing?.baths != null || listing?.price) && (
            <p className="font-mono text-[11px] text-vicinity-500 mt-0.5">
              {listing?.price && `$${listing.price.toLocaleString()}/mo`}
              {listing?.beds != null && ` · ${listing.beds} bed`}
              {listing?.baths != null && ` · ${listing.baths} ba`}
            </p>
          )}
          {routes.length > 0 && (
            <div className="mt-2 space-y-1">
              {routes.map(r => (
                <div key={r.route_id} className="flex items-center gap-1.5">
                  <span className="inline-block w-2 h-2 rounded-full shrink-0"
                        style={{ background: ROUTE_TEAL }} />
                  <span className="font-mono text-[11px] text-vicinity-700 font-medium">
                    {r.dest_label}
                  </span>
                  <span className="font-mono text-[11px] text-vicinity-400">
                    · {r.duration_min?.toFixed(0)}min
                  </span>
                </div>
              ))}
            </div>
          )}
          {routes.length === 0 && (
            <p className="font-body text-[11px] text-vicinity-400 mt-1 italic">
              No routes configured
            </p>
          )}
        </div>
        <button onClick={clear}
          className="p-1 text-vicinity-400 hover:text-vicinity-black transition-colors shrink-0">
          <X size={13} />
        </button>
      </div>
    </motion.div>
  )
}


function MapLegend() {
  const bookmarks = useStore(s => s.bookmarks)
  const fid = useStore(s => s.focusedListingId)
  const routes = useStore(s => s.focusedRoutes)
  if (!bookmarks?.length && !fid) return null

  return (
    <div className="absolute bottom-4 left-4 z-20 panel-lifted px-3 py-2.5">
      <p className="lbl mb-1.5">Legend</p>
      <div className="space-y-1.5">
        {fid && (
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full border border-vicinity-white shadow-sm"
                  style={{ background: FOCUS_BLACK }} />
            <span className="font-body text-[11px] text-vicinity-700">Focused</span>
          </div>
        )}
        {bookmarks?.length > 0 && (
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full border shadow-sm"
                  style={{ background: BOOKMARK_GOLD, borderColor: BOOKMARK_GOLD_DEEP }} />
            <span className="font-body text-[11px] text-vicinity-700">
              Bookmarked ({bookmarks.length})
            </span>
          </div>
        )}

        {fid && routes?.length > 0 && (
          <>
            <div className="pt-1 mt-1 border-t border-vicinity-100">
              <div className="flex items-center gap-2">
                <span className="block w-4 h-[3px] rounded-full shadow-sm"
                      style={{ background: ROUTE_TEAL }} />
                <span className="font-body text-[11px] text-vicinity-700">
                  Commute route ({routes.length})
                </span>
              </div>
              <div className="flex items-center gap-2 mt-1">
                <span className="w-3 h-3 rounded-full border-[1.5px] border-vicinity-white shadow-sm"
                      style={{ background: ROUTE_TEAL }} />
                <span className="font-body text-[11px] text-vicinity-600">Destination</span>
              </div>
              <div className="flex items-center gap-1.5 mt-1">
                <span className="w-2.5 h-2.5 rounded-full border border-vicinity-white shadow-sm"
                      style={{ background: ROUTE_HOTSPOT_LOW }} />
                <span className="w-3 h-3 rounded-full border border-vicinity-white shadow-sm"
                      style={{ background: ROUTE_HOTSPOT_MID }} />
                <span className="w-3.5 h-3.5 rounded-full border border-vicinity-white shadow-sm"
                      style={{ background: ROUTE_HOTSPOT_HIGH }} />
                <span className="font-body text-[10px] text-vicinity-500 ml-1">
                  crime along route
                </span>
              </div>
            </div>
          </>
        )}

        <div className="pt-1 mt-1 border-t border-vicinity-100">
          <div className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full border border-vicinity-white shadow-sm"
                  style={{ background: 'rgb(210,210,210)' }} />
            <span className="w-3 h-3 rounded-full border border-vicinity-white shadow-sm"
                  style={{ background: 'rgb(120,120,120)' }} />
            <span className="w-3.5 h-3.5 rounded-full border border-vicinity-white shadow-sm"
                  style={{ background: 'rgb(50,50,50)' }} />
            <span className="font-body text-[10px] text-vicinity-500 ml-1">
              safer → less safe
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}


function MapContent({ onListingClick }) {
  const map = useMap()
  const listings      = useStore(s => s.listings)
  const transit       = useStore(s => s.transitStops)
  const crimePoints   = useStore(s => s.crimePoints)
  const amenities     = useStore(s => s.amenities)
  const bookmarks     = useStore(s => s.bookmarks)
  const layers        = useStore(s => s.layers)
  const fid           = useStore(s => s.focusedListingId)
  const center        = useStore(s => s.mapCenter)
  const fetchListings = useStore(s => s.fetchListings)
  const fetchTransit  = useStore(s => s.fetchTransit)
  const fetchCrimes   = useStore(s => s.fetchCrimes)
  const fetchAmenities = useStore(s => s.fetchAmenities)
  const fetchBookmarks = useStore(s => s.fetchBookmarks)

  const focusActive = !!fid

  const bookmarkedIds = useMemo(
    () => new Set((bookmarks || []).map(b => b.listing_id)),
    [bookmarks]
  )

  const { focusedListing, bookmarkedListings, defaultListings } = useMemo(() => {
    const bms = []
    const defs = []
    let foc = null
    for (const l of listings) {
      if (l.listing_id === fid) {
        foc = l
      } else if (bookmarkedIds.has(l.listing_id)) {
        bms.push(l)
      } else {
        defs.push(l)
      }
    }
    return {
      focusedListing: foc,
      bookmarkedListings: bms,
      defaultListings: defs,
    }
  }, [listings, fid, bookmarkedIds])

  useEffect(() => {
    fetchListings()
    fetchTransit()
    fetchBookmarks()
    if (layers.crimes) fetchCrimes()
  }, [])

  useEffect(() => { if (map && center) map.panTo(center) }, [map, center.lat, center.lng])
  useEffect(() => { if (layers.crimes && !crimePoints.length) fetchCrimes() }, [layers.crimes])
  useEffect(() => {
    if (layers.amenities) fetchAmenities(center.lat, center.lng)
  }, [layers.amenities, center.lat, center.lng])

  const amenitiesToShow = useMemo(() => (amenities || []).slice(0, 120), [amenities])

  return (
    <>
      <CrimeHeatmap points={crimePoints} visible={layers.crimes} dimmed={focusActive} />

      <ClusteredListings
        listings={defaultListings}
        visible={layers.listings}
        dimmed={focusActive}
        onListingClick={onListingClick} />

      {layers.listings && bookmarkedListings.map(l => (
        <BookmarkedPin key={l.listing_id} listing={l}
          dimmed={focusActive}
          onClick={onListingClick} />
      ))}

      {layers.listings && focusedListing && (
        <FocusedPin
          listing={focusedListing}
          isBookmarked={bookmarkedIds.has(focusedListing.listing_id)}
          onClick={onListingClick} />
      )}

      {layers.transit   && transit.map(s =>
        <TransitPin key={s.stop_id} stop={s} dimmed={focusActive} />
      )}
      {layers.amenities && amenitiesToShow.map((a, i) =>
        <AmenityPin key={a.osm_id || i} amenity={a} dimmed={focusActive} />
      )}

      <FocusRing />
      <RouteGraph />
    </>
  )
}


export default function MapView({ onListingClick }) {
  const center = useStore(s => s.mapCenter)
  const zoom = useStore(s => s.mapZoom)

  if (!MAPS_KEY) {
    return (
      <div className="h-full flex items-center justify-center bg-vicinity-50">
        <p className="font-body text-[15px] text-vicinity-300">
          Set VITE_GOOGLE_MAPS_KEY in .env
        </p>
      </div>
    )
  }

  return (
    <div className="relative h-full w-full">
      <APIProvider apiKey={MAPS_KEY} libraries={['visualization', 'marker']}>
        <GoogleMap defaultCenter={center} defaultZoom={zoom}
          mapId={MAP_ID}
          gestureHandling="greedy"
          disableDefaultUI
          styles={STYLES}
          className="h-full w-full">
          <MapContent onListingClick={onListingClick} />
        </GoogleMap>
      </APIProvider>
      <LayerControls />
      <FocusBanner />
      <MapLegend />
    </div>
  )
}