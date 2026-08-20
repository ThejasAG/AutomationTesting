import { BrowserRouter as Router, Routes, Route, NavLink, Navigate, useNavigate } from 'react-router-dom';
import type { ReactElement } from 'react';
import { Boxes, LayoutDashboard, Settings, PlayCircle, Code2, MessageSquare, FolderGit2, GitBranch, GitPullRequest, ListChecks, FileText, Zap, Workflow, Clapperboard } from 'lucide-react';
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
import PerformancePage from './pages/PerformancePage';
import WorkflowPage from './pages/WorkflowPage';
import BuildUpdateBell from './components/BuildUpdateBell';
import JobQueuePage from './pages/JobQueuePage';
import { getAuthToken, getGoldenRun } from './api';

const ProtectedRoute = ({ children }: { children: ReactElement }) => {
  const token = getAuthToken();
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return children;
};

const ProtectedLayout = ({ children }: { children: ReactElement }) => {
  const navigate = useNavigate();
  // Demo mode: jump straight to the pinned known-green run (Live Steps + report) if a
  // live run blips during a demo. Falls back to the latest passed run server-side.
  const openDemo = async () => {
    try {
      const g = await getGoldenRun();
      if (g.run_id) navigate(`/run/${g.run_id}`);
      else alert('No green run recorded yet — run flow_book_demo once to create one.');
    } catch { alert('Could not load the demo run.'); }
  };
  return (
  <div className="app-container">
    <aside className="sidebar">
      <div className="sidebar-logo">
        <Boxes size={28} />
        <span>Automation Platform</span>
      </div>
      <nav className="nav-links">
        <button
          onClick={openDemo}
          className="nav-link"
          title="Show the last known-green run (Live Steps + report) — your fallback if a live run blips"
          style={{ background: 'rgba(52,211,153,0.14)', color: 'var(--success, #34d399)',
                   border: '1px solid rgba(52,211,153,0.3)', cursor: 'pointer',
                   width: '100%', textAlign: 'left', font: 'inherit' }}
        >
          <Clapperboard size={20} />
          Demo run
        </button>
        <BuildUpdateBell />
        <NavLink to="/" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <LayoutDashboard size={20} />
          Dashboard
        </NavLink>
        <NavLink to="/projects" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <FolderGit2 size={20} />
          Projects
        </NavLink>
        <NavLink to="/workflow" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Workflow size={20} />
          Workflow
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
        <NavLink to="/queue" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <ListChecks size={20} />
          Job Queue
        </NavLink>
        <NavLink to="/scenarios" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <ListChecks size={20} />
          Scenarios
        </NavLink>
        <NavLink to="/reports" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <FileText size={20} />
          Reports
        </NavLink>
        <NavLink to="/performance" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Zap size={20} />
          Performance
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
};

function App() {
  return (
    <Router>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        
        {/* Protected Routes */}
        <Route path="/" element={<ProtectedRoute><ProtectedLayout><DashboardHome /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/projects" element={<ProtectedRoute><ProtectedLayout><ProjectsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/graph" element={<ProtectedRoute><ProtectedLayout><GraphPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/workflow" element={<ProtectedRoute><ProtectedLayout><WorkflowPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/automation" element={<ProtectedRoute><ProtectedLayout><AutomationPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/pull-requests" element={<ProtectedRoute><ProtectedLayout><PullRequestsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/scripts" element={<ProtectedRoute><ProtectedLayout><ScriptEditorPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/scenarios" element={<ProtectedRoute><ProtectedLayout><ScenariosPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/queue" element={<ProtectedRoute><ProtectedLayout><JobQueuePage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/reports" element={<ProtectedRoute><ProtectedLayout><ReportsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/performance" element={<ProtectedRoute><ProtectedLayout><PerformancePage /></ProtectedLayout></ProtectedRoute>} />
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
