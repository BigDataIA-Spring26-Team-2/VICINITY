import { create } from 'zustand'
import * as api from './api'

const API = import.meta.env.VITE_API_URL || ''

const authSlice = (set, get) => ({
  user: null,
  token: localStorage.getItem('vicinity_token'),

  login: async (email, password) => {
    const res = await fetch(`${API}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    })
    const text = await res.text()
    let data; try { data = JSON.parse(text) } catch { throw new Error(text || `HTTP ${res.status}`) }
    if (!res.ok) throw new Error(data.detail || 'Login failed')
    localStorage.setItem('vicinity_token', data.token)
    set({ token: data.token, user: { user_id: data.user_id, email: data.email, display_name: data.display_name } })
  },

  register: async (email, password, displayName) => {
    const res = await fetch(`${API}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, display_name: displayName }),
    })
    const text = await res.text()
    let data; try { data = JSON.parse(text) } catch { throw new Error(text || `HTTP ${res.status}`) }
    if (!res.ok) throw new Error(data.detail || 'Registration failed')
    return data
  },

  loadUser: async () => {
    const { token } = get(); if (!token) return
    try {
      const r = await fetch(`${API}/auth/me`, { headers: { Authorization: `Bearer ${token}` } })
      if (r.ok) {
        const d = await r.json()
        set({ user: { user_id: d.user_id, email: d.email, display_name: d.display_name } })
      } else {
        localStorage.removeItem('vicinity_token')
        set({ token: null, user: null })
      }
    } catch (err) {
      console.warn('loadUser failed:', err.message)
    }
  },

  logout: async () => {
    const sid = get().sessionId
    const token = get().token
    if (sid && token) {
      try {
        await fetch(`${API}/chat/session/${sid}`, {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${token}` },
          keepalive: true,
        })
      } catch { /* best effort */ }
    }
    localStorage.removeItem('vicinity_token')
    localStorage.removeItem('vicinity_session_id')
    set({ token: null, user: null, bookmarks: [], userRoutes: [],
          sessionId: null, messages: [], toolEvents: [] })
  },
})

const refreshAfterWrite = (get) => {
  const { token } = get()
  if (!token) return
  try { get().fetchBookmarks() }  catch {}
  try { get().fetchUserRoutes() } catch {}
}

async function streamSSE(response, set, get, evts) {
  const aMsg = { role: 'assistant', content: '', ts: Date.now() }
  let aMsgInList = false
  let committedLen = 0
  let liveLen = 0
  let rafHandle = null
  let streamDone = false

  const flushIfNeeded = () => {
    if (liveLen > committedLen) {
      committedLen = liveLen
      set(s => {
        const msgs = s.messages
        if (aMsgInList && msgs[msgs.length - 1]?.ts === aMsg.ts) {
          const next = msgs.slice(0, -1)
          next.push({ ...aMsg })
          return { messages: next }
        }
        return s
      })
    }
  }

  const rafTick = () => {
    flushIfNeeded()
    if (!streamDone) {
      rafHandle = requestAnimationFrame(rafTick)
    } else {
      rafHandle = null
    }
  }

  const startRafLoop = () => {
    if (rafHandle == null && !streamDone) {
      rafHandle = requestAnimationFrame(rafTick)
    }
  }

  const stopRafLoop = () => {
    streamDone = true
    if (rafHandle != null) {
      cancelAnimationFrame(rafHandle)
      rafHandle = null
    }
    flushIfNeeded()
  }

  try {
    const reader = response.body.getReader()
    const dec = new TextDecoder()
    let buf = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buf += dec.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6).trim()
        if (!raw || raw === '[DONE]') continue

        let frame
        try { frame = JSON.parse(raw) }
        catch { continue }

        const { type, data } = frame

        if (type === 'token' || (type === 'node_end' && data?.content)) {
          aMsg.content += (data.content || '')
          liveLen = aMsg.content.length

          if (!aMsgInList) {
            aMsgInList = true
            set(s => ({ messages: [...s.messages, { ...aMsg }] }))
            committedLen = liveLen
            startRafLoop()
          }
        } else if (type === 'tool_start') {
          evts.push({ type: 'start', tool: data.tool, args: data.args, ts: Date.now() })
          set({ toolEvents: [...evts] })
        } else if (type === 'tool_end') {
          evts.push({ type: 'end', tool: data.tool, error: data.error, size: data.size, ts: Date.now() })
          set({ toolEvents: [...evts] })
        } else if (type === 'node_start') {
          evts.push({ type: 'node_start', node: data.node, ts: Date.now() })
          set({ toolEvents: [...evts] })
        } else if (type === 'interrupt') {
          set({ interrupt: data })
        } else if (type === 'route') {
          evts.push({ type: 'route', route: data.route, ts: Date.now() })
          set({ toolEvents: [...evts] })
        } else if (type === 'done') {
          evts.push({ type: 'done', ...data, ts: Date.now() })
          set({ toolEvents: [...evts] })
        } else if (type === 'log') {
          evts.push({ type: 'log', ...data, ts: Date.now() })
          set({ toolEvents: [...evts] })
        } else if (type === 'error') {
          aMsg.content += `\n\nError: ${data.error}`
          liveLen = aMsg.content.length
          if (!aMsgInList) {
            aMsgInList = true
            set(s => ({ messages: [...s.messages, { ...aMsg }] }))
            committedLen = liveLen
          }
        }
      }
    }
  } finally {
    stopRafLoop()
  }

  return { aMsg, aMsgInList }
}


const chatSlice = (set, get) => ({
  messages: [], toolEvents: [], isStreaming: false, interrupt: null,
  sessionId: localStorage.getItem('vicinity_session_id') || null,

  _setSession: (sid) => {
    if (sid) {
      localStorage.setItem('vicinity_session_id', sid)
      set({ sessionId: sid })
    }
  },

  sendMessage: async (text) => {
    const { token, messages, sessionId } = get()
    set({
      messages: [...messages, { role: 'user', content: text, ts: Date.now() }],
      isStreaming: true, toolEvents: [], interrupt: null,
    })
    const evts = []

    try {
      const res = await fetch(`${API}/chat/send`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ message: text, session_id: sessionId || null }),
      })
      const sid = res.headers.get('X-Session-Id')
      if (sid) get()._setSession(sid)

      await streamSSE(res, set, get, evts)
    } catch (err) {
      set(s => ({
        messages: [...s.messages, {
          role: 'assistant',
          content: `Connection error: ${err.message}`,
          ts: Date.now(),
        }],
      }))
    } finally {
      set({ isStreaming: false })
      refreshAfterWrite(get)
    }
  },

  composeListingSeed: (listing, routes = []) => {
    const parts = []
    const id = listing?.listing_id || '?'
    const street = listing?.street || 'this listing'
    const nb = listing?.neighborhood || listing?.city || 'the area'
    parts.push(
      `Tell me about listing ${id} (${street}, ${nb}). ` +
      `What stands out about this place — safety, commute feasibility, neighborhood vibe?`
    )
    if (Array.isArray(routes) && routes.length > 0) {
      const dests = routes.map(r => r.dest_label).filter(Boolean).slice(0, 3)
      if (dests.length > 0) {
        parts.push(
          `My commute routes to ${dests.join(', ')} are already set up — ` +
          `factor those into the commute assessment.`
        )
      }
    }
    return parts.join(' ')
  },

  askVicinityAboutListing: async (messageText) => {
    set({ chatOpen: true })
    await get().sendMessage(messageText)
  },

  resumeInterrupt: async (response) => {
    const { token, sessionId } = get()
    set({ interrupt: null, isStreaming: true })
    const evts = []

    try {
      const res = await fetch(`${API}/chat/resume`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          session_id: sessionId,
          thread_id: sessionId,
          response,
        }),
      })
      await streamSSE(res, set, get, evts)
    } catch (err) {
      set(s => ({
        messages: [...s.messages, {
          role: 'assistant',
          content: `Error: ${err.message}`,
          ts: Date.now(),
        }],
      }))
    } finally {
      set({ isStreaming: false })
      refreshAfterWrite(get)
    }
  },

  clearChat: () => {
    localStorage.removeItem('vicinity_session_id')
    set({ messages: [], toolEvents: [], interrupt: null, sessionId: null })
  },
})

const mapSlice = (set, get) => ({
  listings: [], transitStops: [], crimePoints: [], amenities: [],
  bookmarks: [], userRoutes: [],
  focusedListingId: null, focusedRoutes: [], selectedListing: null,
  detail: null, scorecard: null, narratives: null,
  distribution: null, crimeTypes: null, detailLoading: false,
  mapCenter: { lat: 42.3601, lng: -71.0589 }, mapZoom: 13,
  layers: { listings: true, transit: false, crimes: true, amenities: false },
  errors: [],

  addError: (msg) => set(s => ({ errors: [...s.errors, { msg, ts: Date.now() }].slice(-5) })),

  toggleLayer: (l) => set(s => ({ layers: { ...s.layers, [l]: !s.layers[l] } })),
  setSelectedListing: (l) => set({ selectedListing: l, detailPanelOpen: !!l }),
  clearSelectedListing: () => set({
    selectedListing: null, detailPanelOpen: false,
    detail: null, scorecard: null, narratives: null,
    distribution: null, crimeTypes: null,
  }),
  panTo: (lat, lng, zoom) => set({ mapCenter: { lat, lng }, ...(zoom ? { mapZoom: zoom } : {}) }),

  focusListing: async (listing) => {
    const { token } = get()
    set({
      selectedListing: listing,
      focusedListingId: listing.listing_id,
      focusedRoutes: [],
      detailPanelOpen: true,
    })
    if (listing.lat && listing.lon) {
      // Zoom 13 = multi-neighborhood context. Users can see individual
      // listing pins clearly but also see where the focused listing
      // sits relative to surrounding neighborhoods. Zoom 14+ is too
      // aggressive — it frames the listing in isolation, losing the
      // "where am I" spatial context that makes the map useful.
      // Zoom 13 matches what Zillow / Redfin default to on focus.
      set({ mapCenter: { lat: listing.lat, lng: listing.lon }, mapZoom: 13 })
    }
    if (token) {
      try {
        const d = await api.getUserRoutes({ listing_id: listing.listing_id })
        if (d.success) set({ focusedRoutes: d.data || [] })
      } catch (e) { get().addError(`Routes: ${e.message}`) }
    }
  },
  clearFocus: () => set({
    focusedListingId: null, focusedRoutes: [], selectedListing: null, detailPanelOpen: false,
    detail: null, scorecard: null, narratives: null,
    distribution: null, crimeTypes: null,
  }),

  fetchListings: async () => {
    try {
      const d = await api.getMapListings({ limit: 5000 })
      if (d.success) set({ listings: d.data || [] })
    } catch (e) { get().addError(`Listings: ${e.message}`) }
  },

  fetchTransit: async () => {
    try {
      const d = await api.getMapTransit({ limit: 1000 })
      if (d.success) set({ transitStops: d.data || [] })
    } catch (e) { get().addError(`Transit: ${e.message}`) }
  },

  fetchCrimes: async () => {
    try {
      const d = await api.getCrimesHeatmap({ max_points: 50000 })
      if (d.success) set({ crimePoints: d.data || [] })
    } catch (e) { get().addError(`Crime heatmap: ${e.message}`) }
  },

  fetchAmenities: async (lat, lon) => {
    try {
      const d = await api.getMapAmenities(lat, lon, { radius_m: 1200, limit: 150 })
      if (d.success) set({ amenities: d.data || [] })
    } catch (e) { get().addError(`Amenities: ${e.message}`) }
  },

  fetchBookmarks: async () => {
    const { token } = get(); if (!token) return
    try {
      const d = await api.getUserBookmarks()
      if (d.success) set({ bookmarks: d.data || [] })
    } catch (e) { get().addError(`Bookmarks: ${e.message}`) }
  },

  fetchUserRoutes: async () => {
    const { token } = get(); if (!token) return
    try {
      const d = await api.getUserRoutes()
      if (d.success) set({ userRoutes: d.data || [] })
    } catch (e) { get().addError(`Routes: ${e.message}`) }
  },

  fetchDetail: async (listingId) => {
    set({
      detailLoading: true,
      detail: null, scorecard: null, narratives: null,
      distribution: null, crimeTypes: null,
    })
    const { token, selectedListing } = get()
    const lat = selectedListing?.lat
    const lon = selectedListing?.lon
    const [detRes, scRes, narrRes, distRes, typesRes] = await Promise.all([
      api.getListingDetail(listingId).catch(e => { get().addError(`Detail: ${e.message}`); return null }),
      token ? api.getScorecard(listingId, { days: 90 }).catch(() => null) : Promise.resolve(null),
      api.getListingNarratives(listingId, { limit: 12 }).catch(() => null),
      (lat && lon) ? api.getCrimesDistribution(lat, lon).catch(() => null) : Promise.resolve(null),
      (lat && lon) ? api.getCrimesTypes(lat, lon, { top: 10 }).catch(() => null) : Promise.resolve(null),
    ])
    set({
      detail: detRes?.data?.[0] || null,
      scorecard: scRes?.data || null,
      narratives: narrRes?.data || null,
      distribution: distRes?.data || null,
      crimeTypes: typesRes?.data || null,
      detailLoading: false,
    })
  },
})

const uiSlice = (set) => ({
  chatOpen: true,
  chatWidth: 440,
  toolLogOpen: false,
  detailPanelOpen: false,
  sidebarCollapsed: false,
  detailTab: 'overview',
  setChatOpen: (o) => set({ chatOpen: o }),
  setChatWidth: (w) => set({ chatWidth: Math.max(340, Math.min(680, w)) }),
  setToolLogOpen: (o) => set({ toolLogOpen: o }),
  setDetailPanelOpen: (o) => set({ detailPanelOpen: o }),
  setSidebarCollapsed: (v) => set({ sidebarCollapsed: v }),
  setDetailTab: (t) => set({ detailTab: t }),
})

export const useStore = create((set, get) => ({
  ...authSlice(set, get),
  ...chatSlice(set, get),
  ...mapSlice(set, get),
  ...uiSlice(set, get),
}))