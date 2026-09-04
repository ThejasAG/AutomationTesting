from sqlalchemy import (Column, String, Integer, DateTime, Float, ForeignKey, Text, JSON,
                        Boolean, UniqueConstraint)
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

    # A human-readable narrative report of this run (AI-generated, stored for reuse).
    report_summary = Column(Text, nullable=True)
    report_generated_at = Column(DateTime, nullable=True)

    # CI / Orchestration data
    triggered_by = Column(String, nullable=True)
    branch = Column(String, nullable=True)
    commit_sha = Column(String, nullable=True)
    timeline = Column(String, nullable=True)
    
    # Intelligence Data
    is_flaky = Column(Boolean, default=False)
    risk_score = Column(Integer, default=0)
    # Flaky-retry: how many attempts this run took, and whether it only passed on a retry.
    # Scenarios the PLAN chose for this job, as [{id, name, steps}]. The planner picks a
    # subset via the dependency graph, but the agent used to ignore it entirely and run
    # the repo's fixed `execution.command` — so the plan was advisory and every PR ran
    # the same suite. Null/empty means "no plan; run the project's default command".
    planned_scenarios = Column(JSON, nullable=True)
    attempts = Column(Integer, default=1)
    flaky_detected = Column(Boolean, default=False)
    # Set True when a visual regression was found on an otherwise-passing run.
    visual_warning = Column(Boolean, default=False)
    # Set True when the app crashed / red-boxed during the run (APP bug, not automation).
    crash_detected = Column(Boolean, default=False)
    
    # Distributed Execution
    agent_id = Column(String, ForeignKey("execution_agents.id"), nullable=True)
    # Which MACHINE this run is meant for — durable routing intent, decided at
    # queue time. Distinct from agent_id, which is the worker that happens to be
    # executing it right now: a stranded job has its agent_id cleared on reclaim
    # but must keep its machine_id, or it loses the machine it was routed to.
    #
    # Nullable, and NULL is meaningful: "legacy run, match on UDID alone". Every
    # existing row stays NULL — historical agent ids predate stable machine
    # identity and are not reliable enough to backfill from.
    machine_id = Column(String(120), nullable=True, index=True)
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
    # Failure evidence: a screenshot of the screen at the moment the segment failed,
    # stored as a self-contained `data:image/png;base64,...` URI. Shown in the rich
    # (web) report next to the failure reason; intentionally NOT included in the
    # downloadable/exported report.
    screenshot = Column(Text, nullable=True)
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
    # True while this machine's row exists only because the BACKEND registered
    # itself — no execution agent has claimed it yet. The backend never polls, so
    # a row in that state must not be treated as a worker that can take jobs.
    # A real agent registering on the same machine adopts the row and clears this,
    # which is the point: one physical Mac, one row, whoever reports it.
    is_backend = Column(Boolean, default=False)

    # Per-agent credential (Phase 4F.1).
    #
    # Until now every agent shared one AGENT_TOKEN and announced its own identity
    # in the request body, so any holder of that token could act as any machine.
    # Registration now issues a secret unique to this machine; only its SHA-256 is
    # stored, so the database never holds anything replayable. The backend derives
    # the agent from the presented credential instead of believing the client.
    agent_credential_hash = Column(String(64), nullable=True, index=True)
    agent_credential_issued_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    
    jobs = relationship("TestRun", back_populates="agent")

class DeviceRecord(Base):
    """A device (simulator or physical) as reported by one machine.

    Named DeviceRecord, not Device, because `automation.device_manager.models.Device`
    is the pydantic shape the API already returns — the two are different layers and
    code that touches both should not have to guess which one it holds.

    Uniqueness is (machine_id, udid), NOT udid alone. A UDID identifies a simulator
    within one Mac's CoreSimulator; it is not a global identifier, and two machines
    may legitimately report the same one. Keying on udid alone would silently merge
    two different physical devices into one row the first time a second Mac joined.

    A surrogate `id` is the primary key rather than the composite pair, matching every
    other table here (String(36) uuid PKs) and keeping a future allocation/reservation
    table able to reference a device with a single column.
    """
    __tablename__ = "devices"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)

    # The machine that owns the device. For an agent this is execution_agents.id;
    # for the backend's own simulators it is "local:<hostname>" — see
    # device_manager.service.local_machine_id().
    machine_id = Column(String(120), nullable=False, index=True)
    udid = Column(String(255), nullable=False, index=True)

    hostname = Column(String(255), nullable=True)
    # Mirrors device_manager.models.Device.provider: an agent id, or "local".
    provider = Column(String(120), nullable=True)

    name = Column(String(255), nullable=True)
    manufacturer = Column(String(120), nullable=True)
    model = Column(String(255), nullable=True)
    platform = Column(String(50), nullable=True)
    platform_version = Column(String(50), nullable=True)
    connection_type = Column(String(50), nullable=True)

    # Last reported status. Never trusted on its own after a restart — freshness is
    # decided from last_seen against DEVICE_STALE_AFTER, exactly as before.
    status = Column(String(50), default="UNKNOWN")
    capabilities = Column(JSON, nullable=True)

    last_seen = Column(DateTime, nullable=True)

    # ── Reservation (Phase 4E.1) ────────────────────────────────────────────
    # Exclusive use of ONE simulator. The row is the resource: its id already
    # encodes (machine_id, udid) via the unique constraint below, so locking the
    # row is exactly locking one physical simulator on one machine — never a
    # machine-wide lock, so two simulators on one Mac stay independently usable.
    #
    # reserved_by is the OWNER and the only thing ownership is decided by. It
    # holds a TestRun.id: stable across an agent restart and never cleared by
    # reclaim, unlike agent_id. NULL means available.
    reserved_by = Column(String(36), nullable=True, index=True)
    reserved_at = Column(DateTime, nullable=True)
    # Observability only — 'reserved' (acquired) or 'active' (execution started).
    # Never consulted to decide ownership; reserved_by is the authority.
    reserved_state = Column(String(20), nullable=True)

    # Machine-global WDA port for this simulator (Phase 4E.2).
    #
    # wda.port_for() kept its map in a module-level dict, so two OS processes each
    # assigned 8100 to their FIRST device — two different simulators, one port.
    # Storing it on the row makes the assignment machine-scoped (ports are only
    # compared within a machine_id) and visible across processes. Allocated once
    # and kept: a stable port per device is easier to reason about than a pool
    # that has to be released, and it cannot leak.
    wda_port = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("machine_id", "udid", name="uq_devices_machine_udid"),
    )


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


class SavedScenario(Base):
    """A reusable, user-built scenario: an ordered list of plain-language steps
    run against a chosen app + simulator. Created/edited/deleted from the
    Scenarios tab and executed through the scenario runner."""
    __tablename__ = "saved_scenarios"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    project_id = Column(String(36), ForeignKey("test_projects.id"), nullable=True, index=True)
    bundle_id = Column(String(255), nullable=True)     # overrides the project's bundle id
    device_id = Column(String(255), nullable=True)     # target simulator UDID
    steps = Column(JSON, default=list)                 # ["tap Book Table", "select date", …]
    # Screens/modules this scenario exercises (e.g. ["Store", "Cart"]). Used for
    # graph-driven smart test selection: run a scenario only when a PR's affected
    # files touch what it covers.
    covers = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "project_id": self.project_id, "bundle_id": self.bundle_id,
            "device_id": self.device_id, "steps": self.steps or [],
            "covers": self.covers or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CrossAppFlowEdit(Base):
    """A user-edited (or brand-new) cross-app flow.

    The built-in flows live in code (`cross_app_flows.FLOWS`) because their steps
    encode hard-won on-device knowledge. Editing them from the dashboard writes a row
    here instead of touching that file: `id` matching a built-in OVERRIDES it, any
    other `id` is a new flow. Deleting the row reverts to the built-in definition, so
    an experiment can never permanently destroy a working flow.
    """
    __tablename__ = "cross_app_flow_edits"

    id = Column(String(100), primary_key=True)          # flow_id, e.g. "flow1" or "my_flow"
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    # [{"num": "1", "name": "...", "role": "consumer|waiter|kitchen", "steps": [...]}, ...]
    segments = Column(JSON, default=list)
    based_on = Column(String(100), nullable=True)       # built-in id this overrides, if any
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "description": self.description or "",
            "segments": self.segments or [], "based_on": self.based_on,
            "custom": True,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class PerformanceMetric(Base):
    __tablename__ = "performance_metrics"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String, ForeignKey("test_runs.id"))
    timestamp = Column(DateTime)
    cpu_percent = Column(Float)
    memory_mb = Column(Float)
    fps = Column(Float, nullable=True)
    network_requests = Column(Integer, default=0)
    avg_response_ms = Column(Float, nullable=True)


class PerformanceSummary(Base):
    __tablename__ = "performance_summaries"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String, ForeignKey("test_runs.id"), unique=True)
    app_launch_time_s = Column(Float, nullable=True)
    avg_cpu_percent = Column(Float)
    peak_cpu_percent = Column(Float)
    avg_memory_mb = Column(Float)
    peak_memory_mb = Column(Float)
    avg_fps = Column(Float, nullable=True)
    min_fps = Column(Float, nullable=True)
    dropped_frames = Column(Integer, default=0)
    api_calls = Column(Integer, default=0)
    avg_api_response_ms = Column(Float, nullable=True)
    slowest_api_ms = Column(Float, nullable=True)
    slowest_api_endpoint = Column(String(500), nullable=True)
    performance_score = Column(Integer, nullable=True)
    grade = Column(String(1), nullable=True)
    issues = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Ticket(Base):
    """A pasted issue/ticket linked to a PR + the scenarios that verify it.

    When a PR whose number matches `pr_number` is pushed, the platform runs the
    linked scenarios and flips `status`. Powers the traceability view in the
    (repurposed) Scripts page.
    """
    __tablename__ = "tickets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(500))
    description = Column(Text)                          # full pasted ticket text
    project_id = Column(String(36), ForeignKey("test_projects.id"), nullable=True, index=True)
    pr_number = Column(String(50), nullable=True, index=True)   # linked PR (set manually or auto-filled)
    pr_url = Column(String(500), nullable=True)
    # Ticket/branch key that exists BEFORE the PR (e.g. "NEWVYA-1134"). When a PR whose
    # branch/title/body contains this key is opened, the webhook links + tests it and
    # back-fills pr_number — so you don't need a PR number that doesn't exist yet.
    match_key = Column(String(120), nullable=True, index=True)
    scenario_ids = Column(JSON, default=list)          # SavedScenario ids that cover it
    # A reusable setup scenario (e.g. "Book an event") whose steps run BEFORE each
    # linked scenario — so a "cancel event" check first creates the event to cancel.
    setup_scenario_id = Column(String(36), nullable=True)
    ticket_type = Column(String(20), default="ui")     # ui | calc | data | crash | mixed
    status = Column(String(20), default="untested")    # untested | passed | failed | running
    last_run_id = Column(String, nullable=True)        # most recent TestRun
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "project_id": self.project_id, "pr_number": self.pr_number, "pr_url": self.pr_url,
            "match_key": self.match_key,
            "scenario_ids": self.scenario_ids or [], "ticket_type": self.ticket_type,
            "setup_scenario_id": self.setup_scenario_id,
            "status": self.status, "last_run_id": self.last_run_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class VisualRegressionResult(Base):
    __tablename__ = "visual_regression_results"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String, ForeignKey("test_runs.id"))
    screen_name = Column(String(255))
    diff_percentage = Column(Float)
    severity = Column(String(10))  # high/medium/low/none
    baseline_path = Column(String(500))
    current_path = Column(String(500))
    diff_path = Column(String(500))
    passed = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
