import { BrowserRouter as Router, Routes, Route, NavLink, Navigate } from 'react-router-dom';
import type { ReactElement } from 'react';
import { Boxes, LayoutDashboard, Settings, PlayCircle, Code2, MessageSquare, FolderGit2, GitBranch, GitPullRequest, ListChecks, FileText } from 'lucide-react';
import DashboardHome from './pages/DashboardHome';
import ScenariosPage from './pages/ScenariosPage';
import ReportsPage from './pages/ReportsPage';
import ProjectsPage from './pages/ProjectsPage';
import GraphPage from './pages/GraphPage';
import RunDetails from './pages/RunDetails';
import SettingsPage from './pages/SettingsPage';
import AutomationPage from './pages/AutomationPage';
import PullRequestsPage from './pages/PullRequestsPage';
import { LoginPage } from './pages/LoginPage';
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
        <NavLink to="/projects" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <FolderGit2 size={20} />
          Projects
        </NavLink>
        <NavLink to="/graph" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <GitBranch size={20} />
          Dependency Graph
        </NavLink>
        <NavLink to="/automation" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <PlayCircle size={20} />
          Automation
        </NavLink>
        <NavLink to="/pull-requests" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <GitPullRequest size={20} />
          Pull Requests
        </NavLink>
        <NavLink to="/scripts" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Code2 size={20} />
          Scripts
        </NavLink>
        <NavLink to="/scenarios" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <ListChecks size={20} />
          Scenarios
        </NavLink>
        <NavLink to="/reports" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <FileText size={20} />
          Reports
        </NavLink>
        <NavLink to="/chat" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <MessageSquare size={20} />
          AI Chat
        </NavLink>
        <div className="flex-1"></div>
        <NavLink to="/settings" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Settings size={20} />
          Settings
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
        <Route path="/projects" element={<ProtectedRoute><ProtectedLayout><ProjectsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/graph" element={<ProtectedRoute><ProtectedLayout><GraphPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/automation" element={<ProtectedRoute><ProtectedLayout><AutomationPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/pull-requests" element={<ProtectedRoute><ProtectedLayout><PullRequestsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/scripts" element={<ProtectedRoute><ProtectedLayout><ScriptEditorPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/scenarios" element={<ProtectedRoute><ProtectedLayout><ScenariosPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/reports" element={<ProtectedRoute><ProtectedLayout><ReportsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/chat" element={<ProtectedRoute><ProtectedLayout><ChatPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/run/:id" element={<ProtectedRoute><ProtectedLayout><RunDetails /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/settings" element={<ProtectedRoute><ProtectedLayout><SettingsPage /></ProtectedLayout></ProtectedRoute>} />

        {/* Removed sections — redirect stale bookmarks instead of 404. */}
        <Route path="/script-editor" element={<Navigate to="/scripts" replace />} />
        <Route path="/script-generator" element={<Navigate to="/scripts" replace />} />
        <Route path="/ops" element={<Navigate to="/" replace />} />
        <Route path="/fleet" element={<Navigate to="/" replace />} />
        <Route path="/ci-cd" element={<Navigate to="/pull-requests" replace />} />
        <Route path="/insights" element={<Navigate to="/" replace />} />
        <Route path="/command-center" element={<Navigate to="/" replace />} />
        <Route path="/analytics" element={<Navigate to="/" replace />} />
        <Route path="/admin" element={<Navigate to="/settings" replace />} />
      </Routes>
    </Router>
  );
}

export default App;
