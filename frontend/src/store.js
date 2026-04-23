import { create } from 'zustand'
const API = import.meta.env.VITE_API_URL || ''

const authSlice = (set, get) => ({
  user: null, token: localStorage.getItem('vicinity_token'),
  login: async (email, password) => {
    const res = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) })
    const text = await res.text(); let data; try { data = JSON.parse(text) } catch { throw new Error(text || `HTTP ${res.status}`) }
    if (!res.ok) throw new Error(data.detail || 'Login failed')
    localStorage.setItem('vicinity_token', data.token)
    set({ token: data.token, user: { user_id: data.user_id, email: data.email, display_name: data.display_name } })
  },
  register: async (email, password, displayName) => {
    const res = await fetch(`${API}/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password, display_name: displayName }) })
    const text = await res.text(); let data; try { data = JSON.parse(text) } catch { throw new Error(text || `HTTP ${res.status}`) }
    if (!res.ok) throw new Error(data.detail || 'Registration failed'); return data
  },
  loadUser: async () => {
    const { token } = get(); if (!token) return
    try { const r = await fetch(`${API}/auth/me`, { headers: { Authorization: `Bearer ${token}` } }); if (r.ok) { const d = await r.json(); set({ user: { user_id: d.user_id, email: d.email, display_name: d.display_name } }) } else { localStorage.removeItem('vicinity_token'); set({ token: null, user: null }) } } catch {}
  },
  logout: () => { localStorage.removeItem('vicinity_token'); set({ token: null, user: null, bookmarks: [], userRoutes: [] }) },
})

const chatSlice = (set, get) => ({
  messages: [], toolEvents: [], isStreaming: false, interrupt: null, sessionId: null,
  sendMessage: async (text) => {
    const { token, messages, sessionId } = get()
    set({ messages: [...messages, { role: 'user', content: text, ts: Date.now() }], isStreaming: true, toolEvents: [], interrupt: null })
    const aMsg = { role: 'assistant', content: '', ts: Date.now() }; const evts = []
    try {
      const res = await fetch(`${API}/chat/send`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify({ message: text, session_id: sessionId || null }) })
      const sid = res.headers.get('X-Session-Id'); if (sid) set({ sessionId: sid })
      const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = ''
      while (true) {
        const { done, value } = await reader.read(); if (done) break
        buf += dec.decode(value, { stream: true }); const lines = buf.split('\n'); buf = lines.pop() || ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue; const raw = line.slice(6).trim(); if (!raw || raw === '[DONE]') continue
          try {
            const { type, data } = JSON.parse(raw)
            if (type === 'token') { aMsg.content += data.content; set({ messages: [...get().messages.filter(m => m !== aMsg), aMsg] }) }
            else if (type === 'tool_start') { evts.push({ type: 'start', tool: data.tool, args: data.args, ts: Date.now() }); set({ toolEvents: [...evts] }) }
            else if (type === 'tool_end') { evts.push({ type: 'end', tool: data.tool, error: data.error, size: data.size, ts: Date.now() }); set({ toolEvents: [...evts] }) }
            else if (type === 'node_start') { evts.push({ type: 'node_start', node: data.node, ts: Date.now() }); set({ toolEvents: [...evts] }) }
            else if (type === 'node_end' && data.content) { aMsg.content += data.content; set({ messages: [...get().messages.filter(m => m !== aMsg), aMsg] }) }
            else if (type === 'interrupt') set({ interrupt: data })
            else if (type === 'route') { evts.push({ type: 'route', route: data.route, ts: Date.now() }); set({ toolEvents: [...evts] }) }
            else if (type === 'done') { evts.push({ type: 'done', ...data, ts: Date.now() }); set({ toolEvents: [...evts] }) }
            else if (type === 'error') aMsg.content += `\n\nError: ${data.error}`
          } catch {}
        }
      }
      if (aMsg.content) set(s => ({ messages: s.messages.some(m => m === aMsg) ? s.messages : [...s.messages, aMsg] }))
    } catch (err) { set(s => ({ messages: [...s.messages, { role: 'assistant', content: `Connection error: ${err.message}`, ts: Date.now() }] })) }
    finally {
      set({ isStreaming: false })
      // Re-fetch bookmarks and routes after every chat (catches new bookmarks dynamically)
      const { token: t } = get()
      if (t) { get().fetchBookmarks(); get().fetchUserRoutes() }
    }
  },
  resumeInterrupt: async (response) => {
    const { token, sessionId } = get(); set({ interrupt: null, isStreaming: true })
    try {
      const res = await fetch(`${API}/chat/resume`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify({ session_id: sessionId, thread_id: sessionId, response }) })
      const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = ''; const msg = { role: 'assistant', content: '', ts: Date.now() }
      while (true) { const { done, value } = await reader.read(); if (done) break; buf += dec.decode(value, { stream: true }); const lines = buf.split('\n'); buf = lines.pop() || ''; for (const line of lines) { if (!line.startsWith('data: ')) continue; const raw = line.slice(6).trim(); if (!raw) continue; try { const e = JSON.parse(raw); if (e.type === 'token' || (e.type === 'node_end' && e.data.content)) { msg.content += (e.data.content || ''); set(s => ({ messages: [...s.messages.filter(m => m !== msg), msg] })) } } catch {} } }
      if (msg.content) set(s => ({ messages: s.messages.some(m => m === msg) ? s.messages : [...s.messages, msg] }))
    } catch (err) { set(s => ({ messages: [...s.messages, { role: 'assistant', content: `Error: ${err.message}`, ts: Date.now() }] })) }
    finally { set({ isStreaming: false }); const { token: t } = get(); if (t) { get().fetchBookmarks(); get().fetchUserRoutes() } }
  },
  clearChat: () => set({ messages: [], toolEvents: [], interrupt: null }),
})

const mapSlice = (set, get) => ({
  listings: [], transitStops: [], crimePoints: [], amenities: [],
  bookmarks: [], userRoutes: [],
  focusedListingId: null, focusedRoutes: [],
  selectedListing: null,
  mapCenter: { lat: 42.3601, lng: -71.0589 }, mapZoom: 13,
  layers: { listings: true, transit: false, crimes: false, amenities: false },

  toggleLayer: (l) => set(s => ({ layers: { ...s.layers, [l]: !s.layers[l] } })),
  setSelectedListing: (l) => set({ selectedListing: l }),
  clearSelectedListing: () => set({ selectedListing: null }),
  panTo: (lat, lng, zoom) => set({ mapCenter: { lat, lng }, ...(zoom ? { mapZoom: zoom } : {}) }),

  focusListing: async (listing) => {
    const { token } = get()
    set({ selectedListing: listing, focusedListingId: listing.listing_id, focusedRoutes: [] })
    if (listing.lat && listing.lon) set({ mapCenter: { lat: listing.lat, lng: listing.lon }, mapZoom: 14 })
    if (token) {
      try {
        const r = await fetch(`${API}/users/routes?listing_id=${listing.listing_id}`, { headers: { Authorization: `Bearer ${token}` } })
        const text = await r.text(); try { const d = JSON.parse(text); if (d.success && d.data) set({ focusedRoutes: d.data }) } catch {}
      } catch {}
    }
  },
  clearFocus: () => set({ focusedListingId: null, focusedRoutes: [], selectedListing: null }),

  fetchListings: async () => { try { const r = await fetch(`${API}/map/listings?limit=1000`); const d = await r.json(); if (d.success) set({ listings: d.data }) } catch {} },
  fetchTransit: async () => { try { const r = await fetch(`${API}/map/transit?limit=1000`); const d = await r.json(); if (d.success) set({ transitStops: d.data }) } catch {} },
  fetchCrimes: async (lat, lon) => { try { const r = await fetch(`${API}/safety/crimes?lat=${lat}&lon=${lon}&radius_m=2000&limit=1000`); const d = await r.json(); if (d.success) set({ crimePoints: d.data }) } catch {} },
  fetchAmenities: async (lat, lon) => { try { const r = await fetch(`${API}/map/amenities?lat=${lat}&lon=${lon}&limit=300`); const d = await r.json(); if (d.success) set({ amenities: d.data }) } catch {} },
  fetchBookmarks: async () => { const { token } = get(); if (!token) return; try { const r = await fetch(`${API}/users/bookmarks`, { headers: { Authorization: `Bearer ${token}` } }); const t = await r.text(); try { const d = JSON.parse(t); if (d.success) set({ bookmarks: d.data || [] }) } catch {} } catch {} },
  fetchUserRoutes: async () => { const { token } = get(); if (!token) return; try { const r = await fetch(`${API}/users/routes`, { headers: { Authorization: `Bearer ${token}` } }); const t = await r.text(); try { const d = JSON.parse(t); if (d.success) set({ userRoutes: d.data || [] }) } catch {} } catch {} },
})

const uiSlice = (set) => ({
  chatOpen: true, chatWidth: 420, toolLogOpen: false, detailDrawerOpen: false, sidebarCollapsed: false,
  setChatOpen: (o) => set({ chatOpen: o }), setChatWidth: (w) => set({ chatWidth: Math.max(320, Math.min(640, w)) }),
  setToolLogOpen: (o) => set({ toolLogOpen: o }), setDetailDrawerOpen: (o) => set({ detailDrawerOpen: o }),
  setSidebarCollapsed: (v) => set({ sidebarCollapsed: v }),
})

export const useStore = create((set, get) => ({ ...authSlice(set, get), ...chatSlice(set, get), ...mapSlice(set, get), ...uiSlice(set) }))