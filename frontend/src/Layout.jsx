import { useRef, useCallback, useEffect, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Map as MapIcon, MessageSquare, LogOut, Bookmark, Route, ChevronLeft, ChevronRight, Shield, Navigation, ArrowLeft } from 'lucide-react'
import { useStore } from './store'
import MapView from './Map'
import Chat from './Chat'
import ListingDetail from './ListingDetail'

function Header() {
  const user = useStore(s => s.user); const logout = useStore(s => s.logout)
  return (
    <header className="h-12 bg-white border-b border-gray-200 flex items-center justify-between px-5 shrink-0 z-40">
      <div className="flex items-center gap-3">
        <h1 className="font-display text-lg font-black text-gray-900 tracking-tight">Vicinity</h1>
        <div className="hidden sm:block w-px h-5 bg-gray-200 ml-1" />
        <span className="hidden sm:inline font-body text-[11px] text-gray-400 tracking-widest uppercase">Boston Housing Intelligence</span>
      </div>
      {user ? (
        <div className="flex items-center gap-3">
          <span className="font-body text-sm text-gray-600">{user.display_name || user.email}</span>
          <div className="w-7 h-7 rounded-full bg-gray-900 text-white flex items-center justify-center font-body text-xs font-bold">{(user.display_name || user.email || '?')[0].toUpperCase()}</div>
          <button onClick={logout} className="p-1.5 text-gray-400 hover:text-gray-900 transition-colors"><LogOut size={14} /></button>
        </div>
      ) : <span className="font-body text-sm text-gray-400">Guest</span>}
    </header>
  )
}

function Sidebar() {
  const user = useStore(s => s.user)
  const chatOpen = useStore(s => s.chatOpen); const setChatOpen = useStore(s => s.setChatOpen)
  const collapsed = useStore(s => s.sidebarCollapsed); const setCollapsed = useStore(s => s.setSidebarCollapsed)
  const bookmarks = useStore(s => s.bookmarks); const userRoutes = useStore(s => s.userRoutes)
  const fetchBookmarks = useStore(s => s.fetchBookmarks); const fetchUserRoutes = useStore(s => s.fetchUserRoutes)
  const focusListing = useStore(s => s.focusListing); const focusedListingId = useStore(s => s.focusedListingId); const clearFocus = useStore(s => s.clearFocus)
  const [tab, setTab] = useState('map')

  useEffect(() => { if (user) { fetchBookmarks(); fetchUserRoutes() } }, [user?.user_id])

  const navClick = (t) => {
    if (t === 'chat') { setChatOpen(!chatOpen); return }
    // Auto-expand sidebar if collapsed
    if (collapsed) setCollapsed(false)
    setTab(tab === t ? 'map' : t)
  }

  return (
    <motion.div animate={{ width: collapsed ? 52 : 260 }} transition={{ duration: 0.2, ease: 'easeOut' }}
      className="h-full bg-white border-r border-gray-200 flex flex-col shrink-0 overflow-hidden">
      <div className="px-3 py-3 flex justify-end">
        <button onClick={() => setCollapsed(!collapsed)} className="p-1.5 text-gray-400 hover:text-gray-900 transition-colors">
          {collapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
        </button>
      </div>
      <nav className="px-2 space-y-0.5">
        <NavBtn icon={MapIcon} label="Map" active={tab === 'map'} collapsed={collapsed} onClick={() => { navClick('map'); clearFocus() }} />
        <NavBtn icon={MessageSquare} label="Chat" collapsed={collapsed} onClick={() => navClick('chat')} dot={chatOpen} />
        {user && <>
          <NavBtn icon={Bookmark} label="Bookmarks" active={tab === 'bookmarks'} collapsed={collapsed} onClick={() => navClick('bookmarks')} count={bookmarks.length} />
          <NavBtn icon={Route} label="Routes" active={tab === 'routes'} collapsed={collapsed} onClick={() => navClick('routes')} count={userRoutes.length} />
        </>}
      </nav>

      {!collapsed && (
        <div className="flex-1 overflow-y-auto mt-2 border-t border-gray-100">
          {focusedListingId && (
            <button onClick={clearFocus} className="w-full flex items-center gap-2 px-4 py-2 text-sm text-gray-500 hover:text-gray-900 hover:bg-gray-50 border-b border-gray-100 transition-colors">
              <ArrowLeft size={14} /> Back to all
            </button>
          )}

          {tab === 'bookmarks' && user && (
            <div className="p-3">
              <p className="font-body text-[11px] font-bold text-gray-400 uppercase tracking-widest mb-3">Saved ({bookmarks.length})</p>
              {!bookmarks.length ? <p className="font-body text-sm text-gray-400">Ask the chat to bookmark a listing.</p> :
                <div className="space-y-2">{bookmarks.map(bm => {
                  const rts = userRoutes.filter(r => r.listing_id === bm.listing_id)
                  const active = bm.listing_id === focusedListingId
                  return (
                    <button key={bm.listing_id} onClick={() => focusListing(bm)}
                      className={`w-full text-left p-3 rounded-lg border transition-all ${active ? 'border-gray-900 bg-gray-50 shadow-sm' : 'border-gray-200 hover:border-gray-400 hover:shadow-sm'}`}>
                      <p className="font-body text-sm font-bold text-gray-900 truncate">{bm.street || 'Listing'}</p>
                      <div className="flex items-center gap-3 mt-1">
                        {bm.price && <span className="font-mono text-xs text-gray-500">${bm.price.toLocaleString()}</span>}
                        {bm.safety_score != null && <span className="flex items-center gap-1 font-mono text-xs text-gray-400"><Shield size={9} />{bm.safety_score}</span>}
                      </div>
                      {rts.length > 0 && <div className="mt-2 pt-1.5 border-t border-gray-100 space-y-0.5">{rts.map(r => <p key={r.route_id} className="font-body text-xs text-gray-500 flex items-center gap-1"><Navigation size={9} />{r.dest_label} · {r.duration_min?.toFixed(0)}min</p>)}</div>}
                    </button>
                  )
                })}</div>
              }
            </div>
          )}

          {tab === 'routes' && user && (
            <div className="p-3">
              <p className="font-body text-[11px] font-bold text-gray-400 uppercase tracking-widest mb-2">Commute Routes ({userRoutes.length})</p>
              <p className="font-body text-xs text-gray-400 mb-3 leading-relaxed">Routes show commutes from bookmarked listings to your destinations. Select a bookmark to see them on the map.</p>
              {!userRoutes.length ? <p className="font-body text-sm text-gray-400">No routes yet.</p> :
                <div className="space-y-2">{userRoutes.map(r => (
                  <div key={r.route_id} className="p-3 rounded-lg border border-gray-200">
                    <p className="font-body text-sm font-bold text-gray-900">{r.dest_label}</p>
                    <p className="font-body text-xs text-gray-400 truncate">{r.dest_address}</p>
                    <div className="flex gap-3 mt-1"><span className="font-mono text-xs text-gray-600 font-medium">{r.duration_min?.toFixed(0)}min</span><span className="font-mono text-xs text-gray-400">{r.travel_mode}</span></div>
                  </div>
                ))}</div>
              }
            </div>
          )}
        </div>
      )}
    </motion.div>
  )
}

function NavBtn({ icon: Icon, label, active, collapsed, onClick, dot, count }) {
  return <button onClick={onClick} className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg font-body text-sm transition-colors ${active ? 'bg-gray-100 text-gray-900 font-semibold' : 'text-gray-500 hover:bg-gray-50 hover:text-gray-900'}`}>
    <Icon size={16} className="shrink-0" />{!collapsed && <span>{label}</span>}{!collapsed && dot && <span className="ml-auto w-1.5 h-1.5 rounded-full bg-gray-900" />}{!collapsed && count > 0 && !dot && <span className="ml-auto font-mono text-xs text-gray-400">{count}</span>}
  </button>
}

function ResizeHandle({ onResize }) {
  const d = useRef(false)
  const start = useCallback(e => { e.preventDefault(); d.current = true; document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none'; const mv = e => { if (d.current) onResize(window.innerWidth - e.clientX) }; const up = () => { d.current = false; document.body.style.cursor = ''; document.body.style.userSelect = ''; document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up) }; document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up) }, [onResize])
  return <div className="resize-handle left-0" onMouseDown={start} />
}

export default function Layout() {
  const chatOpen = useStore(s => s.chatOpen); const chatWidth = useStore(s => s.chatWidth); const setChatWidth = useStore(s => s.setChatWidth)
  const setSelectedListing = useStore(s => s.setSelectedListing); const loadUser = useStore(s => s.loadUser)
  useEffect(() => { loadUser() }, [])
  return (
    <div className="h-full flex flex-col">
      <Header />
      <div className="flex-1 flex min-h-0">
        <Sidebar />
        <div className="flex-1 relative min-w-0">
          <MapView onListingClick={useCallback(l => setSelectedListing(l), [])} />
          <ListingDetail />
        </div>
        <AnimatePresence>{chatOpen && (
          <motion.div initial={{ width: 0, opacity: 0 }} animate={{ width: chatWidth, opacity: 1 }} exit={{ width: 0, opacity: 0 }} transition={{ duration: 0.2, ease: 'easeOut' }} className="relative shrink-0 h-full">
            <ResizeHandle onResize={setChatWidth} /><Chat />
          </motion.div>
        )}</AnimatePresence>
      </div>
    </div>
  )
}