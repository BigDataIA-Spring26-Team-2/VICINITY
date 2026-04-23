/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        vicinity: {
          black: '#0a0a0a',
          950: '#141414',
          900: '#1a1a1a',
          800: '#262626',
          700: '#333333',
          600: '#4a4a4a',
          500: '#6b6b6b',
          400: '#8a8a8a',
          300: '#b0b0b0',
          200: '#d4d4d4',
          150: '#e5e5e5',
          100: '#f0f0f0',
          50: '#f7f7f7',
          white: '#fefefe',
        },
      },
      fontFamily: {
        display: ['"Playfair Display"', 'Georgia', 'serif'],
        body: ['"DM Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'Consolas', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.65rem', { lineHeight: '1rem' }],
      },
      animation: {
        'fade-in': 'fadeIn 0.4s ease-out',
        'slide-up': 'slideUp 0.35s cubic-bezier(0.16, 1, 0.3, 1)',
        'slide-right': 'slideRight 0.35s cubic-bezier(0.16, 1, 0.3, 1)',
        'pulse-ring': 'pulseRing 2s cubic-bezier(0, 0, 0.2, 1) infinite',
        'typewriter': 'typewriter 0.05s steps(1) infinite',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        slideRight: {
          '0%': { opacity: '0', transform: 'translateX(-12px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
        pulseRing: {
          '0%': { transform: 'scale(0.8)', opacity: '0.8' },
          '80%, 100%': { transform: 'scale(2.2)', opacity: '0' },
        },
      },
      boxShadow: {
        'editorial': '0 1px 3px rgba(0,0,0,0.08), 0 8px 32px rgba(0,0,0,0.06)',
        'lifted': '0 2px 8px rgba(0,0,0,0.12), 0 16px 48px rgba(0,0,0,0.08)',
      },
    },
  },
  plugins: [],
}