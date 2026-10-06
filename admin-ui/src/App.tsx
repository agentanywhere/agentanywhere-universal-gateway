import { useEffect, useState } from 'react'
import { BrowserRouter as Router, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Layout from './components/Layout'
import ErrorBoundary from './components/ErrorBoundary'
import Dashboard from './pages/Dashboard'
import Analytics from './pages/Analytics'
import AIInsights from './pages/AIInsights'
import Reports from './pages/Reports'
import APIKeys from './pages/APIKeys'
import Agents from './pages/Agents'
import Monitoring from './pages/Monitoring'
import RoutesPage from './pages/Routes'
import Protocols from './pages/Protocols'
import ProtocolTest from './pages/ProtocolTest'
import Logs from './pages/Logs'
import Settings from './pages/Settings'
import Login from './pages/Login'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import { ThemeProvider } from './contexts/ThemeContext'
import { ToastProvider } from './components/ui/Toast'
import { idaHandleCallback } from './ida'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

const BASENAME = (import.meta.env.BASE_URL || '/').replace(/\/$/, '') || undefined

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuth()
  const location = useLocation()

  if (!isAuthenticated && location.pathname !== '/login') {
    return <Navigate to="/login" replace />
  }

  if (isAuthenticated && location.pathname === '/login') {
    return <Navigate to="/dashboard" replace />
  }

  return <>{children}</>
}

function AppRoutes() {
  return (
    <AuthProvider>
      <Router basename={BASENAME}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/*"
            element={
              <ProtectedRoute>
                <Layout>
                  <Routes>
                    <Route path="/" element={<Navigate to="/dashboard" replace />} />
                    <Route path="/dashboard" element={<Dashboard />} />
                    <Route path="/agents" element={<Agents />} />
                    <Route path="/monitoring" element={<Monitoring />} />
                    <Route path="/analytics" element={<Analytics />} />
                    <Route path="/ai-insights" element={<AIInsights />} />
                    <Route path="/reports" element={<Reports />} />
                    <Route path="/api-keys" element={<APIKeys />} />
                    <Route path="/routes" element={<RoutesPage />} />
                    <Route path="/protocols" element={<Protocols />} />
                    <Route path="/test/:protocol" element={<ProtocolTest />} />
                    <Route path="/logs" element={<Logs />} />
                    <Route path="/settings" element={<Settings />} />
                  </Routes>
                </Layout>
              </ProtectedRoute>
            }
          />
        </Routes>
      </Router>
    </AuthProvider>
  )
}

function App() {
  const [booting, setBooting] = useState(true)

  useEffect(() => {
    idaHandleCallback().finally(() => setBooting(false))
  }, [])

  if (booting) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <div className="h-10 w-10 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    )
  }

  return (
    <ErrorBoundary>
      <ThemeProvider>
        <ToastProvider>
          <QueryClientProvider client={queryClient}>
            <AppRoutes />
          </QueryClientProvider>
        </ToastProvider>
      </ThemeProvider>
    </ErrorBoundary>
  )
}

export default App
