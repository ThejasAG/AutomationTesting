/** Parse a timestamp the API returned.
 *
 * The backend stores `datetime.utcnow()` and serialises it naive — no `Z`, no
 * offset — so `new Date(...)` / `parseISO(...)` read it as LOCAL time. On a
 * GMT+5:30 machine a run that just finished showed as "about 6 hours ago".
 *
 * Anything from the API goes through here. Date-only strings ("2026-08-19") are
 * left alone: forcing them to UTC shifts them a day for anyone west of GMT.
 */
const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/;
const HAS_TIME = /\d{2}:\d{2}/;

export function parseServerDate(iso: string): Date {
  return new Date(HAS_TIME.test(iso) && !HAS_ZONE.test(iso) ? `${iso}Z` : iso);
}

/** Same, but tolerates null/undefined — returns null so callers can render a dash. */
export function parseServerDateOrNull(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = parseServerDate(iso);
  return isNaN(d.getTime()) ? null : d;
}
