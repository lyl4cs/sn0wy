/**
 * Report page ("/scans/:id/report"): score, the five core metrics, and findings
 * with on-demand AI fixes.
 *
 * HOW IT WORKS
 * - GET /scans/{id}/report. A 409 means the scan isn't done yet, so we send you to
 *   the scan page (which polls). 404 is an unknown id.
 * - Findings are shown in the order the API returns them (biggest estimated time
 *   savings first; the backend sorts them).
 * - "Get AI fix" calls GET /scans/{id}/fixes?limit=N. That endpoint returns fixes
 *   for the TOP N findings in order, so to get the fix for the finding at index i we
 *   ask for N = i + 1 (the API caps N at 10). The response also contains fixes for
 *   every finding above it, so `useFixes` caches them all by index: opening any of
 *   those later costs no request. If a bigger request is already in flight, a
 *   click on a finding it covers waits for that request instead of firing another.
 * - 503 from /fixes means the server has no AI configured (no PROJECT_ID /
 *   DATABASE_URL). That's a deployment fact, not a failure, so it gets a calm inline
 *   note instead of an error.
 *
 * WHY FETCH FIXES LAZILY
 * Each fix is a Gemini call (seconds of latency and real money, shown on the card).
 * Only generating the ones a person actually opens keeps the report instant and the
 * bill small.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api,
  ApiError,
  type Conditions,
  type FixOut,
  type Offender,
  type Report,
  type StoredFinding,
} from "../api";
import { Explain } from "../components/Explain";
import { describeConditions } from "../lib/conditions";
import {
  ChevronIcon,
  CopyIcon,
  ExternalIcon,
  RetryIcon,
  SparkleIcon,
  SpinnerIcon,
  TrendIcon,
} from "../components/Icons";
import { Inline } from "../components/Inline";
import { ErrorState, Loading } from "../components/States";
import {
  METRICS,
  band,
  displayUrl,
  findingScore,
  formatBytes,
  formatCost,
  formatDuration,
  formatLongDate,
  formatMetric,
  formatMs,
  formatTime,
  rateMetric,
  sourceLink,
} from "../lib/format";
import { useTitle } from "../lib/useTitle";
import { useRescan } from "../lib/useRescan";

/** The fixes endpoint covers at most the top 10 findings (services/api/main.py). */
export const MAX_FIX_LIMIT = 10;

export function fixLimitFor(index: number): number {
  return Math.min(index + 1, MAX_FIX_LIMIT);
}

type FixView =
  | { kind: "loading" }
  | { kind: "open" }
  | { kind: "unconfigured"; message: string }
  | { kind: "error"; message: string };

export function useFixes(scanId: string) {
  const navigate = useNavigate();
  const cache = useRef(new Map<number, FixOut>());
  const inflight = useRef<{ limit: number; promise: Promise<FixOut[]> } | null>(null);
  const [views, setViews] = useState<Record<number, FixView | undefined>>({});

  const setView = (i: number, v: FixView | undefined) => setViews((prev) => ({ ...prev, [i]: v }));

  const open = useCallback(
    async (index: number) => {
      if (cache.current.has(index)) {
        setView(index, { kind: "open" });
        return;
      }
      const limit = fixLimitFor(index);
      setView(index, { kind: "loading" });
      let req = inflight.current;
      if (!req || req.limit < limit) {
        const promise = api.getFixes(scanId, limit);
        req = { limit, promise };
        inflight.current = req;
        promise.finally(() => {
          if (inflight.current === req) inflight.current = null;
        }).catch(() => {});
      }
      try {
        const fixes = await req.promise;
        fixes.forEach((f, j) => cache.current.set(j, f));
        setView(index, cache.current.has(index) ? { kind: "open" } : { kind: "error", message: "The API returned no fix for this finding." });
      } catch (e) {
        if (e instanceof ApiError && e.status === 503) {
          setView(index, { kind: "unconfigured", message: e.message });
        } else if (e instanceof ApiError && e.status === 409) {
          navigate(`/scans/${scanId}`, { replace: true });
        } else {
          setView(index, { kind: "error", message: e instanceof Error ? e.message : "Something went wrong." });
        }
      }
    },
    [scanId, navigate],
  );

  const hide = useCallback((index: number) => setView(index, undefined), []);
  const get = (index: number) => cache.current.get(index);
  return { views, open, hide, get };
}

function ScoreCard({ score }: { score: number | null }) {
  const b = score === null ? null : band(score);
  const tickList = [];
  for (let i = 0; i <= 100; i += 2) {
    const near = score === null ? 0 : Math.exp(-((i - score) ** 2) / (2 * 49));
    const c = score === null ? "#DDD6C7" : i === score ? "#12202B" : i < score ? b!.bar : "#DDD6C7";
    tickList.push(<span key={i} style={{ height: Math.round(10 + 32 * near), background: c }} />);
  }
  return (
    <div className="score-card">
      <div className="score-card-head">
        <span className="eyebrow">Performance</span>
        {b && <span className={`band-chip tone-${b.tone}`}>{b.label}</span>}
      </div>
      <div className="score-big">
        <b>{score ?? "—"}</b>
        <span>/ 100</span>
      </div>
      <div className="score-scale" aria-hidden="true">
        <div className="score-ticks">{tickList}</div>
        <div className="score-legend">
          <span>0 · poor</span>
          <span>50 · needs work</span>
          <span>90+</span>
        </div>
      </div>
    </div>
  );
}

function MetricCards({ metrics }: { metrics: Report["metrics"] }) {
  return (
    <ul className="metrics-grid" aria-label="Core metrics">
      {METRICS.map((def) => {
        const value = metrics[def.field];
        const r = rateMetric(def, value);
        return (
          <li key={def.key} className="metric">
            <div className="metric-head">
              <span className="metric-key">{def.key}</span>
              <Explain label={def.name}>{def.help}</Explain>
              <span className={`metric-rating tone-${r.tone}`}>{r.label}</span>
            </div>
            <span className="metric-value">{formatMetric(def, value)}</span>
            <span className="metric-name">{def.name}</span>
            <div className="metric-bar" aria-hidden="true">
              <span className="g" style={{ width: `${r.widths.good}%` }} />
              <span className="n" style={{ width: `${r.widths.mid}%` }} />
              <span className="p" style={{ width: `${r.widths.poor}%` }} />
              {r.marker !== null && <span className="marker" style={{ left: `${r.marker}%` }} />}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(t);
  }, [copied]);
  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {
      setCopied(false); // clipboard blocked (e.g. insecure context); text is still selectable
    }
  }
  return (
    <div className="code">
      <div className="code-head">
        <span>code</span>
        <button type="button" className="copy-btn" onClick={copy}>
          <CopyIcon />
          <span aria-live="polite">{copied ? "Copied" : "Copy"}</span>
          <span className="sr-only"> code</span>
        </button>
      </div>
      <pre tabIndex={0} aria-label="Suggested code">
        <code>{code}</code>
      </pre>
    </div>
  );
}

/**
 * The backend checks every site URL in a fix against the files Lighthouse
 * actually saw (core/fixes/verify.py). Paths in folders the audit never touched
 * may be invented by the model, so we say so instead of letting someone paste a
 * broken URL into production.
 */
function UnverifiedNote({ urls }: { urls: string[] }) {
  if (urls.length === 0) return null;
  return (
    <div className="fix-warning" role="note">
      <strong>Check these paths before using the code.</strong> They aren't in any folder the audit
      found files in, so they may not exist on your site:
      <ul>
        {urls.map((u) => (
          <li key={u}>
            <code>{u}</code>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * "Where to put it": the step between knowing the problem and changing something.
 * Comes from a platform table on the server, not from the model (see ADR 021), so
 * it names the kind of file this lives in, never a path inside the user's repo.
 */
function WhereNote({ where }: { where?: string }) {
  if (!where?.trim()) return null;
  return (
    <div className="fix-where" role="note">
      <span className="fix-label">Where to put it</span>
      <p>
        <Inline text={where} />
      </p>
    </div>
  );
}

/**
 * The settings behind the score, next to it. A bare "47" doesn't say whether it
 * was a throttled phone or an unthrottled desktop, and those differ by tens of
 * points on the same page. Host speed is in the tooltip rather than the line,
 * because it explains small swings between runs of an unchanged site.
 */
function ConditionsLine({ conditions }: { conditions?: Conditions }) {
  const summary = describeConditions(conditions);
  if (!summary) return null;
  const bench = conditions?.benchmark_index;
  return (
    <span
      className="conditions"
      title={
        bench
          ? `Measured on a worker with benchmark index ${Math.round(bench)}. ` +
            "Throttling is relative to the host machine, so a slower worker lowers the score a little."
          : undefined
      }
    >
      Measured as <strong>{summary}</strong>
    </span>
  );
}

function FixPanel({ fix, id }: { fix: FixOut; id: string }) {
  return (
    <div className="fix-panel" id={id}>
      <div className="fix-cols">
        <div>
          <span className="fix-label">Diagnosis</span>
          <p>
            <Inline text={fix.diagnosis} />
          </p>
        </div>
        <div>
          <span className="fix-label">Fix</span>
          <p>
            <Inline text={fix.fix} />
          </p>
        </div>
      </div>
      <WhereNote where={fix.where} />
      {fix.code.trim() && <CodeBlock code={fix.code} />}
      <UnverifiedNote urls={fix.unverified_urls ?? []} />
      <div className="fix-foot">
        <ul className="sources" aria-label="Sources">
          <li className="sources-label" aria-hidden="true">
            Sources
          </li>
          {fix.sources.length === 0 && <li className="fix-cost">none retrieved</li>}
          {fix.sources.map((s, i) => {
            const link = sourceLink(s);
            return (
              <li key={i}>
                {link.href ? (
                  <a className="src" href={link.href} target="_blank" rel="noopener noreferrer">
                    {link.label}
                    <ExternalIcon />
                  </a>
                ) : (
                  <span className="src">{link.label}</span>
                )}
              </li>
            );
          })}
        </ul>
        <span className="fix-cost">
          AI fix · written in {formatMs(fix.latency_ms)} · {formatCost(fix.cost_usd)}
        </span>
      </div>
    </div>
  );
}

/**
 * The exact files behind a finding (from Lighthouse's audit details), so the
 * report says WHAT to change, not just which category. Shows the worst 3; the
 * AI fix prompt gets the same list (up to 5).
 */
function OffenderList({ offenders }: { offenders: Offender[] }) {
  if (offenders.length === 0) return null;
  return (
    <div className="offenders">
      <span className="offenders-label">Worst files</span>
      <ul>
        {offenders.slice(0, 3).map((o) => (
          <li key={o.url}>
            <a href={o.url} target="_blank" rel="noreferrer" title={o.url}>
              {shortResource(o.url)}
            </a>
            {o.wasted_kb !== null && o.wasted_kb > 0 && <span className="offender-waste">−{Math.round(o.wasted_kb)} KB</span>}
            {o.wasted_ms !== null && o.wasted_ms > 0 && <span className="offender-waste">−{o.wasted_ms} ms</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** "https://cdn.x.com/a/b/hero-1920.jpg?v=3" -> "cdn.x.com/…/hero-1920.jpg" */
export function shortResource(url: string): string {
  try {
    const u = new URL(url);
    const parts = u.pathname.split("/").filter(Boolean);
    const file = parts.pop() ?? "";
    return parts.length ? `${u.host}/…/${file}` : `${u.host}/${file}`;
  } catch {
    return url;
  }
}

function FindingCard({
  finding,
  index,
  view,
  fix,
  onOpen,
  onHide,
}: {
  finding: StoredFinding;
  index: number;
  view: FixView | undefined;
  fix: FixOut | undefined;
  onOpen: () => void;
  onHide: () => void;
}) {
  const score = findingScore(finding.score);
  const panelId = `fix-${finding.id}`;
  const isOpen = view?.kind === "open" && fix;
  const canFix = index < MAX_FIX_LIMIT;

  return (
    <li className={`finding${isOpen ? " open" : ""}`}>
      <div className="finding-top">
        <div className="finding-main">
          <span className="finding-rank" aria-hidden="true">
            {String(index + 1).padStart(2, "0")}
          </span>
          <div className="finding-text">
            <h3 id={`finding-${finding.id}`}>{finding.title}</h3>
            {finding.description && (
              <p className="finding-desc">
                <Inline text={finding.description} />
              </p>
            )}
            <div className="finding-tags">
              {finding.savings_ms !== null && finding.savings_ms > 0 && (
                <span className="tag tag-ink" title="Estimated time saved">
                  −{formatMs(finding.savings_ms)}
                </span>
              )}
              {finding.savings_bytes !== null && finding.savings_bytes > 0 && (
                <span className="tag tag-sand" title="Estimated bytes saved">
                  −{formatBytes(finding.savings_bytes)}
                </span>
              )}
              {score !== null && <span className={`tag tone-${band(score).tone}`}>score {score}</span>}
            </div>
            <OffenderList offenders={finding.offenders ?? []} />
          </div>
        </div>

        {!canFix ? (
          <span className="fix-note">AI fixes cover the top {MAX_FIX_LIMIT} findings.</span>
        ) : view?.kind === "loading" ? (
          <button type="button" className="pill pill-sm" disabled>
            <span>Writing fix…</span>
            <span className="pill-icon">
              <SpinnerIcon />
            </span>
          </button>
        ) : isOpen ? (
          <button type="button" className="ghost" onClick={onHide} aria-expanded="true" aria-controls={panelId}>
            Hide fix
            <ChevronIcon />
          </button>
        ) : (
          <button
            type="button"
            className="pill pill-sm"
            onClick={onOpen}
            aria-expanded="false"
            aria-label={`Get AI fix for ${finding.title}`}
          >
            <span aria-hidden="true">{view?.kind === "error" ? "Try again" : "Get AI fix"}</span>
            <span className="pill-icon">{view?.kind === "error" ? <RetryIcon size={20} /> : <SparkleIcon />}</span>
          </button>
        )}
      </div>

      <div aria-live="polite">
        {view?.kind === "loading" && (
          <div className="fix-panel loading" role="status">
            <span className="sr-only">Writing an AI fix…</span>
            <span style={{ width: "38%" }} />
            <span style={{ width: "82%" }} />
            <span style={{ width: "64%" }} />
          </div>
        )}
        {view?.kind === "unconfigured" && (
          <div className="fix-panel problem unconfigured">
            <p>
              <strong>AI fixes aren’t switched on for this server.</strong> The API needs Vertex AI and the
              docs database configured (PROJECT_ID and DATABASE_URL). The Lighthouse finding above still
              applies.
            </p>
          </div>
        )}
        {view?.kind === "error" && (
          <div className="fix-panel problem" role="alert">
            <p>
              <strong>Couldn’t write a fix.</strong> {view.message}
            </p>
          </div>
        )}
        {isOpen && <FixPanel fix={fix} id={panelId} />}
      </div>
    </li>
  );
}

export function ReportPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const fixes = useFixes(id);
  const { rescan, busy, error: rescanError } = useRescan();

  useTitle(report ? `Report · ${displayUrl(report.scan.url)}` : "Report");

  useEffect(() => {
    const ctrl = new AbortController();
    setReport(null);
    setError(null);
    api
      .getReport(id, ctrl.signal)
      .then(setReport)
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return;
        if (e instanceof ApiError && e.status === 409) {
          navigate(`/scans/${id}`, { replace: true }); // not done yet: watch it on the scan page
          return;
        }
        setError(e instanceof Error ? e : new Error("Couldn't load the report."));
      });
    return () => ctrl.abort();
  }, [id, navigate]);

  if (error) {
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <main style={{ paddingTop: 28 }}>
        <ErrorState
          title={notFound ? "Report not found" : "Couldn’t load this report"}
          message={notFound ? `There is no scan with id ${id}.` : error.message}
        >
          <Link to="/">Start a new scan</Link>
        </ErrorState>
      </main>
    );
  }
  if (!report) {
    return (
      <main style={{ paddingTop: 28 }}>
        <Loading label="Loading report…" />
      </main>
    );
  }

  const { scan } = report;
  const finished = scan.finished_at ? new Date(scan.finished_at) : null;
  const n = report.findings.length;

  return (
    <main>
      <section className="page-head">
        <div className="page-head-text">
          <span className="eyebrow">Report</span>
          <h1>{displayUrl(scan.url)}</h1>
          <p className="page-meta">
            {finished && (
              <span>
                Finished <strong>{`${formatLongDate(finished)} · ${formatTime(finished)}`}</strong>
                <Explain label="this timestamp">
                  {
                    "This is when the site was actually measured, which may be earlier " +
                    "than when you asked. Scanning a URL someone already scanned in the " +
                    "last few minutes gives you that result instead of running a second " +
                    "audit, because the score will not have moved and the site would have " +
                    "to serve the whole page again. Press Scan again to measure it now."
                  }
                </Explain>
              </span>
            )}
            {scan.duration_ms !== null && (
              <span>
                Took <strong>{formatDuration(scan.duration_ms)}</strong>
              </span>
            )}
            {report.platform && (
              <span title={report.platform.evidence.join("\n")}>
                Platform <strong>{report.platform.name}</strong>
              </span>
            )}
            {scan.attempts > 0 && (
              <span>
                Attempt <strong>{scan.attempts}</strong>
              </span>
            )}
            <ConditionsLine conditions={report.conditions} />
          </p>
        </div>
        <div className="page-actions">
          <Link to={`/history?url=${encodeURIComponent(scan.url)}`} className="ghost">
            <TrendIcon />
            Site history
          </Link>
          <button type="button" className="pill pill-sm" onClick={() => rescan(scan.url, { force: true })} disabled={busy}>
            <span>{busy ? "Starting…" : "Scan again"}</span>
            <span className="pill-icon">{busy ? <SpinnerIcon /> : <RetryIcon size={20} />}</span>
          </button>
        </div>
        {rescanError && (
          <p className="form-error" role="alert" style={{ flexBasis: "100%" }}>
            {rescanError}
          </p>
        )}
      </section>

      <section className="overview" aria-label="Score and core metrics">
        <ScoreCard score={report.performance_score} />
        <MetricCards metrics={report.metrics} />
      </section>

      <section className="findings" aria-labelledby="findings-h">
        <div className="findings-head">
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <h2 id="findings-h">Below the waterline</h2>
            <p>
              {n === 0
                ? "No findings: Lighthouse found nothing with estimated savings."
                : `${n} finding${n === 1 ? "" : "s"}, biggest estimated savings first.`}
            </p>
          </div>
          {n > 1 && (
            <p className="note">Savings are estimated per finding and overlap, so they don’t add up to a total.</p>
          )}
        </div>
        {n > 0 && (
          <ol className="findings-list">
            {report.findings.map((f, i) => (
              <FindingCard
                key={f.id}
                finding={f}
                index={i}
                view={fixes.views[i]}
                fix={fixes.get(i)}
                onOpen={() => fixes.open(i)}
                onHide={() => fixes.hide(i)}
              />
            ))}
          </ol>
        )}
      </section>
    </main>
  );
}
