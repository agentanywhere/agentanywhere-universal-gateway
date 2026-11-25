/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        // Sovereign type: Calibri body, Cambria display, Consolas code. Carlito and
        // Caladea are their metric-compatible open fallbacks (loaded in index.html).
        sans: ['Calibri', 'Carlito', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        display: ['Cambria', 'Caladea', 'Georgia', '"Times New Roman"', 'serif'],
        mono: ['Consolas', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      animation: {
        'fade-in': 'fadeIn 0.45s cubic-bezier(0.22, 1, 0.36, 1)',
        'slide-up': 'slideUp 0.45s cubic-bezier(0.22, 1, 0.36, 1)',
        'scale-in': 'scaleIn 0.28s cubic-bezier(0.22, 1, 0.36, 1)',
        'shimmer': 'shimmer 2s linear infinite',
        'rise': 'rise 0.6s cubic-bezier(0.22, 1, 0.36, 1)',
        'spin-slow': 'spin 60s linear infinite',
        'spin-slower': 'spin 110s linear infinite reverse',
        'core-pulse': 'corePulse 4s ease-in-out infinite',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0', transform: 'translateY(6px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        scaleIn: {
          '0%': { opacity: '0', transform: 'scale(0.97)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-1000px 0' },
          '100%': { backgroundPosition: '1000px 0' },
        },
        corePulse: {
          '0%, 100%': { opacity: '0.55', transform: 'scale(1)' },
          '50%': { opacity: '1', transform: 'scale(1.06)' },
        },
        rise: {
          '0%': { opacity: '0', transform: 'translateY(16px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
      colors: {
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        popover: {
          DEFAULT: 'hsl(var(--popover))',
          foreground: 'hsl(var(--popover-foreground))',
        },
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        secondary: {
          DEFAULT: 'hsl(var(--secondary))',
          foreground: 'hsl(var(--secondary-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        sidebar: 'hsl(var(--sidebar))',
        brand: {
          cyan: '#2CC7FE',
          blue: '#3B82F6',
          deep: '#2563EB',
          violet: '#6729F4',
          saffron: '#FF9933',
          live: '#10B981',
        },
        sovereign: {
          bg: '#0A0E14',
          card: '#14202E',
          border: '#26344A',
          ink: '#F4F1EA',
          body: '#B8C0CA',
          muted: '#8B95A1',
        },
      },
      boxShadow: {
        soft: '0 1px 2px hsl(216 33% 6% / 0.05), 0 8px 24px hsl(216 33% 6% / 0.05)',
        panel: '0 1px 0 hsl(0 0% 100% / 0.04) inset, 0 12px 40px hsl(216 33% 4% / 0.12)',
        glow: '0 0 0 1px hsl(196 99% 58% / 0.25), 0 12px 48px hsl(217 91% 60% / 0.25)',
        dark: '0 8px 24px rgba(0, 0, 0, 0.28)',
        'dark-lg': '0 16px 40px rgba(0, 0, 0, 0.4)',
      },
      backgroundImage: {
        'gradient-radial': 'radial-gradient(var(--tw-gradient-stops))',
        'gradient-conic': 'conic-gradient(from 180deg at 50% 50%, var(--tw-gradient-stops))',
        shimmer: 'linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent)',
      },
      letterSpacing: {
        brand: '0.02em',
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
}
