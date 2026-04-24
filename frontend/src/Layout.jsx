import { useCallback, useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import {
  Map as MapIcon, MessageSquare, LogOut, Bookmark, Route,
  ChevronLeft, ChevronRight, Shield, Navigation, ArrowLeft,
} from 'lucide-react'
import { useStore } from './store'
import MapView from './Map'
import Chat from './Chat'
import ListingDetail from './ListingDetail'

function Header() {
  const user = useStore(s => s.user)
  const logout = useStore(s => s.logout)

  return (
    <header className="h-16 bg-vicinity-white border-b border-vicinity-150 flex items-center
                       justify-between px-6 shrink-0 z-40">
      <div className="flex items-center gap-3">
        <h1 className="font-display text-[26px] font-bold text-vicinity-black
                       tracking-tight leading-none">Vicinity</h1>
        <div className="hidden sm:block w-px h-5 bg-vicinity-200 ml-1" />
        <span className="hidden sm:inline font-body text-[10.5px] text-vicinity-500
                         tracking-[0.16em] uppercase">
          Boston Housing Intelligence
        </span>
      </div>

      {user ? (
        <div className="flex items-center gap-3">
          <span className="font-body text-sm text-vicinity-700">
            {user.display_name || user.email}
          </span>
          <div className="w-8 h-8 rounded-full bg-vicinity-black text-vicinity-white
                          flex items-center justify-center font-body text-xs font-semibold">
            {(user.display_name || user.email || '?')[0].toUpperCase()}
          </div>
          <button onClick={logout}
            className="p-1.5 text-vicinity-400 hover:text-vicinity-black transition-colors">
            <LogOut size={15} />
          </button>
        </div>
      ) : (
        <span className="font-body text-sm text-vicinity-400">Guest</span>
      )}
    </header>
  )
}

function NavBtn({ icon: Icon, label, active, collapsed, onClick, dot, count }) {
  return (
    <button onClick={onClick}
      className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg font-body text-[13px]
                  transition-colors duration-150
                  ${active
                    ? 'bg-vicinity-100 text-vicinity-black font-medium'
                    : 'text-vicinity-500 hover:bg-vicinity-50 hover:text-vicinity-black'}`}>
      <Icon size={16} className="shrink-0" />
      {!collapsed && <span>{label}</span>}
      {!collapsed && dot && <span className="ml-auto w-1.5 h-1.5 rounded-full bg-vicinity-black" />}
      {!collapsed && count > 0 && !dot && (
        <span className="ml-auto font-mono text-[11px] text-vicinity-400">{count}</span>
      )}
    </button>
  )
}

function Sidebar() {
  const user = useStore(s => s.user)
  const chatOpen = useStore(s => s.chatOpen)
  const setChatOpen = useStore(s => s.setChatOpen)
  const collapsed = useStore(s => s.sidebarCollapsed)
  const setCollapsed = useStore(s => s.setSidebarCollapsed)
  const bookmarks = useStore(s => s.bookmarks)
  const userRoutes = useStore(s => s.userRoutes)
  const fetchBookmarks = useStore(s => s.fetchBookmarks)
  const fetchUserRoutes = useStore(s => s.fetchUserRoutes)
  const focusListing = useStore(s => s.focusListing)
  const focusedListingId = useStore(s => s.focusedListingId)
  const clearFocus = useStore(s => s.clearFocus)
  const [tab, setTab] = useState('map')

  useEffect(() => { if (user) { fetchBookmarks(); fetchUserRoutes() } }, [user?.user_id])

  const navClick = (t) => {
    if (t === 'chat') { setChatOpen(!chatOpen); return }
    if (collapsed) setCollapsed(false)
    setTab(tab === t ? 'map' : t)
  }

  return (
    <motion.aside
      animate={{ width: collapsed ? 64 : 280 }}
      transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
      className="h-full bg-vicinity-white border-r border-vicinity-150 flex flex-col shrink-0 overflow-hidden">
      <div className="px-2.5 pt-3 pb-2">
        <button onClick={() => setCollapsed(!collapsed)}
          className="w-full flex items-center justify-center px-3 py-2 rounded-lg
                     text-vicinity-400 hover:text-vicinity-black hover:bg-vicinity-50
                     transition-colors">
          {collapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
        </button>
      </div>

      <nav className="px-2.5 space-y-1">
        <NavBtn icon={MapIcon} label="Map" active={tab === 'map'} collapsed={collapsed}
          onClick={() => { navClick('map'); clearFocus() }} />
        <NavBtn icon={MessageSquare} label="Chat" collapsed={collapsed}
          onClick={() => navClick('chat')} dot={chatOpen} />
        {user && (
          <>
            <NavBtn icon={Bookmark} label="Bookmarks" active={tab === 'bookmarks'} collapsed={collapsed}
              onClick={() => navClick('bookmarks')} count={bookmarks.length} />
            <NavBtn icon={Route} label="Routes" active={tab === 'routes'} collapsed={collapsed}
              onClick={() => navClick('routes')} count={userRoutes.length} />
          </>
        )}
      </nav>

      {!collapsed && (
        <div className="flex-1 overflow-y-auto mt-3 border-t border-vicinity-100">
          {focusedListingId && (
            <button onClick={clearFocus}
              className="w-full flex items-center gap-2 px-4 py-2.5 font-body text-[13px]
                         text-vicinity-500 hover:text-vicinity-black hover:bg-vicinity-50
                         border-b border-vicinity-100 transition-colors">
              <ArrowLeft size={13} /> Back to all
            </button>
          )}

          {tab === 'bookmarks' && user && (
            <div className="p-3.5">
              <p className="lbl mb-3">Saved · {bookmarks.length}</p>
              {!bookmarks.length ? (
                <p className="font-body text-[13px] text-vicinity-400 leading-relaxed">
                  Ask the chat to bookmark any listing. Bookmarks track safety &amp;
                  livability scores daily through your watch period.
                </p>
              ) : (
                <div className="space-y-2">
                  {bookmarks.map(bm => {
                    const rts = userRoutes.filter(r => r.listing_id === bm.listing_id)
                    const active = bm.listing_id === focusedListingId
                    return (
                      <button key={bm.listing_id} onClick={() => focusListing(bm)}
                        className={`w-full text-left p-3 rounded-lg border transition-all duration-150
                                    ${active
                                      ? 'border-vicinity-black bg-vicinity-50'
                                      : 'border-vicinity-150 hover:border-vicinity-400'}`}>
                        <p className="font-body text-[13px] font-medium text-vicinity-black truncate">
                          {bm.street || 'Listing'}
                        </p>
                        {bm.neighborhood && (
                          <p className="font-body text-[11px] text-vicinity-400 truncate">
                            {bm.neighborhood}
                          </p>
                        )}
                        <div className="flex items-center gap-3 mt-1.5 flex-wrap">
                          {bm.price && (
                            <span className="font-mono text-[11px] text-vicinity-700 font-medium">
                              ${bm.price.toLocaleString()}
                            </span>
                          )}
                          {bm.beds != null && (
                            <span className="font-mono text-[11px] text-vicinity-500">
                              {bm.beds}bd
                            </span>
                          )}
                          {bm.baths != null && (
                            <span className="font-mono text-[11px] text-vicinity-500">
                              {bm.baths}ba
                            </span>
                          )}
                          {bm.safety_score != null && (
                            <span className="flex items-center gap-1 font-mono text-[11px] text-vicinity-500">
                              <Shield size={9} />{bm.safety_score}
                            </span>
                          )}
                        </div>
                        {rts.length > 0 && (
                          <div className="mt-2 pt-1.5 border-t border-vicinity-100 space-y-0.5">
                            {rts.map(r => (
                              <p key={r.route_id}
                                 className="font-body text-[11px] text-vicinity-500 flex items-center gap-1">
                                <Navigation size={9} />{r.dest_label} · {r.duration_min?.toFixed(0)}min
                              </p>
                            ))}
                          </div>
                        )}
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {tab === 'routes' && user && (
            <div className="p-3.5">
              <p className="lbl mb-2">Commute Routes · {userRoutes.length}</p>
              <p className="font-body text-[12px] text-vicinity-400 mb-3 leading-relaxed">
                Routes track safety along your daily commute. Select a bookmark to view.
              </p>
              {!userRoutes.length ? (
                <p className="font-body text-[13px] text-vicinity-400">No routes yet.</p>
              ) : (
                <div className="space-y-2">
                  {userRoutes.map(r => (
                    <div key={r.route_id} className="p-3 rounded-lg border border-vicinity-150">
                      <p className="font-body text-[13px] font-medium text-vicinity-black">
                        {r.dest_label}
                      </p>
                      <p className="font-body text-[11px] text-vicinity-400 truncate">
                        {r.dest_address}
                      </p>
                      <div className="flex gap-3 mt-1.5">
                        <span className="font-mono text-[11px] text-vicinity-700 font-medium">
                          {r.duration_min?.toFixed(0)}min
                        </span>
                        <span className="font-mono text-[11px] text-vicinity-400">
                          {r.travel_mode}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </motion.aside>
  )
}

function ResizeHandle({ onResize }) {
  const dragging = useRef(false)
  const start = useCallback((e) => {
    e.preventDefault()
    dragging.current = true
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    const mv = (ev) => { if (dragging.current) onResize(window.innerWidth - ev.clientX) }
    const up = () => {
      dragging.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      document.removeEventListener('mousemove', mv)
      document.removeEventListener('mouseup', up)
    }
    document.addEventListener('mousemove', mv)
    document.addEventListener('mouseup', up)
  }, [onResize])
  return <div className="resize-handle left-0" onMouseDown={start} />
}

export default function Layout() {
  const chatOpen = useStore(s => s.chatOpen)
  const chatWidth = useStore(s => s.chatWidth)
  const setChatWidth = useStore(s => s.setChatWidth)
  const setSelectedListing = useStore(s => s.setSelectedListing)
  const loadUser = useStore(s => s.loadUser)

  useEffect(() => { loadUser() }, [])

  // Stable onClick so MapView doesn't re-render on every Layout render
  const onListingClick = useCallback(
    l => setSelectedListing(l),
    [setSelectedListing]
  )

  return (
    <div className="h-full flex flex-col">
      <Header />
      <div className="flex-1 flex min-h-0">
        <Sidebar />
        <div className="flex-1 relative min-w-0">
          <MapView onListingClick={onListingClick} />
          <ListingDetail />
        </div>

        {/*
          Chat panel: plain div with CSS width transition instead of
          Framer Motion. The previous motion.div + AnimatePresence was
          keeping Framer's animation loop active during streaming —
          visible in the Performance profile as continuous purple
          Animations work + forced reflows from width recalculation.

          Width transition is GPU-composited and costs zero main-thread
          time outside of the open/close moment.
        */}
        <div
          className="relative shrink-0 h-full overflow-hidden
                     transition-[width] duration-[240ms] ease-[cubic-bezier(0.16,1,0.3,1)]"
          style={{ width: chatOpen ? chatWidth : 0 }}
          aria-hidden={!chatOpen}>
          {chatOpen && (
            <>
              <ResizeHandle onResize={setChatWidth} />
              <Chat />
            </>
          )}
        </div>
      </div>
    </div>
  )
}