/**
 * History page ("/history?url=..."): one site's scans over time and a
 * before/after comparison between any two finished scans.
 *
 * HOW IT WORKS
 * - The site comes from the ?url= query parameter, so a history view is a
 *   shareable link. Without it we show a URL box that sets the parameter.
 * - GET /sites/scans?url=...&limit=100 returns the scans newest first, each with its
 *   score. lib/chart.ts turns them into the score line chart and the timeline.
 * - Before/After default to the oldest and newest finished scans. Pick a side (the
 *   two cards), then click a scan on the timeline or "Set as ..." in the table.
 * - The comparison needs each scan's metrics and finding count, which only the full
 *   report has, so we GET /scans/{id}/report for the two selected scans and cache
 *   every report we fetch (switching back and forth costs nothing).
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, type Report, type ScanSummary } from "../api";
import { Explain } from "../components/Explain";
import { CASE_STUDY, hasCaseStudy } from "../examples";
import { SelfAudit } from "../components/SelfAudit";
import { comparabilityProblems, hostSpeedGap } from "../lib/conditions";
import { ArrowRightIcon, GlobeIcon, RetryIcon, SpinnerIcon } from "../components/Icons";
import { ScoreChip } from "../components/ScoreChip";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { CHART, buildChart, labelAnchor, type ChartModel } from "../lib/chart";
import {
  METRICS,
  displayUrl,
  formatDuration,
  formatLongDate,
  formatMetric,
  formatShortDate,
  formatTime,
  lowerIsBetterDelta,
  normalizeUrlInput,
  scoreDelta,
  type Delta,
} from "../lib/format";
import { useTitle } from "../lib/useTitle";
import { useRescan } from "../lib/useRescan";

type Slot = "before" | "after";

function SiteSearch() {
  const [, setParams] = useSearchParams();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const url = normalizeUrlInput(value);
    if (!url) {
      setError("Enter a full web address, like https://yoursite.com");
      return;
    }
    setParams({ url });
  }
  return (
    <main className="history-search">
      <span className="eyebrow">Site history</span>
      <h1>How has your site changed?</h1>
      <p>Enter a URL you’ve scanned before to see its score over time and compare any two scans.</p>
      <form className="scan-form" onSubmit={onSubmit} noValidate>
        <label htmlFor="history-url">Website URL</label>
        <div className="scan-form-row">
          <div className="field">
            <GlobeIcon />
            <input
              id="history-url"
              type="url"
              inputMode="url"
              placeholder="https://yoursite.com"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? "history-url-error" : undefined}
            />
          </div>
          <button type="submit" className="pill">
            <span>Show history</span>
            <span className="pill-icon">
              <ArrowRightIcon />
            </span>
          </button>
        </div>
        {error && (
          <p className="form-error" id="history-url-error" role="alert">
            {error}
          </p>
        )}
      </form>

      {/* The showcase belongs here, not only behind ?url=. This is the page someone
          lands on when they type /history, and a case study nobody can find is not
          a case study. It loads its own history, so this view stays dataless. */}
      {hasCaseStudy() && <SelfAudit />}
    </main>
  );
}

function ScoreChart({
  model,
  before,
  after,
}: {
  model: ChartModel;
  before: ScanSummary | null;
  after: ScanSummary | null;
}) {
  const find = (s: ScanSummary | null) => (s ? model.placed.find((p) => p.summary.scan.id === s.scan.id) : undefined);
  const b = find(before);
  const a = find(after);
  const scored = model.placed.filter((p) => p.y !== null);
  const failed = model.failMarks.length;
  const summary =
    scored.length === 0
      ? "No finished scans yet, so there is no score to plot."
      : `Performance score ${scored.length > 1 ? `went from ${scored[0].summary.performance_score} on ${formatShortDate(scored[0].time)} to ${scored[scored.length - 1].summary.performance_score} on ${formatShortDate(scored[scored.length - 1].time)}` : `was ${scored[0].summary.performance_score} on ${formatShortDate(scored[0].time)}`}${failed ? `, with ${failed} failed scan${failed > 1 ? "s" : ""}` : ""}.`;
  const bBelow = b && a && (b.col > a.col || b === a);

  return (
    <section className="card trend" aria-labelledby="trend-h">
      <div className="section-head">
        <h2 id="trend-h">Score over time</h2>
        <span className="trend-sub">Performance score, 0–100</span>
      </div>
      <div className="trend-scroll">
      <svg viewBox={`0 0 ${CHART.width} ${CHART.height}`} role="img" aria-label={summary}>
        <line x1="60" y1="70" x2="960" y2="70" stroke="#E6DFD1" strokeWidth="1" strokeDasharray="4 6" />
        <line x1="60" y1="150" x2="960" y2="150" stroke="#E6DFD1" strokeWidth="1" strokeDasharray="4 6" />
        <line x1="60" y1="250" x2="960" y2="250" stroke="#D6CFC0" strokeWidth="1" />
        <text x="48" y="74" textAnchor="end" className="svg-mono">90</text>
        <text x="48" y="154" textAnchor="end" className="svg-mono">50</text>
        <text x="48" y="254" textAnchor="end" className="svg-mono">0</text>
        {model.areaPath && <path d={model.areaPath} fill="#2F7FB9" opacity="0.08" />}
        {model.linePath && (
          <path d={model.linePath} fill="none" stroke="#2F7FB9" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
        )}
        {model.dotsPath && <path d={model.dotsPath} fill="#2F7FB9" />}
        {model.failPath && <path d={model.failPath} fill="none" stroke="#B23A26" strokeWidth="2.2" strokeLinecap="round" />}
        {model.failMarks.map((x, i) => (
          <text key={i} x={x} y="238" textAnchor="middle" className="svg-mono" style={{ fontSize: 11, fill: "#9A2F1E" }}>
            failed
          </text>
        ))}
        {b && b.y !== null && (
          <g>
            <line x1={b.x} y1={b.y} x2={b.x} y2="250" stroke="#12202B" strokeWidth="1" strokeDasharray="2 4" />
            <circle cx={b.x} cy={b.y} r="8" fill="#FFFFFF" stroke="#12202B" strokeWidth="3" />
            <text x={b.x} y={bBelow ? b.y + 28 : b.y - 16} textAnchor={labelAnchor(b.x)} className="svg-label">
              Before · {b.summary.performance_score}
            </text>
          </g>
        )}
        {a && a.y !== null && (
          <g>
            <line x1={a.x} y1={a.y} x2={a.x} y2="250" stroke="#12202B" strokeWidth="1" strokeDasharray="2 4" />
            <circle cx={a.x} cy={a.y} r={b === a ? 4 : 8} fill="#12202B" stroke="#12202B" strokeWidth="3" />
            <text x={a.x} y={a.y - 16} textAnchor={labelAnchor(a.x)} className="svg-label">
              After · {a.summary.performance_score}
            </text>
          </g>
        )}
        {model.labels.map((l) => (
          <text key={`${l.col}-${l.text}`} x={l.x} y="285" textAnchor={l.anchor} className="svg-mono">
            {l.text}
          </text>
        ))}
      </svg>
      </div>
    </section>
  );
}

function Timeline({
  model,
  beforeId,
  afterId,
  slot,
  onPick,
}: {
  model: ChartModel;
  beforeId: string | null;
  afterId: string | null;
  slot: Slot;
  onPick: (id: string) => void;
}) {
  const byCol = new Map(model.placed.map((p) => [p.col, p]));
  const activeId = slot === "before" ? beforeId : afterId;
  const activeCol = model.placed.find((p) => p.summary.scan.id === activeId)?.col ?? -100;
  const cols = model.columns;

  return (
    <div className="timeline-card">
      <div className="timeline" style={{ minWidth: Math.max(760, cols * 13) }}>
        <div
          className="timeline-row"
          role="group"
          aria-label={`Scan timeline: choose the ${slot} scan`}
          style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
        >
          {Array.from({ length: cols }, (_, col) => {
            const p = byCol.get(col);
            const s = p?.summary.scan;
            const near = Math.exp(-((col - activeCol) ** 2) / (2 * 25));
            const isB = !!s && s.id === beforeId;
            const isA = !!s && s.id === afterId;
            let h = Math.round(10 + 40 * near);
            let c = "#DDD6C7";
            let w = 2;
            if (s?.status === "done") {
              h += 10;
              c = "#2F7FB9";
              w = 3;
            } else if (s?.status === "failed") {
              h += 4;
              c = "#E2A091";
              w = 3;
            } else if (s) {
              h += 4;
              c = "#9DB3C2";
              w = 3;
            }
            if (isB || isA) {
              c = "#12202B";
              w = 4;
              h = Math.max(h, 52);
            }
            const tag = isB && isA ? "Both" : isB ? "Before" : isA ? "After" : "";
            const bar = <span className="tick" style={{ width: w, height: h, background: c }} />;
            if (p && s?.status === "done") {
              const when = `${formatShortDate(p.time)}, ${formatTime(p.time)}`;
              return (
                <button
                  key={col}
                  type="button"
                  className="tl-cell"
                  onClick={() => onPick(s.id)}
                  aria-label={`Set ${slot} to ${when} scan, score ${p.summary.performance_score ?? "unknown"}`}
                  aria-pressed={slot === "before" ? isB : isA}
                >
                  {tag && <span className="tl-tag">{tag}</span>}
                  {bar}
                </button>
              );
            }
            return (
              <span key={col} className="tl-cell" aria-hidden="true">
                {bar}
              </span>
            );
          })}
        </div>
        <div className="timeline-axis" aria-hidden="true">
          {model.labels.map((l) => {
            const pct = cols > 1 ? (l.col / (cols - 1)) * 100 : 50;
            const shift = l.anchor === "start" ? "0" : l.anchor === "end" ? "-100%" : "-50%";
            return (
              <span key={`${l.col}-${l.text}`} style={{ left: `${pct}%`, transform: `translateX(${shift})` }}>
                {l.text}
              </span>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function DeltaPill({ d }: { d: Delta | null }) {
  if (!d) return <span className="delta tone-none">—</span>;
  const tone = d.direction === "better" ? "good" : d.direction === "worse" ? "poor" : "none";
  const words = d.direction === "better" ? "better" : d.direction === "worse" ? "worse" : "no change";
  return (
    <span className={`delta tone-${tone}`}>
      {d.label}
      <span className="sr-only"> ({words})</span>
    </span>
  );
}

function Comparison({
  before,
  after,
  reports,
  loadError,
}: {
  before: ScanSummary;
  after: ScanSummary;
  reports: Map<string, Report>;
  loadError: string | null;
}) {
  const rb = reports.get(before.scan.id);
  const ra = reports.get(after.scan.id);
  const bs = before.performance_score;
  const as = after.performance_score;
  const sd = bs !== null && as !== null ? scoreDelta(bs, as) : null;

  const isCaseStudy = hasCaseStudy() && before.scan.url === CASE_STUDY.url;
  const problems = comparabilityProblems(rb?.conditions, ra?.conditions);
  const hostGap = problems.length === 0 ? hostSpeedGap(rb?.conditions, ra?.conditions) : null;

  return (
    <div className="diff">
      <div className="diff-score">
        <span className="eyebrow">Score</span>
        <div className="diff-score-row">
          <b>{bs ?? "—"}</b>
          <svg width="34" height="20" viewBox="0 0 34 20" fill="none" stroke="#9DB3C2" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label="to">
            <path d="M2 10h28" />
            <path d="M22 3l8 7l-8 7" />
          </svg>
          <b>{as ?? "—"}</b>
        </div>
        {sd && (
          <span className={`delta-dark ${sd.direction === "better" ? "up" : sd.direction === "worse" ? "down" : ""}`}>
            {sd.label} points
          </span>
        )}
      </div>
      <div className="diff-table-wrap">
        {isCaseStudy && (
          <p className="case-note" role="note">
            This is sn0wy's own test case: scanned, fixed using the suggestions it gave, then
            scanned again. {CASE_STUDY.note}
          </p>
        )}
        {problems.length > 0 && (
          <div className="fix-warning" role="note">
            <strong>These two scans aren’t directly comparable.</strong> They weren’t measured the
            same way, so the differences below are partly a settings change, not just your site:
            <ul>
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </div>
        )}
        {hostGap !== null && (
          <div className="fix-warning" role="note">
            <strong>Small changes here may not be real.</strong> These two scans ran on machines
            about {hostGap}% apart in speed, and throttling is relative to the machine.
          </div>
        )}
        {loadError ? (
          <p className="form-error" role="alert" style={{ padding: "16px 0" }}>
            Couldn’t load the reports to compare. {loadError}
          </p>
        ) : !rb || !ra ? (
          <div style={{ padding: "16px 0" }}>
            <Loading label="Loading both reports…" />
          </div>
        ) : (
          <table className="diff-table">
            <caption className="sr-only">Metric changes from the before scan to the after scan</caption>
            <thead>
              <tr>
                <th scope="col">Metric</th>
                <th scope="col">Before</th>
                <th scope="col">After</th>
                <th scope="col">Change</th>
              </tr>
            </thead>
            <tbody>
              {METRICS.map((def) => {
                const b = rb.metrics[def.field];
                const a = ra.metrics[def.field];
                return (
                  <tr key={def.key}>
                    <th scope="row">
                      <span className="k">{def.key}</span> <span className="n">{def.name}</span>{" "}
                      <Explain label={def.name}>{def.help}</Explain>
                    </th>
                    <td className="b">{formatMetric(def, b)}</td>
                    <td className="a">{formatMetric(def, a)}</td>
                    <td>
                      <DeltaPill d={lowerIsBetterDelta(b, a, (v) => formatMetric(def, v))} />
                    </td>
                  </tr>
                );
              })}
              <tr>
                <th scope="row">
                  <span className="k">Findings</span>
                </th>
                <td className="b">{rb.findings.length}</td>
                <td className="a">{ra.findings.length}</td>
                <td>
                  <DeltaPill d={lowerIsBetterDelta(rb.findings.length, ra.findings.length, String)} />
                </td>
              </tr>
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function statusLabel(s: ScanSummary["scan"]): string {
  if (s.status === "failed") {
    const first = (s.error || "unknown error").split("\n")[0];
    return `Failed · ${first.length > 48 ? `${first.slice(0, 47)}…` : first}`;
  }
  return { done: "Done", queued: "Queued", running: "Running" }[s.status];
}

function scanDuration(s: ScanSummary["scan"]): string {
  if (s.duration_ms !== null) return formatDuration(s.duration_ms);
  if (s.finished_at) return formatDuration(Date.parse(s.finished_at) - Date.parse(s.created_at));
  return "—";
}

function SiteHistory({ url }: { url: string }) {
  const [history, setHistory] = useState<ScanSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [slot, setSlot] = useState<Slot>("after");
  const [picked, setPicked] = useState<{ before: string | null; after: string | null }>({ before: null, after: null });
  const [reports, setReports] = useState<Map<string, Report>>(new Map());
  const [reportError, setReportError] = useState<string | null>(null);
  const { rescan, busy, error: rescanError } = useRescan();

  useTitle(`History · ${displayUrl(url)}`);

  useEffect(() => {
    const ctrl = new AbortController();
    setHistory(null);
    setError(null);
    setPicked({ before: null, after: null });
    api
      .siteHistory(url, 100, ctrl.signal)
      .then(setHistory)
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(
          e instanceof ApiError && e.status === 422
            ? `“${url}” isn’t a valid URL.`
            : e instanceof Error
              ? e.message
              : "Couldn't load history.",
        );
      });
    return () => ctrl.abort();
  }, [url]);

  const model = useMemo(() => (history ? buildChart(history) : null), [history]);
  const done = useMemo(() => (history ?? []).filter((h) => h.scan.status === "done"), [history]);
  const beforeId = picked.before ?? (done.length > 1 ? done[done.length - 1].scan.id : null);
  const afterId = picked.after ?? (done.length > 1 ? done[0].scan.id : null);
  const before = done.find((h) => h.scan.id === beforeId) ?? null;
  const after = done.find((h) => h.scan.id === afterId) ?? null;

  // Fetch (and cache) the two reports being compared.
  useEffect(() => {
    const ids = [beforeId, afterId].filter((x): x is string => !!x && !reports.has(x));
    if (ids.length === 0) return;
    const ctrl = new AbortController();
    setReportError(null);
    Promise.all(ids.map((id) => api.getReport(id, ctrl.signal)))
      .then((rs) =>
        setReports((prev) => {
          const next = new Map(prev);
          rs.forEach((r) => next.set(r.scan.id, r));
          return next;
        }),
      )
      .catch((e: unknown) => {
        if (!ctrl.signal.aborted) setReportError(e instanceof Error ? e.message : "Request failed.");
      });
    return () => ctrl.abort();
  }, [beforeId, afterId, reports]);

  const pick = (id: string) => setPicked({ before: beforeId, after: afterId, [slot]: id });

  if (error) {
    return (
      <main style={{ paddingTop: 28 }}>
        <ErrorState title="Couldn’t load this site’s history" message={error}>
          <Link to="/history">Try another URL</Link>
        </ErrorState>
      </main>
    );
  }
  if (!history || !model) {
    return (
      <main style={{ paddingTop: 28 }}>
        <Loading label="Loading history…" />
      </main>
    );
  }

  const failedCount = history.filter((h) => h.scan.status === "failed").length;
  const oldest = history[history.length - 1];
  // Matched against the scans' own stored URLs rather than the query param, so a
  // differently-spelled but equivalent URL ("sn0wy.dev", a trailing slash) still
  // resolves to the same normalised history the API returned.
  const isSelfAudit = hasCaseStudy() && history.some((h) => h.scan.url === CASE_STUDY.url);

  return (
    <main>
      <section className="page-head">
        <div className="page-head-text">
          <span className="eyebrow">Site history</span>
          <h1>{displayUrl(url)}</h1>
          {history.length > 0 && (
            <p className="page-meta">
              {history.length} scan{history.length === 1 ? "" : "s"} since{" "}
              {formatLongDate(new Date(oldest.scan.created_at))}
              {failedCount > 0 && ` · ${failedCount} failed`}
            </p>
          )}
        </div>
        <button type="button" className="pill pill-sm" onClick={() => rescan(url, { force: true })} disabled={busy}>
          <span>{busy ? "Starting…" : history.length ? "Scan again" : "Scan it now"}</span>
          <span className="pill-icon">{busy ? <SpinnerIcon /> : <RetryIcon size={20} />}</span>
        </button>
        {rescanError && (
          <p className="form-error" role="alert" style={{ flexBasis: "100%" }}>
            {rescanError}
          </p>
        )}
      </section>

      {history.length === 0 ? (
        <EmptyState title="No scans for this URL yet">
          <p>Scan it once to start its history, then again after each change to see the difference.</p>
        </EmptyState>
      ) : (
        <>
          <ScoreChart model={model} before={before} after={after} />

          {isSelfAudit && <SelfAudit history={history} />}

          <section className="compare" aria-labelledby="compare-h">
            <div className="compare-intro">
              <h2 id="compare-h" className="hist-h2-lg">
                Before and after
              </h2>
              <p>
                {done.length < 2
                  ? "Once this site has two finished scans, you can compare any pair here."
                  : "Pick a side, then choose a scan on the timeline."}
              </p>
            </div>

            {before && after && (
              <>
                <div className="slots">
                  {(["before", "after"] as const).map((side) => {
                    const s = side === "before" ? before : after;
                    const t = new Date(s.scan.created_at);
                    return (
                      <button
                        key={side}
                        type="button"
                        className="slot"
                        aria-pressed={slot === side}
                        onClick={() => setSlot(side)}
                      >
                        <span className="slot-text">
                          <span className="slot-label">
                            <span className={side === "before" ? "ring" : "dot"} aria-hidden="true" />
                            {side === "before" ? "Before" : "After"}
                          </span>
                          <span className="slot-when">
                            {formatShortDate(t)}, {formatTime(t)}
                          </span>
                        </span>
                        <span className="slot-score">{s.performance_score ?? "—"}</span>
                      </button>
                    );
                  })}
                </div>
                <Timeline model={model} beforeId={beforeId} afterId={afterId} slot={slot} onPick={pick} />
                <Comparison before={before} after={after} reports={reports} loadError={reportError} />
              </>
            )}
          </section>

          <section className="scans-section" aria-labelledby="scans-h">
            <div className="section-head">
              <h2 id="scans-h" className="hist-h2">
                All scans
              </h2>
              <span className="muted">Newest first</span>
            </div>
            <div className="scans-wrap">
              <table className="scans-table">
                <thead>
                  <tr>
                    <th scope="col">Scanned</th>
                    <th scope="col">Score</th>
                    <th scope="col">Duration</th>
                    <th scope="col">Status</th>
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((h) => {
                    const s = h.scan;
                    const t = new Date(s.created_at);
                    const isB = s.id === beforeId;
                    const isA = s.id === afterId;
                    const tag = isB && isA ? "Both" : isB ? "Before" : isA ? "After" : "";
                    return (
                      <tr key={s.id} className={tag ? "selected" : undefined}>
                        <td>
                          <span className="when">
                            <b>{formatShortDate(t)}</b>
                            <span className="t">{formatTime(t)}</span>
                            {tag && <span className="mini-tag">{tag}</span>}
                          </span>
                        </td>
                        <td>
                          {s.status === "done" ? (
                            <ScoreChip score={h.performance_score} size="sm" />
                          ) : (
                            <span className="mono" style={{ color: "#8A939A" }}>
                              —
                            </span>
                          )}
                        </td>
                        <td className="dur">{scanDuration(s)}</td>
                        <td>
                          <span className={`status-text${s.status === "failed" ? " failed" : ""}`} title={s.error ?? undefined}>
                            {statusLabel(s)}
                          </span>
                        </td>
                        <td>
                          {s.status === "done" ? (
                            <span className="row-actions">
                              {done.length > 1 && (
                                <button type="button" className="ghost ghost-sm ghost-outline" onClick={() => pick(s.id)}>
                                  Set as {slot === "before" ? "Before" : "After"}
                                </button>
                              )}
                              <Link to={`/scans/${s.id}/report`} className="ghost ghost-sm">
                                Report
                              </Link>
                            </span>
                          ) : (
                            <Link to={`/scans/${s.id}`} className="ghost ghost-sm">
                              {s.status === "failed" ? "Details" : "View"}
                            </Link>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </main>
  );
}

export function HistoryPage() {
  const [params] = useSearchParams();
  const url = params.get("url");
  return url ? <SiteHistory key={url} url={url} /> : <SiteSearch />;
}
