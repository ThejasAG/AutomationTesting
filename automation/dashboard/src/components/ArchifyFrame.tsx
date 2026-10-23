import { Loader2, AlertTriangle } from 'lucide-react';

/** Renders an Archify artifact.
 *
 *  srcDoc, not src: the artifact comes back through an authenticated fetch (the API
 *  needs a bearer header, which an <iframe src> cannot carry). The HTML is fully
 *  self-contained — no network fetches — so there is nothing to load cross-origin.
 *
 *  sandbox WITHOUT allow-same-origin: the artifact is interactive (search, route
 *  probing, guided stories) so it needs scripts, but it never needs to reach back into
 *  the dashboard's origin, cookies or storage. Granting both allow-scripts and
 *  allow-same-origin together would defeat the sandbox entirely.
 */
export default function ArchifyFrame({
    html, loading, reason, height = 620, title,
}: {
    html?: string;
    loading?: boolean;
    reason?: string;
    height?: number;
    title: string;
}) {
    if (loading) {
        return (
            <div style={{ ...box, height }}>
                <Loader2 size={16} className="spin" /> Rendering diagram…
            </div>
        );
    }
    if (!html) {
        return (
            <div style={{ ...box, height: 'auto', padding: 14, gap: 9, alignItems: 'flex-start' }}>
                <AlertTriangle size={16} style={{ color: '#fbbf24', flexShrink: 0, marginTop: 1 }} />
                <div>
                    <div style={{ fontWeight: 600, fontSize: '0.85rem' }}>Diagram unavailable</div>
                    <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginTop: 3 }}>
                        {reason || 'The renderer produced no output.'}
                    </div>
                </div>
            </div>
        );
    }
    return (
        <iframe
            title={title}
            srcDoc={html}
            sandbox="allow-scripts"
            style={{
                width: '100%', height, border: '1px solid var(--border-color)',
                borderRadius: 10, background: '#0b0f17',
            }}
        />
    );
}

const box: React.CSSProperties = {
    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
    border: '1px solid var(--border-color)', borderRadius: 10,
    color: 'var(--text-muted)', fontSize: '0.85rem', background: 'rgba(0,0,0,0.15)',
};
