export const API_BASE = "http://localhost:8000/api/v1";

/** Expose base URL for components that build URLs manually (e.g. EventSource). */
export const getApiBase = () => API_BASE;

export const getAuthToken = () => localStorage.getItem('access_token');
export const setAuthToken = (token: string | null) => {
    if (token) {
        localStorage.setItem('access_token', token);
    } else {
        localStorage.removeItem('access_token');
        localStorage.removeItem('role');
    }
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
    launch_time: number | null;
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

export interface TestRun {
    id: string;
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
export async function cloneProject(id: string, force = false): Promise<Project> {
    const res = await fetch(`${API_BASE}/projects/${id}/clone?force=${force}`, {
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
}

/** Starts the full pipeline (clone→build→install app) in the background. */
export async function startPreparation(
    id: string,
    opts: { device_id?: string; generate_yaml?: boolean } = {},
): Promise<PreparationTask> {
    const res = await fetch(`${API_BASE}/projects/${id}/prepare`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({
            device_id: opts.device_id ?? null,
            generate_yaml: opts.generate_yaml ?? false,
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
    updated_at: string;
    url: string;
}

export async function getPullRequests(
    projectId: string,
): Promise<{ repo: string; pull_requests: PullRequest[] }> {
    const res = await fetch(`${API_BASE}/projects/${projectId}/pulls`, { headers: getHeaders() });
    return await handleResponse(res);
}

export async function testPullRequest(
    projectId: string,
    number: number,
): Promise<{ run_id: string; branch: string; pr_number: number }> {
    const res = await fetch(`${API_BASE}/projects/${projectId}/pulls/${number}/test`, {
        method: 'POST',
        headers: getHeaders(),
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
}): Promise<ScenarioRunResult> {
    const res = await fetch(`${API_BASE}/scenario/run`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(body),
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
          saved_to: string | null;
          script: string;
      }
    | { type: 'error'; detail: string };

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
