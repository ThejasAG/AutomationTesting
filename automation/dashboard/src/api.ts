export const API_BASE = "http://localhost:8000/api/v1";

export const getAuthToken = () => localStorage.getItem('access_token');
export const setAuthToken = (token: string | null) => {
    if (token) {
        localStorage.setItem('access_token', token);
    } else {
        localStorage.removeItem('access_token');
        localStorage.removeItem('role');
    }
};

const getHeaders = (isJson = true) => {
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
        throw new Error(`API Error: ${res.status}`);
    }
    return await res.json();
};

export interface TestRun {
    id: string;
    test_suite: string;
    test_name: string;
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
}

export interface RCAReport {
    run_id: string;
    root_cause: string;
    failure_category: string;
    affected_modules: string[];
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

export interface ProjectHealth {
    repository_exists: boolean;
    yaml_valid: boolean;
    venv_exists: boolean;
    dependencies_installed: boolean;
}

export interface Project {
    id: string;
    name: string;
    description: string;
    git_url: string;
    default_branch: string;
    status: string;
    health: ProjectHealth;
}

export async function getProjects(): Promise<Project[]> {
    const res = await fetch(`${API_BASE}/projects`, { headers: getHeaders() });
    const data = await handleResponse(res);
    return data.projects;
}

export async function addProject(data: any): Promise<void> {
    const res = await fetch(`${API_BASE}/projects`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(data)
    });
    await handleResponse(res);
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

