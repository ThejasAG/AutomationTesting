// Self-check for parseServerDate. No test runner in this project and not worth
// adding one for a two-line function, so compile the real source with esbuild
// and assert against it — nothing is duplicated here, so the check cannot drift.
// npx fetches esbuild on first run if it is not already in the npm cache.
//
// Run:  node src/time.check.mjs
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const src = fileURLToPath(new URL('./time.ts', import.meta.url));
const js = execFileSync('npx', ['esbuild', src, '--format=esm', '--log-level=error'], { encoding: 'utf8' });
const { parseServerDate, parseServerDateOrNull } =
  await import('data:text/javascript,' + encodeURIComponent(js));

// The bug: the API sends naive UTC, so this must NOT be read as local time.
assert.equal(parseServerDate('2026-08-19T13:02:00').toISOString(), '2026-08-19T13:02:00.000Z');
assert.equal(parseServerDate('2026-08-19T13:02:00.123456').toISOString(), '2026-08-19T13:02:00.123Z');

// Already zoned — leave alone, or we'd shift it a second time.
assert.equal(parseServerDate('2026-08-19T13:02:00Z').toISOString(), '2026-08-19T13:02:00.000Z');
assert.equal(parseServerDate('2026-08-19T18:32:00+05:30').toISOString(), '2026-08-19T13:02:00.000Z');

// Date-only: must stay untouched. Forcing UTC here shifts the day west of GMT.
assert.equal(parseServerDate('2026-08-19').toISOString(), '2026-08-19T00:00:00.000Z');

assert.equal(parseServerDateOrNull(null), null);
assert.equal(parseServerDateOrNull(''), null);
assert.equal(parseServerDateOrNull('not-a-date'), null);

console.log('ok');
