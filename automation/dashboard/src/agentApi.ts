// AI Agent page API (backend: automation/api/v1/routers/agentic.py).
// The agent is Claude Code sessions (automation/claude_code/): each session runs on
// its own git branch and every change waits for a person to approve it.
// Kept out of api.ts so the agent can change without touching the shared client.
import { API_BASE, getHeaders } from './api';

export type SessionKind = 'heal' | 'fix_run' | 'analyze' | 'generate' | 'nightly';
export type SessionTrigger = 'manual' | 'run_all' | 'nightly' | 'app_change';
export type SessionStatus =
    | 'queued' | 'preparing' | 'running' | 'checking'
    | 'review' | 'no_changes' | 'gates_failed' | 'approved' | 'rejected'
    | 'failed' | 'stopped' | 'interrupted';

/** Statuses of a session that is still working. */
export const ACTIVE_STATUSES: readonly string[] = ['queued', 'preparing', 'running', 'checking'];
export const isActiveStatus = (s?: string | null) => !!s && ACTIVE_STATUSES.includes(s);

export type Gate = { name: string; ok: boolean; detail: string };
export type FileChange = { path: string; added: number; removed: number };

export type FlowOutcome = {
    flow_id: string; final_status: string; category: string; root_cause: string; action: string;
    runs: string[];
};
export type Fix = {
    title: string; files: string[]; why: string;
    verified_runs: { flow_id: string; run_id: string }[]; unit_tests: string;
};
export type NewScenario = {
    flow_id: string; covers: string; status: 'kept' | 'dropped'; run_id: string; reason: string;
};
export type AppBug = { app: string; flow_id: string; summary: string; evidence: string; patch_file: string };

export type FeatureStatus = 'covered' | 'partial' | 'missing';
export type Feature = {
    feature: string; role: string; screens: string[]; status: FeatureStatus; risk: 'high' | 'medium' | 'low';
    covered_by: string[]; gap: string; scenario: string; ids: string[];
};
export type FeatureTotals = { covered: number; partial: number; missing: number };
export type FeatureMap = {
    generated: string; mode: string;
    apps: { consumer?: Feature[]; business?: Feature[] } & Record<string, Feature[] | undefined>;
    totals?: Record<string, FeatureTotals>;
    session_id?: string; finished_at?: string | null;
};

export type SessionSummary = {
    headline?: string; flows?: FlowOutcome[]; fixes?: Fix[]; new_scenarios?: NewScenario[];
    app_bugs?: AppBug[]; infra?: string[]; needs_human?: string[]; feature_map?: FeatureMap;
};
export type SessionResult = {
    subtype?: string; is_error?: boolean; turns?: number; duration_ms?: number;
    timed_out?: boolean; stopped?: boolean; api_equivalent_usd?: number;
};
export type SessionOptions = {
    run_ids?: string[]; flow_ids?: string[]; focus?: string; features?: string[]; mode?: string; max_new?: number;
};

export type Session = {
    id: string; kind: SessionKind; trigger: SessionTrigger; status: SessionStatus; phase: string | null;
    options: SessionOptions; created_by: string | null;
    created_at: string | null; started_at: string | null; finished_at: string | null;
    branch: string | null; base_commit: string | null; files_changed: FileChange[]; gates: Gate[];
    summary: SessionSummary; result: SessionResult; error: string | null;
    decision_by: string | null; decided_at: string | null; claude_session_id: string | null;
    stop_requested: boolean;
};

export type RunRow = {
    flow_id: string; label: string; run_id: string; resumed_from: string | null; status: string;
    category: string | null; failed_segment: string | null; failed_step: string | null; summary: string | null;
    started: string | null; finished: string | null; seconds: number | null;
};
export type SessionDetail = Session & { report: string; runs: RunRow[]; app_patches: { name: string; patch: string }[] };

export type AgentEvent = {
    i: number; kind: 'info' | 'text' | 'tool' | 'result' | 'end'; text: string;
    tool?: string; id?: string; error?: boolean; sub?: boolean;
};
export type EventsPage = { events: AgentEvent[]; last: number; status: SessionStatus; phase: string | null };

export type ClaudeStatus = { installed: boolean; version: string; logged_in: boolean; plan: string; auth: string };
export type ScheduleState = { installed: boolean; time?: string; loaded?: boolean; error?: string };

export type AgentSettings = {
    nightly_enabled: boolean; nightly_time: string; nightly_mode: 'auto' | 'delta' | 'full';
    auto_heal_after_run_all: boolean; env: string; include_demos: boolean;
    model: string; effort: string;
    max_new_scenarios: number; max_fix_attempts: number; max_changed_lines: number;
    hours: Record<string, number>;
    report_emails: string[]; report_slack: boolean; report_manual_sessions: boolean;
};

export type AgentStatus = {
    claude: ClaudeStatus; active: Session | null; settings: AgentSettings; schedule: ScheduleState;
    secrets: { smtp: boolean; slack: boolean; github: boolean };
    apps: Record<string, boolean>;
};

export type CoverageRoute = { route: string; file: string | null; covered: boolean; ids: number; covered_ids: string[] };
export type AppCoverage = {
    screens: number; screens_covered: number; ids: number; ids_covered: number; routes: CoverageRoute[];
};
export type Coverage = Record<string, AppCoverage>;

export type AgentFlow = { id: string; name: string; segments: number; demo: boolean; draft: boolean };
export type PreflightCheck = { name: string; ok: boolean; detail: string };

export type StartSession = {
    kind: Exclude<SessionKind, 'nightly'>; run_ids?: string[]; flow_ids?: string[]; focus?: string;
    features?: string[]; mode?: string; max_new?: number;
};
export type ApproveResult = { merged: boolean; into: string; branch: string; check_flows: string; restart_needed: boolean };

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await fetch(`${API_BASE}/agent${path}`, { headers: getHeaders(), ...init });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
        const d = (body as { detail?: unknown }).detail;
        throw new Error(typeof d === 'string' ? d : `Request failed (${res.status})`);
    }
    return body as T;
}

const post = <T>(path: string, data: unknown = {}) =>
    call<T>(path, { method: 'POST', body: JSON.stringify(data) });

const sid = (id: string) => `/sessions/${encodeURIComponent(id)}`;

export const agentApi = {
    status: () => call<AgentStatus>('/status'),
    saveSettings: (changes: Partial<AgentSettings>) =>
        call<{ settings: AgentSettings; schedule?: ScheduleState }>('/settings',
            { method: 'PUT', body: JSON.stringify({ changes }) }),
    flows: () => call<{ flows: AgentFlow[] }>('/flows'),
    preflight: (env: string) =>
        call<{ ok: boolean; checks: PreflightCheck[] }>(`/preflight?env=${encodeURIComponent(env)}`),
    inventory: () => call<Coverage>('/inventory'),
    featureMap: () => call<{ feature_map: FeatureMap | null }>('/feature-map'),

    sessions: (limit = 30) => call<{ sessions: Session[] }>(`/sessions?limit=${limit}`),
    session: (id: string) => call<SessionDetail>(sid(id)),
    events: (id: string, after = 0) => call<EventsPage>(`${sid(id)}/events?after=${after}`),
    diff: (id: string) => call<{ diff: string }>(`${sid(id)}/diff`),
    start: (body: StartSession) => post<{ started: boolean; id: string }>('/sessions', body),
    stop: (id: string) => post<{ stopping: boolean }>(`${sid(id)}/stop`),
    approve: (id: string) => post<ApproveResult>(`${sid(id)}/approve`),
    reject: (id: string) => post<{ rejected: boolean }>(`${sid(id)}/reject`),
    restartBackend: () => post<{ restarting: boolean }>('/restart-backend'),
};
