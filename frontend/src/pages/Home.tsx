/**
 * Home ("/"): URL form that starts a scan, YOUR recent scans, and example reports.
 *
 * HOW IT WORKS
 * Submitting POSTs /scans. The API answers 202 with the queued scan right away
 * (the slow Lighthouse run happens later in the scanner), so we immediately
 * navigate to /scans/:id, which polls until it finishes.
 * "Your recent scans" are the scans THIS browser started (ids kept in
 * localStorage, see lib/recentScans.ts), each looked up by id. There is no global
 * list: it used to leak every visitor's URLs (ADR 017). A done scan links to its
 * report; anything else to its scan page. "Example reports" are curated real
 * scans (src/examples.ts) so first-time visitors see results immediately.
 *
 * The iceberg on the right is decorative: its score and findings are fixed
 * illustration content from the design, not data, and are hidden from screen
 * readers apart from the SVG's description.
 */
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, type ScanSummary } from "../api";
import { EXAMPLES } from "../examples";
import { ArrowRightIcon, GlobeIcon, SpinnerIcon } from "../components/Icons";
import { HeroIceberg } from "../components/Illustrations";
import { ScoreChip } from "../components/ScoreChip";
import { Loading } from "../components/States";
import { displayUrl, formatRelativeDay, normalizeUrlInput } from "../lib/format";
import { clearRecent, forgetScans, loadRecent, rememberScan } from "../lib/recentScans";
import { useTitle } from "../lib/useTitle";

/** Shared by Home and the History search: validates, POSTs, then navigates. */
export function ScanForm({ initial = "" }: { initial?: string }) {
  const navigate = useNavigate();
  const [value, setValue] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const url = normalizeUrlInput(value);
    if (!url) {
      setError("Enter a full web address, like https://yoursite.com");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // No `force`: a plain submission is happy to be served a recent scan of the
      // same URL rather than making the site serve another audit (dedupe.py).
      const { scan } = await api.createScan(url);
      rememberScan(scan);
      navigate(`/scans/${scan.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't start the scan.");
      setBusy(false);
    }
  }

  return (
    <form className="scan-form" onSubmit={onSubmit} noValidate>
      <label htmlFor="scan-url">Website URL</label>
      <div className="scan-form-row">
        <div className="field">
          <GlobeIcon />
          <input
            id="scan-url"
            type="url"
            inputMode="url"
            autoComplete="url"
            placeholder="https://yoursite.com"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "scan-url-error scan-url-hint" : "scan-url-hint"}
          />
        </div>
        <button type="submit" className="pill" disabled={busy}>
          <span>{busy ? "Starting…" : "Scan site"}</span>
          <span className="pill-icon">{busy ? <SpinnerIcon /> : <ArrowRightIcon />}</span>
        </button>
      </div>
      {error && (
        <p className="form-error" id="scan-url-error" role="alert">
          {error}
        </p>
      )}
      <p className="form-hint" id="scan-url-hint">
        A scan takes about 30–60 seconds.
      </p>
    </form>
  );
}

const SHOW_RECENT = 6;

/** Look up one remembered scan; a done scan also gets its score. null = gone (404). */
async function lookup(id: string, signal: AbortSignal): Promise<ScanSummary | null> {
  try {
    const scan = await api.getScan(id, signal);
    if (scan.status !== "done") return { scan, performance_score: null };
    const report = await api.getReport(id, signal).catch(() => null);
    return { scan, performance_score: report?.performance_score ?? null };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

function ScanRow({ scan, performance_score }: ScanSummary) {
  const to = scan.status === "done" ? `/scans/${scan.id}/report` : `/scans/${scan.id}`;
  const statusText = scan.status === "done" ? undefined : scan.status === "failed" ? "Failed" : "In progress";
  return (
    <li>
      <Link to={to} className="recent-row">
        <span className="recent-meta">
          <span className="recent-url">{displayUrl(scan.url)}</span>
          <span className="recent-when">
            {formatRelativeDay(new Date(scan.created_at))}
            {statusText && ` · ${statusText}`}
          </span>
        </span>
        <ScoreChip
          score={scan.status === "done" ? performance_score : null}
          label={scan.status === "done" ? undefined : statusText}
        />
      </Link>
    </li>
  );
}

function RecentScans() {
  const [items, setItems] = useState<ScanSummary[] | null>(() => (loadRecent().length ? null : []));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const mine = loadRecent().slice(0, SHOW_RECENT);
    if (mine.length === 0) return;
    const ctrl = new AbortController();
    Promise.all(mine.map((r) => lookup(r.id, ctrl.signal)))
      .then((found) => {
        forgetScans(mine.filter((_, i) => found[i] === null).map((r) => r.id));
        setItems(found.filter((x): x is ScanSummary => x !== null));
      })
      .catch((e: unknown) => {
        if (!ctrl.signal.aborted) setError(e instanceof Error ? e.message : "Couldn't load scans.");
      });
    return () => ctrl.abort();
  }, []);

  function onClear() {
    clearRecent();
    setItems([]);
  }

  return (
    <section className="recent" aria-labelledby="recent-h">
      <div className="section-head">
        <h2 id="recent-h">Your recent scans</h2>
        {items && items.length > 0 ? (
          <button type="button" className="link-button" onClick={onClear}>
            Clear
          </button>
        ) : (
          <Link to="/history">Site history</Link>
        )}
      </div>
      {error ? (
        <p className="state state-error" role="alert">
          Couldn’t load your recent scans. {error}
        </p>
      ) : items === null ? (
        <Loading label="Loading your recent scans…" />
      ) : items.length === 0 ? (
        <p className="state">Scans you run in this browser show up here. They stay private to you.</p>
      ) : (
        <ul className="recent-grid">
          {items.map((it) => (
            <ScanRow key={it.scan.id} {...it} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** Curated real reports; a card that fails to load is hidden, never an error. */
function ExampleReports() {
  const [cards, setCards] = useState<{ label: string; note?: string; scanId: string; score: number | null }[]>([]);

  useEffect(() => {
    const ctrl = new AbortController();
    Promise.all(
      EXAMPLES.map((ex) =>
        api
          .getReport(ex.scanId, ctrl.signal)
          .then((r) => ({ ...ex, score: r.performance_score }))
          .catch(() => null),
      ),
    ).then((res) => {
      if (!ctrl.signal.aborted) setCards(res.filter((c) => c !== null));
    });
    return () => ctrl.abort();
  }, []);

  if (cards.length === 0) return null;
  return (
    <section className="recent" aria-labelledby="examples-h">
      <div className="section-head">
        <h2 id="examples-h">Example reports</h2>
      </div>
      <ul className="recent-grid">
        {cards.map((c) => (
          <li key={c.scanId}>
            <Link to={`/scans/${c.scanId}/report`} className="recent-row">
              <span className="recent-meta">
                <span className="recent-url">{c.label}</span>
                {c.note && <span className="recent-when">{c.note}</span>}
              </span>
              <ScoreChip score={c.score} />
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function Home() {
  useTitle("");
  return (
    <>
      <main className="home-hero">
        <section className="home-copy">
          <div className="home-copy" style={{ gap: 20 }}>
            <h1>See the whole iceberg.</h1>
            <p className="home-lede">
              sn0wy measures how fast your page really loads, then writes a fix for every problem it
              finds, with the code and the docs to back it up.
            </p>
          </div>
          <ScanForm />
        </section>
        <figure className="hero-figure">
          <HeroIceberg />
          <div className="hero-score" aria-hidden="true">
            <span className="score-chip xs tone-mid">54</span>
            Your score
          </div>
          <div className="hero-findings" aria-hidden="true">
            <span className="label">Below the waterline</span>
            <div className="hero-finding">
              <b>Oversized hero image</b>
              <span className="mono">−1.9 s</span>
            </div>
            <div className="hero-finding">
              <b>Render-blocking CSS</b>
              <span className="mono">−1.2 s</span>
            </div>
            <div className="hero-finding">
              <b>Unused JavaScript</b>
              <span className="mono">−1.0 s</span>
            </div>
          </div>
        </figure>
      </main>
      <RecentScans />
      <ExampleReports />
    </>
  );
}
