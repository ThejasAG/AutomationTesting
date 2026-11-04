import { API_BASE, getHeaders } from './api';

// The rich report endpoint requires a login. window.open() performs a plain
// browser navigation with no Authorization header, so it 401s — fetch the HTML
// with credentials and open that instead of leaving the endpoint unprotected.
export async function openRichReport(runId: string) {
    const tab = window.open('', '_blank');
    try {
        const res = await fetch(`${API_BASE}/reports/${runId}/rich`, { headers: getHeaders() });
        if (!res.ok) throw new Error(`Report failed to load (${res.status})`);
        const html = await res.text();
        if (tab) { tab.document.open(); tab.document.write(html); tab.document.close(); }
    } catch (e) {
        if (tab) tab.document.body.innerText = `Could not open the report: ${(e as Error).message}`;
    }
}
