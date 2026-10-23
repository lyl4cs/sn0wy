/**
 * `useRescan`: start a fresh scan of a URL and navigate to it.
 *
 * Used by Retry on a failed scan and by "Scan again" on the report and history
 * pages. POSTs /scans, records the new scan in the recent-scans list, then routes
 * to /scans/{id}. On failure it surfaces the message and clears `busy` so the
 * button is clickable again; on success it stays busy because the page is leaving.
 *
 * WHY IT LIVES HERE AND NOT IN ScanPage
 * It used to be exported from pages/ScanPage.tsx, which ReportPage and HistoryPage
 * imported. That is harmless until the routes become lazy chunks (see App.tsx), at
 * which point Rollup emits `import {...} from "./ScanPage-<hash>.js"` inside the
 * ReportPage and HistoryPage chunks. Opening a report would then fetch two chunks
 * instead of one, the second being the scan page's polling logic, illustrations and
 * copy tables, for the sake of an eighteen-line hook. Measured on this tree: an
 * extra 8.8 kB request on both /scans/:id/report and /history.
 *
 * Splitting it out leaves a 0.37 kB chunk the three page chunks share, so a hook
 * used by three routes costs what a hook should. lib/ is where the other shared
 * hook (useTitle) already lives.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { rememberScan } from "./recentScans";

export interface Rescan {
  rescan: (url: string, opts?: { force?: boolean }) => Promise<void>;
  busy: boolean;
  error: string | null;
}

/**
 * WHO PASSES `force` AND WHO MUST NOT
 *
 * "Scan again" on a report or in the history is asking to measure a site that was
 * already measured successfully. Without `force` the API hands back that same scan
 * (dedupe.py), the page routes to the report the user is already looking at, and
 * the button appears broken. Those callers pass `force: true` and accept the
 * server's per-URL cooldown, which arrives as a 429 saying how long to wait.
 *
 * Retry on a *failed* scan must not force. A failure is never deduped, so a plain
 * request already starts fresh work, and forcing would subject the retry to the
 * cooldown instead. That is wrong for the failures that have nothing to do with
 * the target site: a Chrome crash or our own timeout should be retryable at once,
 * not after 60 seconds of penance for a burst that never happened.
 */
export function useRescan(): Rescan {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function rescan(url: string, opts: { force?: boolean } = {}) {
    setBusy(true);
    setError(null);
    try {
      const { scan } = await api.createScan(url, { force: opts.force });
      rememberScan(scan);
      navigate(`/scans/${scan.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't start a new scan.");
      setBusy(false);
    }
  }
  return { rescan, busy, error };
}
