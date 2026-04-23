import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useStore } from './store'

export default function Auth({ onSkip }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [success, setSuccess] = useState('')

  const login = useStore(s => s.login)
  const register = useStore(s => s.register)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setSuccess('')
    setLoading(true)

    try {
      if (mode === 'login') {
        await login(email, password)
      } else {
        await register(email, password, displayName)
        setSuccess('Account created. Signing you in...')
        await new Promise(r => setTimeout(r, 800))
        await login(email, password)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="h-full flex items-center justify-center bg-vicinity-white">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="w-full max-w-sm px-6"
      >
        {/* Logo / Title */}
        <div className="mb-12 text-center">
          <h1 className="font-display text-5xl tracking-tight text-vicinity-black">
            Vicinity
          </h1>
          <p className="mt-3 font-body text-sm text-vicinity-500 tracking-wide">
            Boston housing intelligence
          </p>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="space-y-4">
          <AnimatePresence mode="wait">
            {mode === 'register' && (
              <motion.div
                key="name"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.25 }}
              >
                <input
                  type="text"
                  placeholder="Display name"
                  value={displayName}
                  onChange={e => setDisplayName(e.target.value)}
                  className="w-full px-0 py-3 bg-transparent border-b border-vicinity-200
                             font-body text-sm text-vicinity-black placeholder:text-vicinity-400
                             focus:outline-none focus:border-vicinity-black transition-colors duration-300"
                />
              </motion.div>
            )}
          </AnimatePresence>

          <input
            type="email"
            placeholder="Email"
            value={email}
            onChange={e => setEmail(e.target.value)}
            required
            className="w-full px-0 py-3 bg-transparent border-b border-vicinity-200
                       font-body text-sm text-vicinity-black placeholder:text-vicinity-400
                       focus:outline-none focus:border-vicinity-black transition-colors duration-300"
          />

          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            required
            minLength={8}
            className="w-full px-0 py-3 bg-transparent border-b border-vicinity-200
                       font-body text-sm text-vicinity-black placeholder:text-vicinity-400
                       focus:outline-none focus:border-vicinity-black transition-colors duration-300"
          />

          {/* Error / Success */}
          <AnimatePresence>
            {error && (
              <motion.p
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="font-body text-xs text-vicinity-600"
              >
                {error}
              </motion.p>
            )}
            {success && (
              <motion.p
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="font-body text-xs text-vicinity-500"
              >
                {success}
              </motion.p>
            )}
          </AnimatePresence>

          {/* Submit */}
          <button
            type="submit"
            disabled={loading}
            className="w-full py-3 mt-4 bg-vicinity-black text-vicinity-white font-body text-sm
                       tracking-wide hover:bg-vicinity-800 active:bg-vicinity-900
                       disabled:opacity-40 disabled:cursor-not-allowed
                       transition-all duration-200"
          >
            {loading ? '...' : mode === 'login' ? 'Sign in' : 'Create account'}
          </button>
        </form>

        {/* Toggle mode */}
        <div className="mt-8 text-center space-y-3">
          <button
            onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError('') }}
            className="font-body text-xs text-vicinity-500 hover:text-vicinity-black
                       transition-colors duration-200 tracking-wide"
          >
            {mode === 'login' ? 'Create an account' : 'Already have an account'}
          </button>

          <div>
            <button
              onClick={onSkip}
              className="font-body text-xs text-vicinity-400 hover:text-vicinity-600
                         transition-colors duration-200"
            >
              Continue without signing in
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  )
}