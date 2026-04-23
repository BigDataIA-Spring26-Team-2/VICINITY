import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from './store'
import Auth from './Auth'
import Layout from './Layout'

export default function App() {
  const user = useStore(s => s.user)
  const token = useStore(s => s.token)
  const loadUser = useStore(s => s.loadUser)
  const [skippedAuth, setSkippedAuth] = useState(false)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    // Try to restore session from stored token
    loadUser().finally(() => setReady(true))
  }, [])

  // Show nothing until we've checked the token
  if (!ready) {
    return (
      <div className="h-full flex items-center justify-center bg-vicinity-white">
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="text-center"
        >
          <h1 className="font-display text-3xl text-vicinity-black tracking-tight">Vicinity</h1>
          <div className="mt-4 w-5 h-5 border-2 border-vicinity-300 border-t-vicinity-black
                          rounded-full animate-spin mx-auto" />
        </motion.div>
      </div>
    )
  }

  const authenticated = user && token
  const showApp = authenticated || skippedAuth

  return (
    <AnimatePresence mode="wait">
      {showApp ? (
        <motion.div
          key="app"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.3 }}
          className="h-full"
        >
          <Layout />
        </motion.div>
      ) : (
        <motion.div
          key="auth"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.3 }}
          className="h-full"
        >
          <Auth onSkip={() => setSkippedAuth(true)} />
        </motion.div>
      )}
    </AnimatePresence>
  )
}