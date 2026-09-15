/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Outfit"', 'system-ui', 'sans-serif'],
        sans: ['"Plus Jakarta Sans"', 'system-ui', 'sans-serif']
      },
      colors: {
        spot: {
          bg: '#0b0b0b',
          surface: '#121212',
          raised: '#181818',
          hover: '#282828',
          green: '#1ed760',
          greenDim: '#1aa34a',
          mute: '#a7a7a7',
          line: '#2a2a2a'
        }
      },
      boxShadow: {
        art: '0 24px 48px rgba(0,0,0,0.55)'
      },
      keyframes: {
        pulseDot: {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.55', transform: 'scale(0.85)' }
        },
        riseIn: {
          '0%': { opacity: '0', transform: 'translateY(10px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' }
        }
      },
      animation: {
        pulseDot: 'pulseDot 1.6s ease-in-out infinite',
        riseIn: 'riseIn 0.45s ease-out both'
      }
    }
  },
  plugins: []
}
