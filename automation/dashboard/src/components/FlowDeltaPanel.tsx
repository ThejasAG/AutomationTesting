import { useState } from 'react';
import { GitCompare, Loader2 } from 'lucide-react';
import { getWorkflowDelta } from '../api';
import type { WorkflowDelta } from '../api';
import ArchifyFrame from './ArchifyFrame';

/** "What did this change do to the application's flow?"
 *
 *  NOT for the Pull Requests page. PRs there are raised against the APPS UNDER TEST
 *  (vya-consumer, vya-business), whose branches — pre-prod-one, staging, preprod-2-May18
 *  — do not exist in the platform repo where automation/workflow/catalog.py lives. Every
 *  such comparison fails with "fatal: invalid object name", because the two refs belong
 *  to different repositories. An app PR cannot change the platform's spine.
 *
 *  Use it only where both refs are PLATFORM refs (the Workflow page does exactly that).
 *
 *  The spine lives in automation/workflow/catalog.py, so a PR that edits it changes the
 *  map. Compares the PR's BASE to the PR's HEAD SHA — not to the working tree, which is
 *  the reviewer's local state and belongs to neither side of the PR.
 *
 *  Collapsed until asked: rendering is a couple of seconds of Node per PR, and most PRs
 *  do not touch the catalog at all. */
export default function FlowDeltaPanel({ baseRef, headRef }: { baseRef: string; headRef: string }) {
    const [delta, setDelta] = useState<WorkflowDelta | null>(null);
    const [loading, setLoading] = useState(false);

    const load = async () => {
        setLoading(true);
        try { setDelta(await getWorkflowDelta(baseRef, headRef)); }
        catch (e) { setDelta({ ok: false, reason: String(e) }); }
        finally { setLoading(false); }
    };

    const s = delta?.summary;
    const unchanged = s
        && !s.components.added && !s.components.removed && !s.components.changed
        && !s.connections.added && !s.connections.removed && !s.connections.changed;

    return (
        <div style={{ marginTop: 10 }}>
            {!delta && (
                <button className="btn" onClick={load} disabled={loading}
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 6,
                             padding: '5px 11px', fontSize: '0.76rem' }}>
                    {loading ? <Loader2 size={12} className="spin" /> : <GitCompare size={12} />}
                    Flow impact
                </button>
            )}

            {delta && !delta.ok && (
                <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                    Flow impact unavailable — {delta.reason}
                </div>
            )}

            {delta?.ok && s && (
                <>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7, alignItems: 'center',
                                  fontSize: '0.76rem', marginBottom: 8 }}>
                        <span style={{ fontWeight: 600 }}>Flow impact</span>
                        <span style={{ color: 'var(--text-muted)', fontFamily: "'Fira Code', monospace" }}>
                            {delta.base_ref} → {delta.head_ref}
                        </span>
                        {unchanged ? (
                            <span style={{ color: 'var(--text-muted)' }}>· no change to the app flow</span>
                        ) : (
                            ([['steps', s.components], ['links', s.connections]] as const).map(([label, c]) => (
                                <span key={label} style={{ border: '1px solid var(--border-color)',
                                                           borderRadius: 6, padding: '2px 7px' }}>
                                    {label} <span style={{ color: '#22c55e' }}>+{c.added}</span>{' '}
                                    <span style={{ color: '#ef4444' }}>−{c.removed}</span>{' '}
                                    <span style={{ color: '#eab308' }}>~{c.changed}</span>
                                </span>
                            ))
                        )}
                    </div>
                    {!unchanged && (
                        <ArchifyFrame title={`Flow delta ${delta.base_ref} to ${delta.head_ref}`}
                            html={delta.html} height={520} />
                    )}
                </>
            )}
        </div>
    );
}
