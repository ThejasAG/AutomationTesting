import { useEffect, useRef } from 'react';

/** Keep a page live without a reload: re-run `load` every `liveMs` while something
 *  is in progress and every `idleMs` otherwise, so a run started anywhere else (another
 *  tab, the AI Agent, the nightly job) still shows up. Polling pauses while the tab is
 *  hidden and catches up the moment it is visible or focused again.
 *
 *  The next poll is scheduled only after the previous one finished, so a slow
 *  backend never piles up overlapping requests. */
export function useLivePoll(load: () => unknown, { live = false, liveMs = 2500, idleMs = 10000 }: {
  live?: boolean; liveMs?: number; idleMs?: number;
} = {}) {
  const ref = useRef(load);
  ref.current = load;

  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    let running = false;
    const visible = () => document.visibilityState === 'visible';

    const run = async () => {
      if (running) return;
      running = true;
      try { await ref.current(); } catch { /* the page shows its own errors */ }
      finally { running = false; }
    };
    const schedule = () => {
      window.clearTimeout(timer);
      if (stopped) return;
      timer = window.setTimeout(async () => {
        if (visible()) await run();
        schedule();
      }, live ? liveMs : idleMs);
    };
    const onBack = () => {
      if (!visible()) return;
      run().then(schedule);
    };

    schedule();
    document.addEventListener('visibilitychange', onBack);
    window.addEventListener('focus', onBack);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onBack);
      window.removeEventListener('focus', onBack);
    };
  }, [live, liveMs, idleMs]);
}
