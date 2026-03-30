import { useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuthStore } from './store/authStore'

import Layout from './components/layout/Layout'
import LoginPage from './pages/LoginPage'
import RegisterPage from './pages/RegisterPage'
import DashboardPage from './pages/DashboardPage'
import AccountsPage from './pages/AccountsPage'
import AccountDetailPage from './pages/AccountDetailPage'
import ProxiesPage from './pages/ProxiesPage'
import InboxPage from './pages/InboxPage'
import TasksPage from './pages/TasksPage'
import AnalyticsPage from './pages/AnalyticsPage'
import CommentingPage from './pages/CommentingPage'
import WarmupPage from './pages/WarmupPage'
import ParserPage from './pages/ParserPage'
import SettingsPage from './pages/SettingsPage'
import { Spinner } from './components/ui'

// Защищённый роут — редиректит на /login если не авторизован
function PrivateRoute({ children }) {
  const { user, loading } = useAuthStore()
  if (loading) {
    return (
      <div style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center' }}>
        <Spinner size={32} />
      </div>
    )
  }
  return user ? children : <Navigate to="/login" replace />
}

export default function App() {
  const init = useAuthStore(s => s.init)

  // При загрузке проверяем токен
  useEffect(() => { init() }, [])

  return (
    <Routes>
      {/* Public */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />

      {/* Protected — всё внутри Layout */}
      <Route path="/*" element={
        <PrivateRoute>
          <Layout>
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/accounts" element={<AccountsPage />} />
              <Route path="/accounts/:id" element={<AccountDetailPage />} />
              <Route path="/proxies" element={<ProxiesPage />} />
              <Route path="/inbox" element={<InboxPage />} />
              <Route path="/tasks" element={<TasksPage />} />
              <Route path="/analytics" element={<AnalyticsPage />} />
              <Route path="/commenting" element={<CommentingPage />} />
              <Route path="/warmup" element={<WarmupPage />} />
              <Route path="/parser" element={<ParserPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Layout>
        </PrivateRoute>
      } />
    </Routes>
  )
}