/** A cross-app flow as an editable script, for the Scripts page.
 *
 *  The user, 2026-11-17: the Scripts page shows an `ios/` folder with the END TO END
 *  SCENARIO and an empty `android/` folder ("keep android folder empty for now as we
 *  don't have any scripts yet").
 *
 *  The format, one block per segment:
 *
 *      [1] Host (C-App): book, pre-order, pay | consumer
 *      open app
 *      @consumer_home
 *
 *  A header line `[number] name | role`, then one step per line. Lines starting with
 *  `#` are comments. Saving sends the segments to the server, which checks every
 *  step before it stores the edit. */
import type { CrossAppFlow, FlowSegment } from './api';

export const SCRIPT_FOLDERS = ['ios', 'android'];

export const SCRIPT_TREE: Record<string, { flowId: string; platform: 'ios' | 'android' }> = {
  'ios/end_to_end_scenario': { flowId: 'flow_full_event', platform: 'ios' },
};

const HEADER = /^\[([^\]]+)\]\s+(.+?)\s+\|\s+([a-z_]+)\s*$/;

function wrap(text: string, width = 86): string[] {
  const lines: string[] = [];
  let line = '';
  for (const word of (text || '').split(/\s+/).filter(Boolean)) {
    if (line && line.length + 1 + word.length > width) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

export function flowToScript(flow: CrossAppFlow): string {
  const out = [`# ${flow.name}`, `# flow: ${flow.id}`, '#',
    ...wrap(flow.description).map(l => `# ${l}`), '#',
    '# One block per segment: [number] name | role, then one step per line.', ''];
  for (const s of flow.segments) {
    out.push(`[${s.num}] ${s.name} | ${s.role}`, ...s.steps, '');
  }
  return out.join('\n');
}

export function scriptToSegments(text: string): { segments: FlowSegment[]; error?: string } {
  const segments: FlowSegment[] = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line || line.startsWith('#')) continue;
    const head = HEADER.exec(line);
    if (head) {
      segments.push({ num: head[1].trim(), name: head[2].trim(), role: head[3], steps: [] });
    } else if (!segments.length) {
      return { segments: [], error: `line ${i + 1}: a step before the first segment header` };
    } else {
      segments[segments.length - 1].steps.push(line);
    }
  }
  if (!segments.length) return { segments, error: 'the script has no segments' };
  const empty = segments.find(s => !s.steps.length);
  if (empty) return { segments, error: `segment [${empty.num}] has no steps` };
  return { segments };
}
