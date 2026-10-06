import React, { createContext, useContext, useState, useEffect, useCallback } from 'react'
import axios from 'axios'
import { idaLogout, isIdaSession, parseIdaProfile } from '../ida'

interface AuthContextType {
  token: string | null
  user: { name: string; email: string } | null
  login: (token: string) => void
  logout: () => void
  isAuthenticated: boolean
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

const BASE_PATH = (import.meta.env.BASE_URL || '/').replace(/\/?$/, '/')

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(() => {
    if (typeof window !== 'undefined') {
      return localStorage.getItem('admin_token')
    }
    return null
  })

  const [user, setUser] = useState<{ name: string; email: string } | null>(() =>
    parseIdaProfile(typeof window !== 'undefined' ? localStorage.getItem('admin_token') : null)
  )

  useEffect(() => {
    if (token) {
      axios.defaults.headers.common['Authorization'] = `Bearer ${token}`
      setUser(parseIdaProfile(token))
    } else {
      delete axios.defaults.headers.common['Authorization']
      setUser(null)
    }
  }, [token])

  const login = (newToken: string) => {
    setToken(newToken)
    if (typeof window !== 'undefined') {
      localStorage.setItem('admin_token', newToken)
    }
    axios.defaults.headers.common['Authorization'] = `Bearer ${newToken}`
    setUser(parseIdaProfile(newToken))
  }

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
    if (isIdaSession()) {
      idaLogout()
      return
    }
    if (typeof window !== 'undefined') {
      localStorage.removeItem('admin_token')
      localStorage.removeItem('auth_mode')
    }
    delete axios.defaults.headers.common['Authorization']
    window.location.href = `${BASE_PATH}login`.replace(/\/+/g, '/').replace(':/', '://')
  }, [])

  return (
    <AuthContext.Provider
      value={{
        token,
        user,
        login,
        logout,
        isAuthenticated: !!token,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
