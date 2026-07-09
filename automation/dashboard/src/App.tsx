import { BrowserRouter as Router, Routes, Route, NavLink, Navigate } from 'react-router-dom';
import type { ReactElement } from 'react';
import { Boxes, LayoutDashboard, Settings, PlayCircle, GitMerge, BarChart3, BrainCircuit, Activity, Shield, Server, Wand2, Code2, MessageSquare } from 'lucide-react';
import DashboardHome from './pages/DashboardHome';
import RunDetails from './pages/RunDetails';
import SettingsPage from './pages/SettingsPage';
import AutomationPage from './pages/AutomationPage';
import CIPage from './pages/CIPage';
import AnalyticsPage from './pages/AnalyticsPage';
import InsightsPage from './pages/InsightsPage';
import CommandCenter from './pages/CommandCenter';
import OperationsDashboard from './pages/OperationsDashboard';
import DeviceOpsCenter from './pages/DeviceOpsCenter';
import AdminCenter from './pages/AdminCenter';
import { LoginPage } from './pages/LoginPage';
import ScriptGeneratorPage from './pages/ScriptGeneratorPage';
import ScriptEditorPage from './pages/ScriptEditorPage';
import ChatPage from './pages/ChatPage';
import { getAuthToken } from './api';

const ProtectedRoute = ({ children }: { children: ReactElement }) => {
  const token = getAuthToken();
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return children;
};

const ProtectedLayout = ({ children }: { children: ReactElement }) => (
  <div className="app-container">
    <aside className="sidebar">
      <div className="sidebar-logo">
        <Boxes size={28} />
        <span>Automation Platform</span>
      </div>
      <nav className="nav-links">
        <NavLink to="/" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <LayoutDashboard size={20} />
          Dashboard
        </NavLink>
        <NavLink to="/automation" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <PlayCircle size={20} />
          Automation
        </NavLink>
        <NavLink to="/ops" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Activity size={20} />
          Ops Center
        </NavLink>
        <NavLink to="/fleet" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Server size={20} />
          Device Fleet
        </NavLink>
        <NavLink to="/ci-cd" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <GitMerge size={20} />
          CI/CD
        </NavLink>
        <NavLink to="/insights" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <BrainCircuit size={20} />
          AI Insights
        </NavLink>
        <NavLink to="/script-generator" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Wand2 size={20} />
          Script Generator
        </NavLink>
        <NavLink to="/script-editor" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Code2 size={20} />
          Script Editor
        </NavLink>
        <NavLink to="/chat" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <MessageSquare size={20} />
          AI Chat
        </NavLink>
        <NavLink to="/command-center" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <BrainCircuit size={20} />
          Command Center
        </NavLink>
        <NavLink to="/analytics" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <BarChart3 size={20} />
          Analytics
        </NavLink>
        <NavLink to="/settings" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Settings size={20} />
          Settings
        </NavLink>
        <div className="flex-1"></div>
        <NavLink to="/admin" className={({isActive}) => isActive ? "nav-link active text-red-400" : "nav-link text-slate-500 hover:text-red-400"}>
          <Shield size={20} />
          Admin
        </NavLink>
      </nav>
    </aside>
    <main className="main-content">
      {children}
    </main>
  </div>
);

function App() {
  return (
    <Router>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        
        {/* Protected Routes */}
        <Route path="/" element={<ProtectedRoute><ProtectedLayout><DashboardHome /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/automation" element={<ProtectedRoute><ProtectedLayout><AutomationPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/ops" element={<ProtectedRoute><ProtectedLayout><OperationsDashboard /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/fleet" element={<ProtectedRoute><ProtectedLayout><DeviceOpsCenter /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/admin" element={<ProtectedRoute><ProtectedLayout><AdminCenter /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/ci-cd" element={<ProtectedRoute><ProtectedLayout><CIPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/insights" element={<ProtectedRoute><ProtectedLayout><InsightsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/command-center" element={<ProtectedRoute><ProtectedLayout><CommandCenter /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/analytics" element={<ProtectedRoute><ProtectedLayout><AnalyticsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/run/:id" element={<ProtectedRoute><ProtectedLayout><RunDetails /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/settings" element={<ProtectedRoute><ProtectedLayout><SettingsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/script-generator" element={<ProtectedRoute><ProtectedLayout><ScriptGeneratorPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/script-editor" element={<ProtectedRoute><ProtectedLayout><ScriptEditorPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/chat" element={<ProtectedRoute><ProtectedLayout><ChatPage /></ProtectedLayout></ProtectedRoute>} />
      </Routes>
    </Router>
  );
}

export default App;
