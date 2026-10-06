import axios from 'axios'

const BASE_PATH = (import.meta.env.BASE_URL || '/').replace(/\/?$/, '/')
// When the SPA is served under /console/, BASE_URL is /console/
// and API calls should be /console/admin/... so the host nginx can strip
// the prefix once and forward /admin/... to the gateway. Do NOT also rewrite
// /admin URLs in the request interceptor — that double-prefixes the path and
// the admin-ui nginx then serves index.html (HTML) instead of JSON.
const API_BASE_URL = import.meta.env?.VITE_API_URL || BASE_PATH.replace(/\/$/, '') || '/'

export const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
})

apiClient.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('admin_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    // 401 = bad/expired token. 403 on admin APIs usually means the SSO
    // token is valid but missing required roles (e.g. after a role grant) —
    // force re-login so the browser picks up a fresh access token.
    if (status === 401 || status === 403) {
      localStorage.removeItem('admin_token')
      localStorage.removeItem('auth_mode')
      localStorage.removeItem('ida_refresh_token')
      localStorage.removeItem('ida_id_token')
      localStorage.removeItem('admin_user')
      const loginUrl = `${BASE_PATH}login`.replace(/\/+/g, '/').replace(':/', '://')
      if (!window.location.pathname.endsWith('/login')) {
        window.location.href = loginUrl
      }
    }
    return Promise.reject(error)
  }
)

export default apiClient
