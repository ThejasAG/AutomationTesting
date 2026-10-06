// AI Agent page API (backend: automation/api/v1/routers/agentic.py).
// Kept out of api.ts so the agent can change without touching the shared client.
import { API_BASE, getHeaders } from './api';

export type AgentSettings = {
    enabled: boolean; nightly_time: string; env: string; flows: string[];
    include_demos: boolean; model: string; budget_usd: number; triage: boolean;
    autofix_tests: boolean; app_fix_mode: 'off' | 'propose' | 'pr'; max_fix_attempts: number;
    infra_wait_minutes: number; report_emails: string[]; report_slack: boolean;
    generator_enabled: boolean; generator_day: string;
};

export type AgentStatus = {
    batch_id: string | null; run_id: string | null; phase: string; busy: string | null;
    running: boolean; next_run: string; settings: AgentSettings;
    secrets: { anthropic: boolean; smtp: boolean; slack: boolean; github: boolean };
    apps: Record<string, boolean>; claude_ready: boolean;
};

export type AgentBatch = {
    id: string; trigger: string; env: string; status: string; started_at: string | null;
    finished_at: string | null; totals: Record<string, number>; spend_usd: number; report_sent: boolean;
};

export type AgentVerdict = {
    category?: string; root_cause?: string; evidence?: string[]; confidence?: string;
    recommended_action?: string;
    test_fix?: { segment_num: string; old_step: string; new_steps: string[]; reason: string };
    app_fix?: { app: string; patch: string; explanation: string };
};

export type AgentItem = {
    id: string; position: number; flow_id: string; flow_name: string; status: string;
    run_id: string | null; final_run_id: string | null; attempts: number; category: string | null;
    root_cause: string | null; verdict: AgentVerdict | null; started_at: string | null; finished_at: string | null;
};

export type AgentAction = {
    id: string; batch_id: string | null; item_id: string | null; run_id: string | null;
    kind: string; status: string; title: string; detail: Record<string, unknown>;
    cost_usd: number; created_at: string | null;
};

export type CoverageRoute = { route: string; file: string | null; covered: boolean; ids: number; covered_ids: string[] };
export type AppCoverage = {
    screens: number; screens_covered: number; ids: number; ids_covered: number; routes: CoverageRoute[];
};
export type Coverage = Record<string, AppCoverage>;
export type Feature = {
    name: string; app: string; screens: string[]; coverage: 'covered' | 'partial' | 'missing';
    covered_by: string[]; risk: 'high' | 'medium' | 'low'; notes: string;
};

export type AgentFlow ={ id: string; name: string; segments: number; demo: boolean; agent: boolean };
export type PreflightCheck = { name: string; ok: boolean; detail: string };

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

export const agentApi = {
    status: () => call<AgentStatus>('/status'),
    saveSettings: (changes: Partial<AgentSettings>) =>
        call<AgentSettings>('/settings', { method: 'PUT', body: JSON.stringify({ changes }) }),
    flows: () => call<{ flows: AgentFlow[] }>('/flows'),
    preflight: (env: string) => call<{ ok: boolean; checks: PreflightCheck[] }>(`/preflight?env=${env}`),
    run: (flow_ids?: string[]) => post<{ batch_id: string }>('/run', { flow_ids: flow_ids?.length ? flow_ids : null }),
    stop: () => post<{ stopping: boolean }>('/stop'),
    batches: () => call<{ batches: AgentBatch[] }>('/batches'),
    batch: (id: string) => call<{ batch: AgentBatch; items: AgentItem[]; actions: AgentAction[] }>(`/batches/${id}`),
    actions: (kind?: string, status?: string) => {
        const q = new URLSearchParams();
        if (kind) q.set('kind', kind);
        if (status) q.set('status', status);
        return call<{ actions: AgentAction[] }>(`/actions?${q}`);
    },
    action: (id: string) => call<AgentAction>(`/actions/${id}`),
    analyze: (runId: string) => post<{ started: boolean }>(`/analyze/${runId}`),
    generate: (focus: string) => post<{ started: boolean }>('/generate', { focus }),
    approve: (id: string) => post<Record<string, unknown>>(`/actions/${id}/approve`),
    approveTest: (id: string) => post<{ flow_id: string; batch_id: string }>(`/actions/${id}/approve-test`),
    inventory: () => call<Coverage>('/inventory'),
    analysis: () => call<{ analysis: AgentAction | null }>('/analysis'),
    reject: (id: string) => post<{ rejected: boolean }>(`/actions/${id}/reject`),
    rollback: (id: string) => post<{ rolled_back: boolean }>(`/actions/${id}/rollback`),
    resendReport: (batchId: string) => post<Record<string, string>>(`/batches/${batchId}/report`),
};
