import { BrowserRouter as Router, Routes, Route, NavLink, Navigate, useNavigate } from 'react-router-dom';
import { Suspense, lazy, useEffect } from 'react';
import type { ReactElement } from 'react';
import { Boxes, LayoutDashboard, Settings, Code2, MessageSquare, FolderGit2, GitBranch, GitPullRequest, ListChecks, FileText, Workflow, Clapperboard, Crosshair, Bot, Loader2} from 'lucide-react';
import DashboardHome from './pages/DashboardHome';
import { LoginPage } from './pages/LoginPage';
import BuildUpdateBell from './components/BuildUpdateBell';
import { getAuthToken, getGoldenRun } from './api';

// Every page but the landing ones loads on first visit, so the dashboard opens
// without first downloading the charts, the code editor and the graph library.
// Once the app is up they are fetched in the background, so moving between pages
// stays instant.
const PAGES = {
  scenarios: () => import('./pages/ScenariosPage'),
  inspector: () => import('./pages/InspectorPage'),
  reports: () => import('./pages/ReportsPage'),
  projects: () => import('./pages/ProjectsPage'),
  graph: () => import('./pages/GraphPage'),
  run: () => import('./pages/RunDetails'),
  settings: () => import('./pages/SettingsPage'),
  prs: () => import('./pages/PullRequestsPage'),
  scripts: () => import('./pages/ScriptEditorPage'),
  chat: () => import('./pages/ChatPage'),
  workflow: () => import('./pages/WorkflowPage'),
  queue: () => import('./pages/JobQueuePage'),
  agent: () => import('./pages/AgentPage'),
};
const ScenariosPage = lazy(PAGES.scenarios);
const InspectorPage = lazy(PAGES.inspector);
const ReportsPage = lazy(PAGES.reports);
const ProjectsPage = lazy(PAGES.projects);
const GraphPage = lazy(PAGES.graph);
const RunDetails = lazy(PAGES.run);
const SettingsPage = lazy(PAGES.settings);
const PullRequestsPage = lazy(PAGES.prs);
const ScriptEditorPage = lazy(PAGES.scripts);
const ChatPage = lazy(PAGES.chat);
const WorkflowPage = lazy(PAGES.workflow);
const JobQueuePage = lazy(PAGES.queue);
const AgentPage = lazy(PAGES.agent);

const PageLoading = () => (
  <div style={{ color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 8 }}>
    <Loader2 size={16} className="spin" /> Loading…
  </div>
);

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
        <NavLink to="/agent" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Bot size={20} />
          AI Agent
        </NavLink>
        <NavLink to="/scenarios" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <ListChecks size={20} />
          Scenarios
        </NavLink>
        <NavLink to="/inspector" className={({isActive}) => isActive ? "nav-link active" : "nav-link"}>
          <Crosshair size={20} />
          Inspector
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
      <Suspense fallback={<PageLoading />}>{children}</Suspense>
    </main>
  </div>
  );
};

function App() {
  useEffect(() => {
    const t = window.setTimeout(() => Object.values(PAGES).forEach(load => load().catch(() => {})), 1500);
    return () => window.clearTimeout(t);
  }, []);

  return (
    <Router>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        
        {/* Protected Routes */}
        <Route path="/" element={<ProtectedRoute><ProtectedLayout><DashboardHome /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/inspector" element={<ProtectedRoute><ProtectedLayout><InspectorPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/projects" element={<ProtectedRoute><ProtectedLayout><ProjectsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/graph" element={<ProtectedRoute><ProtectedLayout><GraphPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/workflow" element={<ProtectedRoute><ProtectedLayout><WorkflowPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/pull-requests" element={<ProtectedRoute><ProtectedLayout><PullRequestsPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/scripts" element={<ProtectedRoute><ProtectedLayout><ScriptEditorPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/scenarios" element={<ProtectedRoute><ProtectedLayout><ScenariosPage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/queue" element={<ProtectedRoute><ProtectedLayout><JobQueuePage /></ProtectedLayout></ProtectedRoute>} />
        <Route path="/agent" element={<ProtectedRoute><ProtectedLayout><AgentPage /></ProtectedLayout></ProtectedRoute>} />
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
