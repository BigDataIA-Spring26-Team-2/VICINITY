import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from './store'
import Auth from './Auth'
import Layout from './Layout'

function ErrorToasts() {
  const errors = useStore(s => s.errors)
  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[100] flex flex-col gap-1.5 pointer-events-none">
      <AnimatePresence>
        {errors.slice(-3).map((e) => (
          <motion.div
            key={e.ts}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
            className="px-3 py-1.5 bg-vicinity-900 text-vicinity-100 rounded-md
                       font-body text-xs shadow-lg pointer-events-auto">
            {e.msg}
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  )
}

export default function App() {
  const user = useStore(s => s.user)
  const token = useStore(s => s.token)
  const loadUser = useStore(s => s.loadUser)
  const [skippedAuth, setSkippedAuth] = useState(false)
  const [ready, setReady] = useState(false)

  useEffect(() => { loadUser().finally(() => setReady(true)) }, [])

  if (!ready) {
    return (
      <div className="h-full flex items-center justify-center bg-vicinity-white">
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-center">
          <h1 className="font-display text-4xl text-vicinity-black tracking-tight">Vicinity</h1>
          <div className="mt-5 w-4 h-4 border-2 border-vicinity-200 border-t-vicinity-black
                          rounded-full animate-spin mx-auto" />
        </motion.div>
      </div>
    )
  }

  const showApp = (user && token) || skippedAuth

  return (
    <>
      <AnimatePresence mode="wait">
        {showApp ? (
          <motion.div key="app" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }} transition={{ duration: 0.3 }} className="h-full">
            <Layout />
          </motion.div>
        ) : (
          <motion.div key="auth" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }} transition={{ duration: 0.3 }} className="h-full">
            <Auth onSkip={() => setSkippedAuth(true)} />
          </motion.div>
        )}
      </AnimatePresence>
      <ErrorToasts />
    </>
  )
}