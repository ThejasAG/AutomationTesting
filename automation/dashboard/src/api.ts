// Where the backend lives. Hardcoding "localhost" meant a COWORKER opening the
// dashboard hit THEIR OWN machine, not this one — the page loaded and every call
// failed. Default to the host the page was served from, so http://<mac-ip>:5173
// just works on the LAN; override with VITE_API_BASE when the API is elsewhere.
export const API_BASE =
    (import.meta as { env?: Record<string, string> }).env?.VITE_API_BASE
    || `http://${window.location.hostname}:8000/api/v1`;

/** Expose base URL for components that build URLs manually (e.g. EventSource). */
export const getApiBase = () => API_BASE;

export const getAuthToken = () => localStorage.getItem('access_token');
export const setAuthToken = (token: string | null, refreshToken?: string | null) => {
    if (token) {
        localStorage.setItem('access_token', token);
        if (refreshToken) localStorage.setItem('refresh_token', refreshToken);
    } else {
        localStorage.removeItem('access_token');
        localStorage.removeItem('refresh_token');
        localStorage.removeItem('role');
    }
};

// ── Stay signed in until you actually sign out ───────────────────────────────
// The access token expires after 30 minutes. Login has always returned a refresh
// token too, but nothing stored or redeemed it — so the UI simply started 401-ing
// mid-task and threw you back to /login. Every API call goes through window.fetch,
// so ONE interceptor here covers every call site: on a 401 from our API, redeem the
// refresh token and replay the request. Only a failed refresh (or a real logout)
// ends the session.
const _fetch = window.fetch.bind(window);
let _refreshing: Promise<boolean> | null = null;

async function _tryRefresh(): Promise<boolean> {
    const rt = localStorage.getItem('refresh_token');
    if (!rt) return false;
    // Collapse concurrent 401s into ONE refresh — the dashboard fires several
    // requests at once, and parallel refreshes would race and invalidate each other.
    if (!_refreshing) {
        _refreshing = (async () => {
            try {
                const res = await _fetch(`${API_BASE}/auth/refresh`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ refresh_token: rt }),
                });
                if (!res.ok) return false;
                const d = await res.json();
                setAuthToken(d.access_token, d.refresh_token);
                return true;
            } catch {
                return false;
            } finally {
                setTimeout(() => { _refreshing = null; }, 0);
            }
        })();
    }
    return _refreshing;
}

window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const res = await _fetch(input as RequestInfo, init);
    const url = typeof input === 'string' ? input
        : input instanceof URL ? input.toString() : (input as Request).url;
    // Only our API, and never the auth endpoints themselves (a failed login is a
    // real 401 and must stay one).
    if (res.status !== 401 || !url.startsWith(API_BASE) || url.includes('/auth/')) return res;
    if (!(await _tryRefresh())) {
        // The refresh token is dead too — expired, revoked, or the server's JWT
        // signing key was rotated. Drop the session so the app falls back to the
        // login screen. Without this every call 401s silently, the dashboard
        // renders empty lists, and controls that gate on loaded data (the deploy
        // button gates on `apps.length`) stay disabled with no visible reason.
        if (getAuthToken()) {
            setAuthToken(null);
            window.location.reload();
        }
        return res;
    }
    const headers = new Headers(init?.headers ?? {});
    headers.set('Authorization', `Bearer ${getAuthToken()}`);
    return _fetch(url, { ...(init ?? {}), headers });
};

export async function getMe(): Promise<{ id: string; username: string; role: string }> {
    const res = await fetch(`${API_BASE}/auth/me`, { headers: getHeaders() });
    return await handleResponse(res);
}

export const getHeaders = (isJson = true) => {
    const token = getAuthToken();
    const headers: Record<string, string> = {};
    if (isJson) headers['Content-Type'] = 'application/json';
    if (token) headers['Authorization'] = `Bearer ${token}`;
    return headers;
};

const handleResponse = async (res: Response) => {
    if (res.status === 401) {
        setAuthToken(null);
        window.location.href = '/login';
        throw new Error('Unauthorized');
    }
    if (!res.ok) {
        // Try to surface the server's detail message for better error UX
        try {
            const body = await res.json();
            throw new Error(body.detail || `API Error: ${res.status}`);
        } catch (jsonErr) {
            if (jsonErr instanceof SyntaxError) throw new Error(`API Error: ${res.status}`);
            throw jsonErr;
        }
    }
    return await res.json();
};

export interface ScenarioResult {
    id: string;
    scenario_num: string;
    scenario_name: string;
    status: string;                 // PASS | FAIL
    consumer_status: string;        // PASS | FAIL | N/A
    business_status: string;        // PASS | FAIL | N/A
    error: string | null;
    reasons: string[];
    // Step-level accounting derived from `reasons` on the server. pass_pct is null
    // when no countable step ran — which is not the same as 0%.
    steps_passed: number;
    steps_failed: number;
    steps_total: number;
    steps_pass_pct: number | null;
    steps_summary: string;
    launch_time: number | null;
    screenshot?: string | null;     // failure screenshot (data-URI), shown in the expanded row
    created_at: string;
}

export interface ScenariosResponse {
    run_id: string;
    bot_type: string;               // ios | android
    total: number;
    passed: number;
    failed: number;
    consumer_passed: number;
    consumer_failed: number;
    business_passed: number;
    business_failed: number;
    rule: string;
    scenarios: ScenarioResult[];
}

export async function getRunScenarios(runId: string): Promise<ScenariosResponse> {
    const res = await fetch(`${API_BASE}/runs/${runId}/scenarios`, { headers: getHeaders() });
    return await handleResponse(res);
}

// Demo mode: the pinned known-green run to show if a live run blips.
export async function getGoldenRun(): Promise<{ run_id: string | null; report_url: string | null }> {
    const res = await fetch(`${API_BASE}/reports/golden-run`, { headers: getHeaders() });
    return await handleResponse(res);
}

/** `apps` = bundle ids installed on this simulator. null means UNKNOWN (the sim is shut
 *  down, and `simctl listapps` needs a booted one) — never treat null as "has nothing". */
export interface SimDevice { udid: string; name: string; state: string; ios: string; apps?: string[] | null; }
export interface CrossAppConfig {
    simulators: SimDevice[];
    devices: { consumer: string; waiter: string; kitchen: string };
    credentials: Record<'consumer' | 'waiter' | 'kitchen', { email: string; has_password: boolean }>;
}

export async function getCrossAppConfig(): Promise<CrossAppConfig> {
    const res = await fetch(`${API_BASE}/runs/cross-app/config`, { headers: getHeaders() });
    return await handleResponse(res);
}

export interface CrossAppRunBody {
    devices?: { consumer?: string; waiter?: string; kitchen?: string };
    credentials?: Record<string, { email?: string; password?: string }>;
    save?: boolean;
}

/** Run the full Consumer + Business scenario across the chosen iOS simulators. */
export async function runCrossAppSuite(
    body: CrossAppRunBody = {},
): Promise<{ started: boolean; run_id: string; message: string }> {
    const res = await fetch(`${API_BASE}/runs/cross-app`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(body),
    });
    return await handleResponse(res);
}

export interface FlowSegment { num: string; name: string; role: string; steps: string[]; }
export interface CrossAppFlow {
    id: string; name: string; description: string; segments: FlowSegment[];
    builtin?: boolean;      // ships in code
    edited?: boolean;       // a stored edit is overriding it
}

// ── Editing cross-app flows ──────────────────────────────────────────────────
export interface StepCatalogEntry { step: string; help: string; }
export async function getFlowStepCatalog(): Promise<{ catalog: Record<string, StepCatalogEntry[]>; roles: string[] }> {
    const res = await fetch(`${API_BASE}/runs/cross-app-flows/step-catalog`, { headers: getHeaders() });
    return await handleResponse(res);
}

/** Create or update a flow. Using a built-in id overrides it for future runs. */
export async function saveCrossAppFlow(
    flowId: string, body: { name: string; description: string; segments: FlowSegment[] },
): Promise<{ saved: boolean; flow_id: string; overrides_builtin: boolean }> {
    const res = await fetch(`${API_BASE}/runs/cross-app-flows/${encodeURIComponent(flowId)}`, {
        method: 'PUT', headers: getHeaders(), body: JSON.stringify(body),
    });
    return await handleResponse(res);
}

/** Delete a custom flow, or revert an edited built-in to its shipped definition. */
export async function deleteCrossAppFlow(flowId: string): Promise<void> {
    const res = await fetch(`${API_BASE}/runs/cross-app-flows/${encodeURIComponent(flowId)}`, {
        method: 'DELETE', headers: getHeaders(),
    });
    if (!res.ok && res.status !== 204) await handleResponse(res);
}

/** The four major end-to-end cross-app flows (consumer ↔ waiter ↔ kitchen). */
export async function listCrossAppFlows(): Promise<{ flows: CrossAppFlow[] }> {
    const res = await fetch(`${API_BASE}/runs/cross-app-flows`, { headers: getHeaders() });
    return await handleResponse(res);
}

export type FlowEnv = 'staging' | 'prod';
/** Which device runs the B-app roles (waiter + kitchen). */
export type BusinessDevice = 'tablet' | 'phone';

/** Run one cross-app flow against an environment; returns the run_id. */
export async function runCrossAppFlow(
    flow_id: string, env: FlowEnv = 'prod', business_device: BusinessDevice = 'tablet',
): Promise<{ started: boolean; run_id: string; flow_id: string; env: string; message: string }> {
    const res = await fetch(`${API_BASE}/runs/cross-app-flow`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ flow_id, env, business_device }),
    });
    return await handleResponse(res);
}

/** Run ALL cross-app flows sequentially against an environment. */
export async function runAllCrossAppFlows(
    env: FlowEnv = 'prod',
): Promise<{ started: boolean; env: string; flows: string[]; message: string }> {
    const res = await fetch(`${API_BASE}/runs/cross-app-flows/run-all`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ env }),
    });
    return await handleResponse(res);
}

export interface TestRun {
    id: string;
    project_id?: string;
    test_suite: string;
    test_name: string;
    /** ios = Appium/XCUITest, android = Vya-agentic-BOT cross-app. */
    bot_type?: string;
    status: string;
    started_at: string;
    completed_at: string | null;
    duration_ms: number;
    device_name?: string;
    os_version?: string;
    platform?: string;
    error_message: string | null;
    created_at: string;
    triggered_by?: string;
    branch?: string;
    commit_sha?: string;
    build_number?: string;
    timeline?: string;
    /** Distributed execution state (queued | downloading | preparing | running | collecting_evidence | completed | failed | cancelled) */
    job_state?: string;
    /** How many attempts the run took (flaky auto-retry). */
    attempts?: number;
    /** True when the run only passed after a retry. */
    flaky_detected?: boolean;
    is_flaky?: boolean;
    risk_score?: number;
    /** True when a visual regression was found on an otherwise-passing run. */
    visual_warning?: boolean;
    /** True when the app crashed / red-boxed during the run (app bug, not automation). */
    crash_detected?: boolean;
}

export interface VisualRegressionItem {
    id: string;
    screen_name: string;
    diff_percentage: number;
    severity: string;   // high | medium | low | none
    passed: boolean;
    baseline_image: string | null;
    current_image: string | null;
    diff_image: string | null;
    created_at: string;
}

export interface RiskPrediction {
    test: string;
    test_name: string;
    risk_score: number;
    risk_level: string;  // HIGH | MEDIUM | LOW
    factors: Record<string, number>;
}

export interface RCAReport {
    run_id: string;
    root_cause: string;
    failure_category: string;
    /** Null on RCA rows written before the affected_modules column existed. */
    affected_modules: string[] | null;
    confidence: number;
    possible_reason: string;
    impact: string;
    suggested_fix: string;
    priority: string;
    severity: string;
    responsible_module: string;
    summary: string;
    llm_provider: string;
    llm_model: string;
    generated_at: string;
}

export interface Evidence {
    git_commit: string;
    git_author: string;
    git_branch: string;
    git_diff: string;
    changed_files: string[];
    appium_logs: any[];
    xml_page_source: string;
    screenshot_path: string;
}

export interface Trends {
    flaky_tests: { test_name: string; fail_count: number }[];
    common_root_causes: { failure_category: string; count: number }[];
    failure_rate: number;
    total_executions: number;
    failed_executions: number;
}

export async function getRuns(): Promise<TestRun[]> {
    const res = await fetch(`${API_BASE}/runs`, { headers: getHeaders() });
    const data = await handleResponse(res);
    return data.runs;
}

export async function getRun(id: string): Promise<TestRun> {
    const res = await fetch(`${API_BASE}/runs/${id}`, { headers: getHeaders() });
    const data = await handleResponse(res);
    return data.run;
}

export async function getRCA(id: string): Promise<RCAReport | null> {
    const res = await fetch(`${API_BASE}/runs/${id}/rca`, { headers: getHeaders() });
    if (res.status === 404) return null;
    const data = await handleResponse(res);
    return data.rca;
}

/** Generate an AI root-cause report for a failed run (Ollama). Returns the RCA. */
export async function triggerAnalysis(id: string): Promise<RCAReport | null> {
    const res = await fetch(`${API_BASE}/runs/${id}/analyze`, {
        method: 'POST',
        headers: getHeaders(),
    });
    const data = await handleResponse(res);
    return data.rca ?? null;
}

export async function getEvidence(id: string): Promise<Evidence | null> {
    const res = await fetch(`${API_BASE}/runs/${id}/evidence`, { headers: getHeaders() });
    if (res.status === 404) return null;
    const data = await handleResponse(res);
    return data.evidence;
}

export async function getTrends(): Promise<Trends> {
    const res = await fetch(`${API_BASE}/trends`, { headers: getHeaders() });
    return await handleResponse(res);
}

// ── Visual regression ────────────────────────────────────────────────────────
export async function getVisualRegression(runId: string): Promise<{
    run_id: string; total: number; regressions: number; results: VisualRegressionItem[];
}> {
    const res = await fetch(`${API_BASE}/runs/${runId}/visual-regression`, { headers: getHeaders() });
    return await handleResponse(res);
}

export async function updateVisualBaseline(runId: string): Promise<{ updated: boolean }> {
    const res = await fetch(`${API_BASE}/runs/${runId}/visual-regression/update-baseline`, {
        method: 'POST', headers: getHeaders(), body: JSON.stringify({}),
    });
    return await handleResponse(res);
}

// ── Test impact prediction ───────────────────────────────────────────────────
export async function getRiskPredictions(runId: string): Promise<{
    run_id: string; project_id: string; predictions: RiskPrediction[];
}> {
    const res = await fetch(`${API_BASE}/runs/${runId}/risk-predictions`, { headers: getHeaders() });
    return await handleResponse(res);
}

// ── AI summaries ─────────────────────────────────────────────────────────────
export async function getRunSummary(runId: string): Promise<{ summary: string; cached: boolean }> {
    const res = await fetch(`${API_BASE}/runs/${runId}/summary`, { headers: getHeaders() });
    return await handleResponse(res);
}

export async function getTrendSummary(projectId: string, days = 7): Promise<{
    summary: string; total: number; passed?: number; failed?: number; flaky?: number; pass_rate?: number;
}> {
    const res = await fetch(`${API_BASE}/projects/${projectId}/trend-summary?days=${days}`, { headers: getHeaders() });
    return await handleResponse(res);
}

// ── Performance ──────────────────────────────────────────────────────────────
export interface PerformanceSummary {
    app_launch_time_s: number | null;
    avg_cpu_percent: number; peak_cpu_percent: number;
    avg_memory_mb: number; peak_memory_mb: number;
    avg_fps: number | null; min_fps: number | null; dropped_frames: number;
    api_calls: number; avg_api_response_ms: number | null;
    slowest_api_ms: number | null; slowest_api_endpoint: string | null;
    performance_score: number | null; grade: string | null;
}
export interface PerformanceResponse {
    summary: PerformanceSummary;
    grade: string | null;
    score: number | null;
    issues: string[];
    metrics_over_time: { timestamp: string; cpu: number | null; memory: number | null; fps: number | null }[];
    api_calls: { total_calls: number; avg_ms: number | null; slowest: { url: string | null; ms: number | null } };
    comparison: { vs_previous_run: {
        prev_run_id: string; prev_score: number | null;
        launch_time_change: number | null; cpu_change: number | null;
        score_change: number | null; better: boolean;
    } } | null;
}
export async function getPerformance(runId: string): Promise<PerformanceResponse | null> {
    const res = await fetch(`${API_BASE}/runs/${runId}/performance`, { headers: getHeaders() });
    if (res.status === 404) return null;
    return await handleResponse(res);
}

export interface Device {
    id: string;
    name: string;
    manufacturer: string;
    model: string;
    platform: string;
    platform_version: string;
    sdk_version: string;
    serial: string | null;
    status: string;
    connection_type: string;
    battery_percentage: number | null;
    charging: boolean | null;
    screen_resolution: string;
    screen_density: string;
    current_activity: string | null;
    appium_ready: boolean;
    last_seen: string | null;
    provider: string;
}

export interface DeviceResponse {
    count: number;
    devices: Device[];
    warning: string | null;
}

export async function getDevices(): Promise<DeviceResponse> {
    const res = await fetch(`${API_BASE}/automation/devices`, { headers: getHeaders() });
    const data = await handleResponse(res);
    return data;
}

export interface LastRun {
    id: string;
    status: string;
    job_state: string | null;
    created_at: string | null;
    duration_ms: number | null;
}

export interface ProjectHealth {
    repository_exists: boolean;
    yaml_valid: boolean;
    venv_exists: boolean;
    dependencies_installed: boolean;
    /** Toolchain-aware: node_modules for RN, venv for Python, build-time for native. */
    dependencies_ok: boolean;
    automation_yaml: boolean;
    agent_status: 'online' | 'offline' | 'unknown';
    last_run: LastRun | null;
}

/** Repository lifecycle state, mirrors TestProject.clone_status on the backend. */
export type CloneStatus =
    | 'not_cloned'
    | 'syncing'
    | 'cloned'
    | 'ready'
    | 'outdated'
    | 'clone_failed';

export type ProjectPlatform = 'ios' | 'android';
export type RepoType = 'github' | 'gitlab' | 'local';

export interface Project {
    id: string;
    /** Null when the project does not belong to an application group. */
    group_id: string | null;
    group_name: string | null;
    name: string;
    description: string;
    git_url: string;
    default_branch: string;
    status: string;
    platform: ProjectPlatform;
    repo_type: RepoType;
    project_type: string;
    project_type_label: string;
    clone_status: CloneStatus;
    clone_error: string | null;
    /** Detected from the built app's Info.plist. Null until the project is built. */
    app_bundle_id: string | null;
    local_path: string;
    current_branch: string | null;
    has_automation_yaml: boolean;
    last_pull_at: string | null;
    last_execution_at: string | null;
    health: ProjectHealth;
}

export interface ProjectInput {
    name: string;
    description?: string;
    git_url: string;
    default_branch?: string;
    platform?: ProjectPlatform;
    repo_type?: RepoType;
    group_id?: string | null;
}

// ── Application Groups ───────────────────────────────────────────────────────

export interface GroupProject {
    id: string;
    name: string;
    platform: string;
    project_type: string;
    clone_status: CloneStatus;
}

export interface ApplicationGroup {
    id: string;
    name: string;
    description: string;
    created_at: string | null;
    project_count: number;
    projects: GroupProject[];
}

export async function getGroups(): Promise<ApplicationGroup[]> {
    const res = await fetch(`${API_BASE}/groups/`, { headers: getHeaders() });
    const data = await handleResponse(res);
    return data.groups;
}

export async function addGroup(data: { name: string; description?: string }): Promise<ApplicationGroup> {
    const res = await fetch(`${API_BASE}/groups/`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(data),
    });
    const body = await handleResponse(res);
    return body.group;
}

export async function updateGroup(id: string, data: { name?: string; description?: string }): Promise<ApplicationGroup> {
    const res = await fetch(`${API_BASE}/groups/${id}`, {
        method: 'PUT',
        headers: getHeaders(),
        body: JSON.stringify(data),
    });
    const body = await handleResponse(res);
    return body.group;
}

/** Deletes the group. Member projects are detached, never deleted. */
export async function deleteGroup(id: string): Promise<void> {
    const res = await fetch(`${API_BASE}/groups/${id}`, {
        method: 'DELETE',
        headers: getHeaders(),
    });
    await handleResponse(res);
}

/** Sibling apps whose suites should also run when one app in the group changes. */
export async function getGroupImpactScope(id: string): Promise<any> {
    const res = await fetch(`${API_BASE}/groups/${id}/impact-scope`, { headers: getHeaders() });
    return handleResponse(res);
}

// ── Cross-App Dependency Analysis ────────────────────────────────────────────

export type AppRole = 'consumer' | 'business' | 'superadmin' | 'api';

export interface AppGroupMember {
    id: string;
    project_id: string;
    app_role: AppRole;
    project_name: string | null;
    platform: string | null;
    cloned: boolean;
}

export interface AppGroup {
    id: string;
    name: string;
    description: string;
    created_at: string | null;
    members: AppGroupMember[];
    member_count: number;
    last_scanned_at: string | null;
    has_graph: boolean;
}

export interface GraphNode {
    id: string;
    type: 'app' | 'endpoint';
    label: string;
    app_role?: AppRole;
    app_type?: string;
    test_suite?: string;
    project_id?: string;
    used_by?: string[];
    shared?: boolean;
}

export interface GraphLink {
    source: string | GraphNode;
    target: string | GraphNode;
    call_sites: string[];
    value: number;
}

export interface DependencyGraphData {
    nodes: GraphNode[];
    links: GraphLink[];
    shared_endpoints: string[];
    stats: { apps?: number; endpoints?: number; shared_endpoints?: number; edges?: number };
    scanned_at: string | null;
}

export interface CrossAppImpactItem {
    app: string;
    reason: string;
    endpoint: string;
    tests: string[];
    test_suite: string | null;
    call_sites: string[];
}

export interface CrossAppImpact {
    changed_files: string[];
    directly_affected_app: string | null;
    directly_affected_apps: string[];
    affected_endpoints: string[];
    cross_app_impact: CrossAppImpactItem[];
    total_apps_affected: number;
    run_all_test_suites: string[];
}

export async function getAppGroups(): Promise<AppGroup[]> {
    const res = await fetch(`${API_BASE}/dependency/groups`, { headers: getHeaders() });
    const data = await handleResponse(res);
    return data.groups;
}

// Groups are created and deleted via createGroup/deleteGroup (the /groups
// endpoints the Projects page uses). The dependency API only reads them.

/** Scans every cloned app in the group and rebuilds the dependency graph. */
export async function scanDependencies(id: string): Promise<any> {
    const res = await fetch(`${API_BASE}/dependency/groups/${id}/scan`, {
        method: 'POST',
        headers: getHeaders(),
    });
    return handleResponse(res);
}

export async function getDependencyGraph(id: string): Promise<DependencyGraphData> {
    const res = await fetch(`${API_BASE}/dependency/groups/${id}/graph`, {
        headers: getHeaders(),
    });
    return handleResponse(res);
}

/** Intra-app FILE dependency graph (Graphify) for one project — same shape,
 *  rendered by the same force layout. */
export async function getModuleGraph(projectId: string): Promise<DependencyGraphData> {
    const res = await fetch(`${API_BASE}/dependency/projects/${projectId}/module-graph`, {
        headers: getHeaders(),
    });
    return handleResponse(res);
}

export async function analyzeCrossAppImpact(
    id: string,
    changed_files: string[],
): Promise<CrossAppImpact> {
    const res = await fetch(`${API_BASE}/dependency/groups/${id}/impact`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ changed_files }),
    });
    return handleResponse(res);
}

// ── Hybrid impact (Graphify blast radius + endpoint graph) ───────────────────

export interface HybridCrossAppItem {
    app: string;
    reason: string;
    endpoints: string[];
    affected_files: string[];
    test_suite: string | null;
    project_id: string | null;
    app_role: string | null;
}

export interface QueuedRun {
    app: string;
    run_id: string | null;
    test_suite?: string | null;
    error?: string;
}

export interface HybridImpact {
    changed_files: string[];
    primary_app: string | null;
    primary_app_project_id: string | null;
    /** Files in the SAME app that depend on the change (from Graphify's AST walk). */
    intra_app_blast_radius: string[];
    affected_endpoints: string[];
    cross_app_impact: HybridCrossAppItem[];
    all_test_suites_to_run: string[];
    total_apps_affected: number;
    graphify_nodes_affected: number;
    graphify_available: boolean;
    group_id?: string;
    group_name?: string;
    skipped_not_cloned?: string[];
    queued_runs?: QueuedRun[];
    error?: string;
}

export interface HybridGraphNode {
    id: string;
    type: 'app' | 'endpoint';
    color: string;
    app_role?: string;
    test_suite?: string;
    project_id?: string;
    used_by?: string[];
    shared?: boolean;
    connections: number;
}

export interface HybridGraphEdge {
    source: string | HybridGraphNode;
    target: string | HybridGraphNode;
    label: string;
}

/** Hybrid impact: Graphify intra-app blast radius + cross-app endpoint coupling. */
export async function analyzeHybridImpact(
    group_id: string,
    changed_files: string[],
    opts: { create_runs?: boolean; device_id?: string; depth?: number } = {},
): Promise<HybridImpact> {
    const res = await fetch(`${API_BASE}/dependency/analyze`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({
            group_id,
            changed_files,
            create_runs: opts.create_runs ?? false,
            device_id: opts.device_id ?? null,
            depth: opts.depth ?? 2,
        }),
    });
    return handleResponse(res);
}

export interface ValidationIssue {
    check: string;
    problem: string;
    cause?: string;
    impact?: string;
    resolution: string;
}

export interface PreparationResult {
    ok: boolean;
    project_type: string;
    branch: string | null;
    clone_status: CloneStatus;
    steps: string[];
    error: string | null;
    /** True when automation.yaml is missing — offer to generate it. */
    needs_automation_yaml: boolean;
    validation: {
        passed: boolean;
        issues: ValidationIssue[];
        warnings: ValidationIssue[];
    } | null;
}

export async function getProjects(): Promise<Project[]> {
    const res = await fetch(`${API_BASE}/projects/`, { headers: getHeaders() });
    const data = await handleResponse(res);
    return data.projects;
}

// ── Saved Scenarios (custom step builder) ────────────────────────────────────
export interface SavedScenario {
    id: string;
    name: string;
    description: string | null;
    project_id: string | null;
    bundle_id: string | null;
    device_id: string | null;
    steps: string[];
    covers: string[];
    created_at: string | null;
    updated_at: string | null;
}
export interface ScenarioInput {
    name: string;
    description?: string | null;
    project_id?: string | null;
    bundle_id?: string | null;
    device_id?: string | null;
    steps: string[];
    covers: string[];
}

export async function getScenarios(): Promise<SavedScenario[]> {
    const res = await fetch(`${API_BASE}/scenarios`, { headers: getHeaders() });
    return await handleResponse(res);
}

// ── Tickets (paste issue → link PR + scenarios → auto-test on PR push) ────────
export interface Ticket {
    id: string;
    title: string;
    description: string;
    project_id: string | null;
    pr_number: string | null;
    pr_url: string | null;
    scenario_ids: string[];
    ticket_type: string;   // ui | calc | data | crash | mixed
    status: string;        // untested | running | passed | failed
    last_run_id: string | null;
    created_at: string | null;
    updated_at: string | null;
}
export interface TicketInput {
    title: string;
    description?: string;
    project_id?: string | null;
    pr_number?: string | null;
    pr_url?: string | null;
    scenario_ids?: string[];
    ticket_type?: string;
    setup_scenario_id?: string | null;
    match_key?: string | null;
}
export async function getTickets(): Promise<Ticket[]> {
    const res = await fetch(`${API_BASE}/tickets`, { headers: getHeaders() });
    const data = await handleResponse(res);
    return data.tickets;
}
export async function createTicket(data: TicketInput): Promise<Ticket> {
    const res = await fetch(`${API_BASE}/tickets`, {
        method: 'POST', headers: getHeaders(), body: JSON.stringify(data),
    });
    return await handleResponse(res);
}
export async function updateTicket(id: string, data: TicketInput): Promise<Ticket> {
    const res = await fetch(`${API_BASE}/tickets/${id}`, {
        method: 'PUT', headers: getHeaders(), body: JSON.stringify(data),
    });
    return await handleResponse(res);
}
export async function deleteTicket(id: string): Promise<void> {
    await fetch(`${API_BASE}/tickets/${id}`, { method: 'DELETE', headers: getHeaders() });
}
export async function runTicket(id: string): Promise<{ started: boolean; scenarios: number }> {
    const res = await fetch(`${API_BASE}/tickets/${id}/run`, { method: 'POST', headers: getHeaders() });
    return await handleResponse(res);
}
// ── Workflow / coverage ──────────────────────────────────────────────────────
export interface CoverageScenario { name: string; status: string; built: boolean }
export interface CoverageCategory { category: string; blocked: boolean; scenarios: CoverageScenario[] }
export interface Coverage {
    summary: { total: number; automatable: number; manual_blocked: number; categories: number };
    categories: CoverageCategory[];
}
export interface SpineNode { id: string; label: string; app: string; order: number; status: string; scenario: string | null }
/** The spine as an Archify artifact: self-contained interactive HTML.
 *  `ok:false` when node/archify is unavailable — the page keeps its existing lanes. */
export interface WorkflowDiagram { ok: boolean; html?: string; reason?: string }

export interface WorkflowDeltaCounts {
    added: number; removed: number; changed: number;
    moved?: number; rerouted?: number; evidenceChanged?: number; geometryChanged?: number;
}
export interface WorkflowDelta {
    ok: boolean;
    base_ref?: string;
    head_ref?: string;
    reason?: string;
    html?: string;
    summary?: {
        components: WorkflowDeltaCounts;
        connections: WorkflowDeltaCounts;
        boundaries: WorkflowDeltaCounts;
        presentationChanged?: boolean;
        provenanceChanged?: boolean;
    };
}

export async function getWorkflowDiagram(): Promise<WorkflowDiagram> {
    const res = await fetch(`${API_BASE}/workflow/diagram`, { headers: getHeaders() });
    return await handleResponse(res);
}

/** Before / Delta / After for the spine.
 *  `headRef` empty compares against the WORKING TREE (what you want while editing);
 *  a PR passes its head sha, because the working tree is not the PR. */
/** Platform refs the flow delta can be built from — i.e. refs whose tree contains
 *  automation/workflow/catalog.py. Branches of the apps under test are a different
 *  repository and are deliberately not offered. */
export async function getWorkflowRefs(): Promise<{ refs: string[]; default: string }> {
    const res = await fetch(`${API_BASE}/workflow/diagram/refs`, { headers: getHeaders() });
    return await handleResponse(res);
}

export async function getWorkflowDelta(baseRef = '', headRef = ''): Promise<WorkflowDelta> {
    const qs = new URLSearchParams({ base_ref: baseRef });
    if (headRef) qs.set('head_ref', headRef);
    const res = await fetch(`${API_BASE}/workflow/diagram/delta?${qs}`, { headers: getHeaders() });
    return await handleResponse(res);
}

export async function getWorkflowCoverage(): Promise<Coverage> {
    const res = await fetch(`${API_BASE}/workflow/coverage`, { headers: getHeaders() });
    return await handleResponse(res);
}
export interface Handoff { source: string; target: string; label: string }
export interface GraphEdge { source: string; target: string; kind: string; label?: string }
export async function getWorkflowGraph(): Promise<{ consumer: SpineNode[]; business: SpineNode[]; branches: SpineNode[]; nodes: SpineNode[]; handoffs: Handoff[]; graph_edges: GraphEdge[]; summary: Coverage['summary'] }> {
    const res = await fetch(`${API_BASE}/workflow/graph`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function getWorkflowPath(goal: string): Promise<{ goal: string; goal_label: string; path: { id: string; label: string }[]; scenarios: string[] }> {
    const res = await fetch(`${API_BASE}/workflow/path?goal=${encodeURIComponent(goal)}`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function recreateWorkflow(goal: string): Promise<{ started: boolean; composed_from_workflow?: string[]; message?: string; error?: string }> {
    const res = await fetch(`${API_BASE}/workflow/recreate?goal=${encodeURIComponent(goal)}`, { method: 'POST', headers: getHeaders() });
    return await handleResponse(res);
}

export interface DraftScenario { name: string; steps: string[]; unverified?: string[] }
export async function draftTicketScenarios(title: string, description: string, projectId = ''): Promise<{ type: string; scenarios: DraftScenario[]; count: number }> {
    const res = await fetch(`${API_BASE}/tickets/draft`, {
        method: 'POST', headers: getHeaders(), body: JSON.stringify({ title, description, project_id: projectId }),
    });
    return await handleResponse(res);
}
export async function createScenario(data: ScenarioInput): Promise<SavedScenario> {
    const res = await fetch(`${API_BASE}/scenarios`, {
        method: 'POST', headers: getHeaders(), body: JSON.stringify(data),
    });
    return await handleResponse(res);
}
export async function updateScenario(id: string, data: ScenarioInput): Promise<SavedScenario> {
    const res = await fetch(`${API_BASE}/scenarios/${id}`, {
        method: 'PUT', headers: getHeaders(), body: JSON.stringify(data),
    });
    return await handleResponse(res);
}
export async function deleteScenario(id: string): Promise<void> {
    const res = await fetch(`${API_BASE}/scenarios/${id}`, {
        method: 'DELETE', headers: getHeaders(),
    });
    if (!res.ok && res.status !== 204) await handleResponse(res);
}
export async function getScenarioDevices(): Promise<{ simulators: SimDevice[] }> {
    const res = await fetch(`${API_BASE}/scenarios/devices`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function suggestCovers(body: { project_id: string; name: string; steps: string[] }): Promise<{ covers: string[]; available: string[] }> {
    const res = await fetch(`${API_BASE}/scenarios/suggest-covers`, { method: 'POST', headers: getHeaders(), body: JSON.stringify(body) });
    return await handleResponse(res);
}

export interface EngineStatus {
    appium: { healthy: boolean; url: string; note: string };
    booted_simulators: string[];
    ready: boolean;
}
export async function getEngineStatus(): Promise<EngineStatus> {
    const res = await fetch(`${API_BASE}/automation/engine-status`, { headers: getHeaders() });
    return await handleResponse(res);
}

// ── Live scenario recorder ───────────────────────────────────────────────────
export interface RecorderFrame { image: string; width: number; height: number; steps: string[]; }
export async function recorderStart(body: { project_id: string; device_id: string; bundle_id?: string | null })
    : Promise<{ session_id: string; width: number; height: number }> {
    const res = await fetch(`${API_BASE}/recorder/start`, { method: 'POST', headers: getHeaders(), body: JSON.stringify(body) });
    return await handleResponse(res);
}
export async function recorderFrame(sid: string): Promise<RecorderFrame> {
    const res = await fetch(`${API_BASE}/recorder/${sid}/frame`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function recorderTap(sid: string, x: number, y: number): Promise<{ step: string; steps: string[]; image: string | null }> {
    const res = await fetch(`${API_BASE}/recorder/${sid}/tap`, { method: 'POST', headers: getHeaders(), body: JSON.stringify({ x, y }) });
    return await handleResponse(res);
}
export async function recorderSetSteps(sid: string, steps: string[]): Promise<{ steps: string[] }> {
    const res = await fetch(`${API_BASE}/recorder/${sid}/steps`, { method: 'PUT', headers: getHeaders(), body: JSON.stringify({ steps }) });
    return await handleResponse(res);
}
export async function recorderSave(sid: string, name: string, description?: string): Promise<SavedScenario> {
    const res = await fetch(`${API_BASE}/recorder/${sid}/save`, { method: 'POST', headers: getHeaders(), body: JSON.stringify({ name, description }) });
    return await handleResponse(res);
}
export async function recorderStop(sid: string): Promise<void> {
    await fetch(`${API_BASE}/recorder/${sid}/stop`, { method: 'POST', headers: getHeaders() });
}

// ── Test Reports ─────────────────────────────────────────────────────────────
export interface ReportRow {
    id: string; test_name: string; test_suite: string; status: string;
    verdict: 'passed' | 'failed' | 'no-tests' | string;
    device_name: string; platform: string; branch: string | null; commit_sha: string | null;
    triggered_by: string | null; duration_ms: number | null; created_at: string | null;
    scenarios_total: number; scenarios_passed: number; has_report: boolean;
}
export interface ReportScenario {
    scenario_num: string | null; scenario_name: string | null; status: string;
    consumer_status: string | null; business_status: string | null; error: string | null; role: string | null;
}
export interface FullReport extends ReportRow {
    error_message: string | null; report_summary: string | null; report_generated_at: string | null;
    scenarios: ReportScenario[];
    rca: { root_cause: string | null; suggested_fix: string | null; summary: string | null; llm_provider: string | null } | null;
}
export async function getReports(): Promise<ReportRow[]> {
    const res = await fetch(`${API_BASE}/reports`, { headers: getHeaders() });
    return await handleResponse(res);
}

export interface ReportTrends {
    days: number; total_runs: number; pass_rate: number; passed: number; failed: number;
    daily: { date: string; passed: number; failed: number }[];
    by_environment: { environment: string; passed: number; failed: number; pass_rate: number }[];
    flaky: { test_name: string; runs: number; passed: number; failed: number }[];
}
export async function getReportTrends(days = 30): Promise<ReportTrends> {
    const res = await fetch(`${API_BASE}/reports/trends?days=${days}`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function getReport(runId: string): Promise<FullReport> {
    const res = await fetch(`${API_BASE}/reports/${runId}`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function generateReport(runId: string): Promise<{ report_summary: string; report_generated_at: string }> {
    const res = await fetch(`${API_BASE}/reports/${runId}/generate`, { method: 'POST', headers: getHeaders() });
    return await handleResponse(res);
}
export async function getReportConfig(): Promise<{ slack: boolean; jira: boolean }> {
    const res = await fetch(`${API_BASE}/reports/config`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function fileJira(runId: string): Promise<{ key: string; url: string }> {
    const res = await fetch(`${API_BASE}/reports/${runId}/jira`, { method: 'POST', headers: getHeaders() });
    return await handleResponse(res);
}
// runScenarioStream (SSE) is defined below and reused by the Scenarios tab.

export async function addProject(data: ProjectInput): Promise<Project> {
    const res = await fetch(`${API_BASE}/projects/`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(data)
    });
    const body = await handleResponse(res);
    return body.project;
}

export async function updateProject(id: string, data: Partial<ProjectInput>): Promise<Project> {
    const res = await fetch(`${API_BASE}/projects/${id}`, {
        method: 'PUT',
        headers: getHeaders(),
        body: JSON.stringify(data)
    });
    const body = await handleResponse(res);
    return body.project;
}

/** Deletes the project. Pass deleteLocal to also remove the cloned repo from disk. */
export async function deleteProject(id: string, deleteLocal: boolean): Promise<void> {
    const res = await fetch(
        `${API_BASE}/projects/${id}?delete_local=${deleteLocal}`,
        { method: 'DELETE', headers: getHeaders() },
    );
    await handleResponse(res);
}

/** Clone the repository. force=true performs a full re-clone. */
export interface BranchList {
    branches: string[];
    default: string;
    current: string | null;
}

/** Branches on the project's remote, default first. Reads the remote, so it
 *  answers before the project has ever been cloned. */
export async function getProjectBranches(id: string): Promise<BranchList> {
    const res = await fetch(`${API_BASE}/projects/${id}/branches`, { headers: getHeaders() });
    return handleResponse(res);
}

/** Branches for a repo URL — for the Register form, where no project id exists yet. */
export async function getBranchesForUrl(gitUrl: string): Promise<{ branches: string[]; default: string }> {
    const res = await fetch(`${API_BASE}/projects/branches-for-url?git_url=${encodeURIComponent(gitUrl)}`,
        { headers: getHeaders() });
    return handleResponse(res);
}

export async function cloneProject(id: string, force = false, branch?: string): Promise<Project> {
    const qs = new URLSearchParams({ force: String(force) });
    if (branch) qs.set('branch', branch);
    const res = await fetch(`${API_BASE}/projects/${id}/clone?${qs}`, {
        method: 'POST',
        headers: getHeaders(),
    });
    const body = await handleResponse(res);
    return body.project;
}

export async function pullProject(id: string): Promise<Project> {
    const res = await fetch(`${API_BASE}/projects/${id}/pull`, {
        method: 'POST',
        headers: getHeaders(),
    });
    const body = await handleResponse(res);
    return body.project;
}

export async function getProjectStatus(id: string, checkRemote = false): Promise<any> {
    const res = await fetch(
        `${API_BASE}/projects/${id}/status?check_remote=${checkRemote}`,
        { headers: getHeaders() },
    );
    return handleResponse(res);
}

/** Runs clone/pull → checkout → detect → validate → install. */
export async function validateProject(
    id: string,
    opts: { device_id?: string; generate_yaml?: boolean } = {},
): Promise<PreparationResult> {
    const res = await fetch(`${API_BASE}/projects/${id}/validate`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({
            device_id: opts.device_id ?? null,
            generate_yaml: opts.generate_yaml ?? false,
        }),
    });
    return handleResponse(res);
}

export interface PreparationTask {
    task_id?: string;
    project_id?: string;
    status: 'idle' | 'running' | 'completed' | 'failed';
    steps: string[];
    result: PreparationResult | null;
    /** Progress through the pipeline's stages. NOT a time estimate: a cold
     *  install or an Xcode build can take seconds or twenty minutes, so any ETA
     *  would be invented. percent is the share of STAGES completed. */
    phase?: number;
    phase_count?: number;
    phase_label?: string;
    percent?: number;
    elapsed_seconds?: number;
    phase_elapsed_seconds?: number;
    /** True while in a stage that routinely takes minutes, so the UI can say
     *  so rather than look stalled. */
    phase_is_long?: boolean;
}

/** Starts the full pipeline (clone→build→install app) in the background. */
export async function startPreparation(
    id: string,
    opts: { device_id?: string; generate_yaml?: boolean; branch?: string } = {},
): Promise<PreparationTask> {
    const res = await fetch(`${API_BASE}/projects/${id}/prepare`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({
            device_id: opts.device_id ?? null,
            generate_yaml: opts.generate_yaml ?? false,
            branch: opts.branch ?? null,
        }),
    });
    return handleResponse(res);
}

export async function getPreparationStatus(id: string): Promise<PreparationTask> {
    const res = await fetch(`${API_BASE}/projects/${id}/prepare/status`, {
        headers: getHeaders(),
    });
    return handleResponse(res);
}

/** Scaffold an automation.yaml from the detected project type. */
export async function generateAutomationYaml(id: string): Promise<{ project_type: string; content: string }> {
    const res = await fetch(`${API_BASE}/projects/${id}/generate-yaml`, {
        method: 'POST',
        headers: getHeaders(),
    });
    return handleResponse(res);
}

export async function startRun(projectId: string, deviceId: string): Promise<string> {
    const res = await fetch(`${API_BASE}/automation/run`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ project_id: projectId, device_id: deviceId })
    });
    const data = await handleResponse(res);
    return data.run_id;
}

export async function getIntelligenceMetrics(): Promise<any> {
    const res = await fetch(`${API_BASE}/intelligence/metrics`, { headers: getHeaders() });
    return await handleResponse(res);
}

export async function getRecommendations(projectId: string, branch: string = "main"): Promise<any> {
    const res = await fetch(`${API_BASE}/intelligence/recommend`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ project_id: projectId, branch })
    });
    return await handleResponse(res);
}

export async function askChat(query: string, context?: any): Promise<any> {
    const res = await fetch(`${API_BASE}/intelligence/chat`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ query, context })
    });
    return await handleResponse(res);
}

export interface PullRequest {
    number: number;
    title: string;
    author: string;
    branch: string;
    base: string;
    commit_sha: string;
    draft: boolean;
    state: 'open' | 'merged' | 'closed';
    merged_at: string | null;
    updated_at: string;
    url: string;
    // Linked ticket ("Jira" description) — from a local ticket or fetched live from Jira.
    ticket_key?: string | null;
    ticket_title?: string | null;
    ticket_description?: string | null;
    ticket_status?: string | null;
    ticket_url?: string | null;
    ticket_source?: 'local' | 'jira';
}

export async function getPullRequests(
    projectId: string,
): Promise<{ repo: string; pull_requests: PullRequest[] }> {
    const res = await fetch(`${API_BASE}/projects/${projectId}/pulls`, { headers: getHeaders() });
    return await handleResponse(res);
}

export interface PRTestPlan {
    pr_number: number; title: string; commit_sha: string; changed_files: string[];
    affected_areas: string[]; summary: string; path_explanation: string; missing_coverage: string;
    selected_scenarios: { id: string; name: string; reason: string; steps: string[]; role?: 'consumer' | 'waiter' | 'kitchen' }[];
    // Cross-app verification split into the three roles (each runs on its own app).
    by_role?: {
        consumer: { id: string; name: string; reason: string; steps: string[]; role?: string }[];
        waiter: { id: string; name: string; reason: string; steps: string[]; role?: string }[];
        kitchen: { id: string; name: string; reason: string; steps: string[]; role?: string }[];
    };
    affected_modules?: string[];
    affected_files?: string[];
    graph_driven?: boolean;
    reduction_pct?: number;
    untagged_count?: number;
    skipped_scenarios?: { id: string; name: string; reason: string }[];
}
/** AI test plan for a PR: what to test and how to reach it. */
export async function planPullRequest(projectId: string, number: number): Promise<PRTestPlan> {
    const res = await fetch(`${API_BASE}/projects/${projectId}/pulls/${number}/plan`, { headers: getHeaders() });
    return await handleResponse(res);
}

/** Autonomous QA: plan → build PR branch → run scenarios → comment on the PR. */
export async function autotestPullRequest(projectId: string, number: number): Promise<{ started: boolean; message: string }> {
    const res = await fetch(`${API_BASE}/projects/${projectId}/pulls/${number}/autotest`, { method: 'POST', headers: getHeaders() });
    return await handleResponse(res);
}

export async function testPullRequest(
    projectId: string,
    number: number,
    deviceId?: string,
): Promise<{ run_id: string; branch: string; pr_number: number }> {
    const res = await fetch(`${API_BASE}/projects/${projectId}/pulls/${number}/test`, {
        method: 'POST',
        headers: getHeaders(),
        // Which simulator to run on. Omitted = server picks (PR_TEST_IOS_DEVICE, then any
        // online iOS device) — which is how a run once landed on a brand-new simulator
        // sitting on the first-run onboarding screen.
        body: JSON.stringify(deviceId ? { device_id: deviceId } : {}),
    });
    return await handleResponse(res);
}

export interface GenerateScriptParams {
    provider: string;          // ollama (local, no key) | openai | gemini | claude
    api_key?: string;          // not needed for ollama
    app_name: string;
    requirements: string;
    platform?: string;         // defaults server-side
    framework?: string;
    known_testids?: string[];  // real ids captured from the running app (grounding)
    folded_testids?: string[];
}

export async function generateScript(
    p: GenerateScriptParams,
): Promise<{ test_script: string; automation_yaml: string }> {
    const res = await fetch(`${API_BASE}/intelligence/generate-scripts`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({
            platform: 'iOS',
            framework: 'Appium + pytest (Python)',
            ...p,
        }),
    });
    return await handleResponse(res);
}

export interface ScenarioStepResult {
    step: string;
    ok: boolean;
    action: string;
    detail: string;
    screenshot: string | null;
    healed?: boolean;
    healed_note?: string;
}

export interface ScenarioRunResult {
    ok: boolean;
    passed: number;
    total: number;
    saved_to: string | null;
    script: string;
    steps: ScenarioStepResult[];
}

export async function runScenario(body: {
    project_id: string;
    steps: string[];
    device_id: string;
    bundle_id?: string;
    name?: string;
    save?: boolean;
    env?: string;
}): Promise<ScenarioRunResult> {
    const res = await fetch(`${API_BASE}/scenario/run`, {
        method: 'POST',
        headers: getHeaders(),
        // Same Staging/Live toggle the cross-app suites use, so one switch governs both.
        body: JSON.stringify({ env: localStorage.getItem('flowEnv') || 'staging', ...body }),
    });
    return await handleResponse(res);
}

/** One progress event from a streaming scenario run. */
export type ScenarioEvent =
    | { type: 'phase'; message: string }
    | { type: 'step_start'; index: number; total: number; step: string }
    | ({ type: 'step'; index: number; total: number } & ScenarioStepResult)
    | {
          type: 'done';
          ok: boolean;
          passed: number;
          total: number;
          healed?: number;
          saved_to: string | null;
          script: string;
      }
    | { type: 'error'; detail: string };

export type BatchEvent =
    | { type: 'phase'; message: string }
    | { type: 'scenario_start'; index: number; total: number; name: string }
    | { type: 'step'; scenario_index: number; index: number; total: number; step: string; ok: boolean; action: string; detail: string; healed?: boolean }
    | { type: 'scenario_done'; index: number; name: string; ok: boolean; passed: number; total: number; healed: number }
    | { type: 'done'; scenarios: { name: string; ok: boolean; passed: number; total: number }[]; passed: number; total: number }
    | { type: 'error'; detail: string };

/** Run several scenarios against ONE reused Appium session (much faster). */
export async function runScenariosBatch(
    body: { project_id: string; device_id: string; bundle_id?: string; prepare?: boolean; scenarios: { name: string; steps: string[] }[] },
    onEvent: (ev: BatchEvent) => void,
    signal?: AbortSignal,
): Promise<void> {
    const res = await fetch(`${API_BASE}/scenario/run-batch/stream`, {
        method: 'POST', headers: getHeaders(), body: JSON.stringify(body), signal,
    });
    if (!res.ok || !res.body) {
        let detail = `HTTP ${res.status}`;
        try { detail = (await res.json())?.detail ?? detail; } catch { /* keep */ }
        throw new Error(detail);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        for (;;) {
            const sep = buf.indexOf('\n\n');
            if (sep === -1) break;
            const frame = buf.slice(0, sep); buf = buf.slice(sep + 2);
            for (const line of frame.split('\n')) {
                if (!line.startsWith('data: ')) continue;
                try { onEvent(JSON.parse(line.slice(6))); } catch { /* skip */ }
            }
        }
    }
}

/** Run a scenario, reporting each step as it happens. Resolves when the stream
 *  ends. Uses fetch rather than EventSource: this is a POST with a body and an
 *  auth header, neither of which EventSource supports. */
export async function runScenarioStream(
    body: {
        project_id: string;
        steps: string[];
        device_id: string;
        bundle_id?: string;
        name?: string;
        save?: boolean;
        prepare?: boolean;
    },
    onEvent: (ev: ScenarioEvent) => void,
    signal?: AbortSignal,
): Promise<void> {
    const res = await fetch(`${API_BASE}/scenario/run/stream`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(body),
        signal,
    });

    // A rejected run (404/400/503) answers with plain JSON, not a stream.
    if (!res.ok || !res.body) {
        let detail = `HTTP ${res.status}`;
        try {
            detail = (await res.json())?.detail ?? detail;
        } catch {
            /* keep the status line */
        }
        throw new Error(detail);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line. A frame can straddle two
        // chunks, so only whole frames are consumed here.
        for (;;) {
            const sep = buf.indexOf('\n\n');
            if (sep === -1) break;
            const frame = buf.slice(0, sep);
            buf = buf.slice(sep + 2);
            for (const line of frame.split('\n')) {
                if (!line.startsWith('data: ')) continue;
                try {
                    onEvent(JSON.parse(line.slice(6)));
                } catch {
                    /* ignore malformed event */
                }
            }
        }
    }
}

export interface LocatorCapture {
    known_testids: string[];
    folded_testids: string[];
    gaps: { type: string; label: string | null; parent_id: string | null; suggested_testid: string }[];
}

/** Capture the live locator catalog from a running app (grounds the generator). */
export async function captureLocators(
    deviceId: string,
    bundleId: string,
): Promise<LocatorCapture> {
    const res = await fetch(`${API_BASE}/intelligence/capture-locators`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ device_id: deviceId, bundle_id: bundleId }),
    });
    return await handleResponse(res);
}

export async function cancelAllQueued(): Promise<{ cancelled: number; message: string }> {
    const res = await fetch(`${API_BASE}/automation/cancel-all-queued`, {
        method: 'POST',
        headers: getHeaders(),
    });
    return await handleResponse(res);
}

export async function stopRun(runId: string): Promise<void> {
    const res = await fetch(`${API_BASE}/automation/stop`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ run_id: runId })
    });
    await handleResponse(res);
}

export async function getLiveStatus(runId: string): Promise<any> {
    const res = await fetch(`${API_BASE}/automation/status/${runId}`, { headers: getHeaders() });
    return await handleResponse(res);
}

export interface AIRecommendation {
    id: string;
    project_id: string;
    run_id: string | null;
    type: string;
    payload: any;
    status: string;
    feedback: string | null;
    created_at: string;
}

export async function getRecommendationsList(status?: string): Promise<AIRecommendation[]> {
    const url = status ? `${API_BASE}/intelligence/recommendations?status=${status}` : `${API_BASE}/intelligence/recommendations`;
    const res = await fetch(url, { headers: getHeaders() });
    const data = await handleResponse(res);
    return data.recommendations;
}

export async function updateRecommendationStatus(id: string, status: string, feedback?: string): Promise<void> {
    const res = await fetch(`${API_BASE}/intelligence/recommendations/${id}`, {
        method: 'PUT',
        headers: getHeaders(),
        body: JSON.stringify({ status, feedback })
    });
    await handleResponse(res);
}

export async function generateTestPlan(projectId: string, deviceId: string = ''): Promise<any> {
    const res = await fetch(`${API_BASE}/automation/plan`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ project_id: projectId, device_id: deviceId })
    });
    return await handleResponse(res);
}
export async function getOpsMetrics(): Promise<any> {
    const res = await fetch(`${API_BASE}/ops/metrics`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function getOpsAlerts(): Promise<any> {
    const res = await fetch(`${API_BASE}/ops/alerts`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function getOpsAgents(): Promise<any> {
    const res = await fetch(`${API_BASE}/ops/agents`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function drainOpsAgent(agentId: string): Promise<any> {
    const res = await fetch(`${API_BASE}/ops/agents/${agentId}/drain`, { method: 'POST', headers: getHeaders() });
    return await handleResponse(res);
}
export async function getAuditLogs(): Promise<any> {
    const res = await fetch(`${API_BASE}/ops/audit`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function getFullHealth(): Promise<any> {
    const res = await fetch(`${API_BASE}/ops/health/full`, { headers: getHeaders() });
    return await handleResponse(res);
}
export async function getQuickHealth(): Promise<any> {
    const res = await fetch(`${API_BASE}/ops/health`);
    return await handleResponse(res);
}

// ── Script Editor: Project File API ─────────────────────────────────────────

export interface FileEntry {
    path: string;
    name: string;
    type: 'file' | 'directory';
}

export async function getProjectFiles(projectId: string): Promise<FileEntry[]> {
    const res = await fetch(`${API_BASE}/projects/${projectId}/files`, {
        headers: getHeaders(),
    });
    const data = await handleResponse(res);
    return data.files as FileEntry[];
}

export async function getProjectFileContent(
    projectId: string,
    filePath: string,
): Promise<string> {
    // The backend returns PlainTextResponse, not JSON, so we use res.text().
    const res = await fetch(
        `${API_BASE}/projects/${projectId}/files/${encodeURIComponent(filePath)}`,
        { headers: getHeaders(false) },
    );
    if (res.status === 401) {
        setAuthToken(null);
        window.location.href = '/login';
        throw new Error('Unauthorized');
    }
    if (!res.ok) {
        throw new Error(`Failed to load file: ${res.status}`);
    }
    return res.text();
}

export async function saveProjectFile(
    projectId: string,
    filePath: string,
    content: string,
): Promise<void> {
    const res = await fetch(
        `${API_BASE}/projects/${projectId}/files/${encodeURIComponent(filePath)}`,
        {
            method: 'PUT',
            headers: getHeaders(),
            body: JSON.stringify({ content }),
        },
    );
    await handleResponse(res);
}

export async function runProjectFile(
    projectId: string,
    filePath: string,
    deviceId: string,
): Promise<{ run_id: string }> {
    const res = await fetch(
        `${API_BASE}/projects/${projectId}/files/${encodeURIComponent(filePath)}/run`,
        {
            method: 'POST',
            headers: getHeaders(),
            body: JSON.stringify({ device_id: deviceId }),
        },
    );
    return handleResponse(res);
}

// ── AI Chat Session API ───────────────────────────────────────────────────

export interface ChatSessionSummary {
    session_id: string;
    title: string;
    created_at: string;
    message_count: number;
}

export interface ChatSessionDetail {
    session_id: string;
    title: string;
    messages: Array<{
        role: 'user' | 'assistant';
        content: string;
        message_type: 'text' | 'structured';
        timestamp: string;
    }>;
}

/** Returns the list of recent chat sessions. */
export async function getSessions(): Promise<ChatSessionSummary[]> {
    const res = await fetch(`${API_BASE}/intelligence/chat/sessions`, {
        headers: getHeaders(),
    });
    const data = await handleResponse(res);
    return data.sessions as ChatSessionSummary[];
}

/** Returns full message history for a session. */
export async function getSession(sessionId: string): Promise<ChatSessionDetail> {
    const res = await fetch(`${API_BASE}/intelligence/chat/sessions/${sessionId}`, {
        headers: getHeaders(),
    });
    return handleResponse(res);
}

/** Deletes a session and all its messages. */
export async function deleteSession(sessionId: string): Promise<void> {
    const res = await fetch(`${API_BASE}/intelligence/chat/sessions/${sessionId}`, {
        method: 'DELETE',
        headers: getHeaders(),
    });
    await handleResponse(res);
}

/**
 * Fallback: synchronous (non-streaming) chat via POST.
 * Used when EventSource streaming fails.
 */
export async function sendChat(query: string, context?: Record<string, unknown>): Promise<string> {
    const res = await fetch(`${API_BASE}/intelligence/chat`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ query, context: context ?? {} }),
    });
    const data = await handleResponse(res);
    return data.reply as string;
}

// ── Latest build: update notification + one-click deploy to every device ─────
export interface BuildUpdateProject {
    project_id: string;
    name: string;
    platform: string;
    branch: string;
    has_updates: boolean;
    current_commit: string;
    author: string;
    /** What is installed on each simulator right now — visible before deploying. */
    installed?: { device: string; device_id: string; version: string; build: string }[];
}
export interface BuildDeployResult {
    project: string;
    device?: string;
    device_id?: string;
    stage: string;
    ok: boolean;
    detail: string;
    bundle_id?: string;
    /** Version READ BACK from the device after installing — not what we hoped to install. */
    version?: string | null;
    build?: string | null;
    previous_version?: string | null;
    previous_build?: string | null;
    commit?: string | null;
    /** False when the device reports a different version than the artifact: the
     *  install did not take, however green the step looked. */
    version_matches?: boolean;
}
/** Weighted progress for the running deploy. `percent`/`done`/`total` are WORK UNITS,
 *  not steps logged — the build is one step but most of the wall clock. `eta_s` is null
 *  until a unit completes, because before that there is no honest basis for a number. */
export interface BuildDeployProgress {
    percent: number;
    done: number;
    total: number;
    phase: string;
    detail: string;
    elapsed_s: number;
    eta_s: number | null;
    phase_elapsed_s: number;
}

export interface BuildDeployStatus {
    status: 'idle' | 'running' | 'completed' | 'completed_with_errors' | 'failed';
    steps: { at: string; message: string }[];
    results: BuildDeployResult[];
    projects?: string[];
    started_at?: string;
    finished_at?: string;
    progress?: BuildDeployProgress;
}

export async function getBuildUpdates(): Promise<{ projects: BuildUpdateProject[]; count: number }> {
    const res = await fetch(`${API_BASE}/builds/updates`, { headers: getHeaders() });
    return await handleResponse(res);
}

/** Pull latest, build once per project, install on the CHOSEN simulators.
 *  Omitting deviceIds installs on every discovered simulator — rarely what you want,
 *  so the UI always sends an explicit selection. */
export async function deployLatestBuild(
    projectIds?: string[], deviceIds?: string[], includeShutdown = true,
): Promise<{ started: boolean; projects: string[] }> {
    const body: Record<string, unknown> = { include_shutdown: includeShutdown };
    if (projectIds?.length) body.project_ids = projectIds;
    if (deviceIds?.length) body.device_ids = deviceIds;
    const res = await fetch(`${API_BASE}/builds/deploy`, {
        method: 'POST', headers: getHeaders(), body: JSON.stringify(body),
    });
    return await handleResponse(res);
}

export async function getBuildDeployStatus(): Promise<BuildDeployStatus> {
    const res = await fetch(`${API_BASE}/builds/deploy/status`, { headers: getHeaders() });
    return await handleResponse(res);
}

// ── UI Inspector ───────────────────────────────────────────────────────────
export interface InspectorElement {
    label: string;
    type: string | null;
    frame: { x: number; y: number; w: number; h: number };
    centre: { x: number; y: number };
}
export interface InspectorProblem {
    label: string;
    frame: { x: number; y: number; w: number; h: number };
    issues: { kind: string; by?: string; fraction?: number; detail: string }[];
}
export interface InspectorTree {
    udid: string;
    screen: { width: number; height: number; orientation: 'portrait' | 'landscape' };
    element_count: number;
    elements: InspectorElement[];
    problems: InspectorProblem[];
    problem_count: number;
}

export async function getInspectorTree(udid: string, safeMargin = 0): Promise<InspectorTree> {
    const res = await fetch(
        `${API_BASE}/inspector/${udid}/tree?safe_margin=${safeMargin}`,
        { headers: getHeaders() });
    return await handleResponse(res);
}

/** Screen image plus its PIXEL size — the app reports POINTS, and on a landscape
 *  device the two are rotated relative to each other, so the caller must scale. */
export async function getInspectorScreenshot(
    udid: string,
): Promise<{ udid: string; width: number; height: number; image: string }> {
    const res = await fetch(`${API_BASE}/inspector/${udid}/screenshot`, { headers: getHeaders() });
    return await handleResponse(res);
}

// ── Execution agent runner control ──────────────────────────────────────────

export interface RunnerState {
    running: boolean;
    pid: number | null;
    last_log: string;
    message?: string;
}

export async function getRunnerState(): Promise<RunnerState> {
    const res = await fetch(`${API_BASE}/agents/runner`, { headers: getHeaders() });
    if (!res.ok) throw new Error('Could not read agent state');
    return res.json();
}

/** Start or stop the execution agent process. The backend runs the same
 *  start-agent.sh / pid-file teardown the terminal does. */
export async function setRunnerRunning(on: boolean): Promise<RunnerState> {
    const res = await fetch(`${API_BASE}/agents/runner/${on ? 'start' : 'stop'}`, {
        method: 'POST', headers: getHeaders(),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `Could not ${on ? 'start' : 'stop'} the agent`);
    return body;
}
