from sqlalchemy import Column, String, Integer, DateTime, Float, ForeignKey, Text, JSON, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

from .config import Base

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(100), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), default="viewer") # admin, qa, developer, viewer
    created_at = Column(DateTime, default=datetime.utcnow)

    # test_runs = relationship("TestRun", back_populates="user")

class ApplicationGroup(Base):
    """A family of related apps (e.g. "Food Delivery" → Business / Consumer / Admin).

    Each member project stays fully independent; the group only expresses that
    they belong together, so future impact analysis can fan a change out across
    every app in the same group.
    """
    __tablename__ = "application_groups"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # When set, a PR touching a project in this group ALSO triggers the Android
    # cross-app bot (Consumer + Business) at this adapter URL.
    android_bot_url = Column(String(500), nullable=True)

    graphs = relationship(
        "DependencyGraph", back_populates="group", cascade="all, delete-orphan"
    )

    projects = relationship("TestProject", back_populates="group")


class TestProject(Base):
    __tablename__ = "test_projects"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Optional — a project may exist without a group.
    group_id = Column(String(36), ForeignKey("application_groups.id"), nullable=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    git_url = Column(String(1024), nullable=False)
    default_branch = Column(String(255), default="main")
    status = Column(String(50), default="active") # active, archiving, error
    owner = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Target platform for automation: ios | android (iOS is the primary target)
    platform = Column(String(50), default="ios")
    # Where the code lives: github | gitlab | local
    repo_type = Column(String(50), default="github")
    # Auto-detected from the cloned tree — see projects/detector.py
    # ios | react_native | flutter | android | python | java | unknown
    project_type = Column(String(50), default="unknown")

    # Repository lifecycle state:
    # not_cloned | syncing | cloned | ready | outdated | clone_failed
    clone_status = Column(String(50), default="not_cloned")
    clone_error = Column(Text, nullable=True)
    current_branch = Column(String(255), nullable=True)
    last_pull_at = Column(DateTime, nullable=True)
    last_execution_at = Column(DateTime, nullable=True)

    # Build artifact produced from the checked-out source (.app / .apk), plus the
    # iOS bundle id so the app can be launched after install.
    app_path = Column(String(1024), nullable=True)
    app_bundle_id = Column(String(255), nullable=True)
    build_status = Column(String(50), default="not_built")  # not_built|building|built|build_failed
    build_error = Column(Text, nullable=True)
    last_build_at = Column(DateTime, nullable=True)

    # Execution link
    runs = relationship("TestRun", back_populates="project")
    group = relationship("ApplicationGroup", back_populates="projects")

class TestRun(Base):
    __tablename__ = "test_runs"
    id = Column(String, primary_key=True, index=True)
    project_id = Column(String, ForeignKey("test_projects.id"), nullable=True)
    test_suite = Column(String, index=True)
    test_name = Column(String, index=True)
    status = Column(String)  # passed, failed, running
    started_at = Column(DateTime)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    device_name = Column(String)
    os_version = Column(String)
    platform = Column(String)
    app_version = Column(String)
    build_number = Column(String)
    environment = Column(String)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # CI / Orchestration data
    triggered_by = Column(String, nullable=True)
    branch = Column(String, nullable=True)
    commit_sha = Column(String, nullable=True)
    timeline = Column(String, nullable=True)
    
    # Intelligence Data
    is_flaky = Column(Boolean, default=False)
    risk_score = Column(Integer, default=0)
    
    # Distributed Execution
    agent_id = Column(String, ForeignKey("execution_agents.id"), nullable=True)
    job_state = Column(String, default="queued") # queued, assigned, downloading, preparing, running, collecting_evidence, completed, failed, cancelled

    # Which engine ran this: "ios" = Appium/XCUITest, "android" = Vya-agentic-BOT
    # (Consumer + Business cross-app). Drives the badge + Scenarios tab in the UI.
    bot_type = Column(String(20), default="ios")

    project = relationship("TestProject", back_populates="runs")
    agent = relationship("ExecutionAgent", back_populates="jobs")
    rca_report = relationship("RCAReport", back_populates="run", uselist=False)
    evidence = relationship("EvidenceBundle", back_populates="run", uselist=False)
    scenarios = relationship(
        "ScenarioResult", back_populates="run", cascade="all, delete-orphan"
    )


class ScenarioResult(Base):
    """One Consumer+Business cross-app scenario result from the Android bot.

    The bot used to write these to a local scenario_history.json on its own
    machine; it now POSTs each one here so the web dashboard, the PR gate, and
    the run history all see the same source of truth. The both-must-pass rule
    (FAIL if either side failed) is applied on write.
    """
    __tablename__ = "scenario_results"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String, ForeignKey("test_runs.id"))
    scenario_num = Column(String(10))
    scenario_name = Column(String(500))
    status = Column(String(10))                       # PASS | FAIL
    consumer_status = Column(String(10), default="N/A")
    business_status = Column(String(10), default="N/A")
    error = Column(Text, nullable=True)
    reasons = Column(JSON, nullable=True)
    launch_time = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    run = relationship("TestRun", back_populates="scenarios")

# ── Cross-app dependency analysis ────────────────────────────────────────────
#
# Groups live in ApplicationGroup (above); membership is TestProject.group_id.
# There used to be a SECOND pair of models here (AppGroup / AppGroupMember) that
# only the dependency router wrote to — so a group created anywhere else in the
# product was invisible to the graph, which then reported "no groups" while the
# group sat in the other table. One grouping model, one membership link.

class DependencyGraph(Base):
    """A snapshot of the app -> endpoint graph produced by a scan."""
    __tablename__ = "dependency_graphs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(
        String(36), ForeignKey("application_groups.id"), nullable=False
    )
    graph_data = Column(JSON)      # the full graph dict (nodes/edges/endpoints)
    api_endpoints = Column(JSON)   # discovered endpoints, endpoint -> {apps, sites}
    scanned_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("ApplicationGroup", back_populates="graphs")


class ExecutionAgent(Base):
    __tablename__ = "execution_agents"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    hostname = Column(String(255), nullable=False)
    os = Column(String(50), nullable=False)
    status = Column(String(50), default="offline") # online, busy, offline, unhealthy
    capabilities = Column(JSON, nullable=True) # Supported frameworks, python version, node version, adb, xcode
    connected_devices = Column(JSON, nullable=True)
    last_heartbeat = Column(DateTime, default=datetime.utcnow)
    cpu_usage = Column(Float, default=0.0)
    memory_usage = Column(Float, default=0.0)
    restart_count = Column(Integer, default=0)
    uptime_seconds = Column(Integer, default=0)
    is_draining = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    jobs = relationship("TestRun", back_populates="agent")

class ModuleStability(Base):
    __tablename__ = "module_stability"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    module_name = Column(String, index=True)
    pass_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    flaky_count = Column(Integer, default=0)
    last_updated = Column(String)

class UserFeedback(Base):
    __tablename__ = "user_feedback"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    recommendation_type = Column(String) # e.g., "test_selection", "rca"
    reference_id = Column(String) # e.g., run_id
    is_helpful = Column(Boolean)
    actual_fix = Column(String, nullable=True)
    created_at = Column(String)

class RCAReport(Base):
    __tablename__ = "rca_reports"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(36), ForeignKey("test_runs.id"), nullable=False, unique=True)
    root_cause = Column(Text)
    failure_category = Column(String(255))
    # The LLM returns this (see RCAAnalysis.affected_modules) and the dashboard
    # renders it, but there was no column to persist it — so it was silently
    # dropped and came back as undefined, crashing RunDetails on .map().
    affected_modules = Column(JSON, nullable=True)
    confidence = Column(Float)
    possible_reason = Column(Text)
    impact = Column(Text)
    suggested_fix = Column(Text)
    priority = Column(String(50))
    severity = Column(String(50))
    responsible_module = Column(String(255))
    summary = Column(Text)
    llm_provider = Column(String(100))
    llm_model = Column(String(100))
    generated_at = Column(DateTime, default=datetime.utcnow)

    run = relationship("TestRun", back_populates="rca_report")

class EvidenceBundle(Base):
    __tablename__ = "evidence_bundles"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(36), ForeignKey("test_runs.id"), nullable=False, unique=True)
    git_commit = Column(String(255), nullable=True)
    git_author = Column(String(255), nullable=True)
    git_branch = Column(String(255), nullable=True)
    git_diff = Column(Text, nullable=True)
    changed_files = Column(Text, nullable=True) # Stored as JSON string or text
    appium_logs = Column(Text, nullable=True)   # Stored as JSON string
    device_logs = Column(Text, nullable=True)
    screenshot_path = Column(String(1024), nullable=True)
    video_path = Column(String(1024), nullable=True)
    xml_page_source = Column(Text, nullable=True)
    network_logs = Column(Text, nullable=True)
    failed_locator = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    run = relationship("TestRun", back_populates="evidence")

class AIRecommendation(Base):
    __tablename__ = "ai_recommendations"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("test_projects.id"), nullable=True)
    run_id = Column(String(36), ForeignKey("test_runs.id"), nullable=True)
    type = Column(String(50)) # test_plan, self_heal, bug_report
    payload = Column(JSON)
    status = Column(String(50), default="pending") # pending, accepted, rejected, modified
    feedback = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class ProjectSettings(Base):
    __tablename__ = "project_settings"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("test_projects.id"), nullable=False, unique=True)
    jira_url = Column(String(255), nullable=True)
    jira_token = Column(String(255), nullable=True)
    github_repo = Column(String(255), nullable=True)
    github_token = Column(String(255), nullable=True)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    timestamp = Column(DateTime, default=datetime.utcnow)
    user_id = Column(String(255), nullable=True)
    action = Column(String(255), nullable=False)
    ip_address = Column(String(50), nullable=True)
    resource_type = Column(String(100), nullable=True)
    resource_id = Column(String(255), nullable=True)
    details = Column(JSON, nullable=True)

class SystemAlert(Base):
    __tablename__ = "system_alerts"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    type = Column(String(100), nullable=False) # e.g. agent_offline, queue_overflow
    severity = Column(String(50), default="warning") # info, warning, critical
    status = Column(String(50), default="active") # active, resolved
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)


# ── Chat Session Storage ────────────────────────────────────────────────────

class ChatSession(Base):
    """Persistent chat session — one row per conversation thread."""
    __tablename__ = "chat_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), nullable=True, index=True)
    # Title is set to the first user message (truncated to 100 chars).
    title = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    messages = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )


class ChatMessage(Base):
    """Individual message within a chat session."""
    __tablename__ = "chat_messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(
        String(36), ForeignKey("chat_sessions.id"), nullable=False, index=True
    )
    role = Column(String(20), nullable=False)  # "user" | "assistant"
    content = Column(Text, nullable=False)
    # "text" for plain/markdown, "structured" for JSON analysis cards
    message_type = Column(String(20), default="text")
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("ChatSession", back_populates="messages")
