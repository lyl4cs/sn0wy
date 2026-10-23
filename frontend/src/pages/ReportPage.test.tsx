import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { mockFetch, scanFixture } from "../test/http";
import { ReportPage, fixLimitFor, shortResource } from "./ReportPage";

const finding = (i: number) => ({
  id: i + 1,
  audit_id: `audit-${i}`,
  title: `Finding ${i}`,
  description: "Do the thing. [Learn more](https://web.dev/x).",
  score: 0.21,
  savings_ms: 2000 - i * 100,
  savings_bytes: i === 0 ? 412000 : null,
});

const report = {
  scan: scanFixture({ status: "done", attempts: 1, duration_ms: 41_000, finished_at: new Date().toISOString() }),
  performance_score: 54,
  metrics: { fcp_ms: 2140, lcp_ms: 4820, tbt_ms: 410, cls: 0.02, speed_index_ms: 5230 },
  findings: [0, 1, 2].map(finding),
};

const fix = (i: number) => ({
  audit_id: `audit-${i}`,
  title: `Finding ${i}`,
  diagnosis: `diag ${i}`,
  fix: `fix ${i}`,
  code: `code ${i}`,
  sources: ["https://web.dev/articles/optimize-lcp", "web.dev/js"],
  latency_ms: 3800,
  cost_usd: 0.0042,
});

function renderReport() {
  return render(
    <MemoryRouter initialEntries={["/scans/abc/report"]}>
      <Routes>
        <Route path="/scans/:id/report" element={<ReportPage />} />
        <Route path="/scans/:id" element={<p>SCAN PAGE</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("fixLimitFor", () => {
  it("asks for the top N that includes the finding, capped at 10", () => {
    expect(fixLimitFor(0)).toBe(1);
    expect(fixLimitFor(2)).toBe(3);
    expect(fixLimitFor(14)).toBe(10);
  });
});

describe("ReportPage", () => {
  it("renders score, metrics and findings in API order", async () => {
    mockFetch((u) => (u.pathname.endsWith("/report") ? { body: report } : undefined));
    renderReport();
    expect(await screen.findByRole("heading", { name: "northwind.shop" })).toBeInTheDocument();
    expect(screen.getByText("54")).toBeInTheDocument();
    expect(screen.getByText("4.8 s")).toBeInTheDocument();
    expect(screen.getByText("410 ms")).toBeInTheDocument();
    expect(screen.getByText("0.02")).toBeInTheDocument();
    expect(screen.getByText("3 findings, biggest estimated savings first.")).toBeInTheDocument();
    const titles = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(titles).toEqual(["Finding 0", "Finding 1", "Finding 2"]);
    expect(screen.getAllByRole("link", { name: "Learn more" })[0]).toHaveAttribute("href", "https://web.dev/x");
  });

  it("sends force:true when Scan again is pressed, so dedupe can't swallow it", async () => {
    const calls = mockFetch((u) =>
      u.pathname.endsWith("/report") ? { body: report } : { body: scanFixture({ id: "new" }), status: 202 },
    );
    renderReport();
    await screen.findByRole("heading", { name: "northwind.shop" });
    fireEvent.click(screen.getByRole("button", { name: /scan again/i }));
    await waitFor(() => expect(calls.mock.calls.some(([, i]) => i?.method === "POST")).toBe(true));
    const post = calls.mock.calls.find(([, i]) => i?.method === "POST")!;
    expect(JSON.parse(post[1]!.body as string)).toEqual({ url: "https://northwind.shop/", force: true });
  });

  it("shows the cooldown message when a forced re-scan is refused", async () => {
    mockFetch((u) =>
      u.pathname.endsWith("/report")
        ? { body: report }
        : { body: { detail: "This site was scanned moments ago. Try again in 41s." }, status: 429 },
    );
    renderReport();
    await screen.findByRole("heading", { name: "northwind.shop" });
    fireEvent.click(screen.getByRole("button", { name: /scan again/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 41s.");
  });

  it("explains why the timestamp can be older than the request", async () => {
    mockFetch((u) => (u.pathname.endsWith("/report") ? { body: report } : undefined));
    renderReport();
    await screen.findByRole("heading", { name: "northwind.shop" });
    fireEvent.click(screen.getByRole("button", { name: /what is this timestamp/i }));
    expect(screen.getByRole("note")).toHaveTextContent(/already scanned in the last few minutes/);
  });

  it("requests limit=index+1 and caches the fixes it got back", async () => {
    const calls = mockFetch((u) => {
      if (u.pathname.endsWith("/report")) return { body: report };
      if (u.pathname.endsWith("/fixes")) {
        const n = Number(u.searchParams.get("limit"));
        return { body: [0, 1, 2].slice(0, n).map(fix) };
      }
    });
    renderReport();
    fireEvent.click(await screen.findByRole("button", { name: "Get AI fix for Finding 1" }));
    expect(await screen.findByText("diag 1")).toBeInTheDocument();
    expect(screen.getByText("code 1")).toBeInTheDocument();
    expect(screen.getByText("AI fix · written in 3.8 s · $0.0042")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /web\.dev\/articles\/optimize-lcp/ })[0]).toHaveAttribute(
      "href",
      "https://web.dev/articles/optimize-lcp",
    );

    // Finding 0 was in that response: opening it must not hit the API again.
    fireEvent.click(screen.getByRole("button", { name: "Get AI fix for Finding 0" }));
    expect(await screen.findByText("diag 0")).toBeInTheDocument();
    const fixCalls = calls.mock.calls.map(([u]) => String(u)).filter((u) => u.includes("/fixes"));
    expect(fixCalls).toEqual(["http://localhost:8000/scans/abc/fixes?limit=2"]);

    fireEvent.click(screen.getAllByRole("button", { name: /hide fix/i })[0]);
    await waitFor(() => expect(screen.queryByText("diag 0")).not.toBeInTheDocument());
  });

  it("explains a 503 (AI not configured) inline", async () => {
    mockFetch((u) =>
      u.pathname.endsWith("/report")
        ? { body: report }
        : { status: 503, body: { detail: "AI not configured (PROJECT_ID/DATABASE_URL)" } },
    );
    renderReport();
    fireEvent.click(await screen.findByRole("button", { name: "Get AI fix for Finding 0" }));
    expect(await screen.findByText(/AI fixes aren’t switched on/)).toBeInTheDocument();
  });

  it("sends you to the scan page when the report isn't ready (409)", async () => {
    mockFetch(() => ({ status: 409, body: { detail: "scan is running, no report yet" } }));
    renderReport();
    expect(await screen.findByText("SCAN PAGE")).toBeInTheDocument();
  });
});

describe("v2 evidence on the report", () => {
  it("shortens resource URLs to host + file", () => {
    expect(shortResource("https://cdn.x.com/a/b/hero-1920.jpg?v=3")).toBe("cdn.x.com/…/hero-1920.jpg");
    expect(shortResource("https://x.com/app.js")).toBe("x.com/app.js");
    expect(shortResource("not a url")).toBe("not a url");
  });

  it("shows the detected platform and the worst files per finding", async () => {
    const withEvidence = {
      ...report,
      platform: { id: "wordpress", name: "WordPress", evidence: ["Lighthouse detected WordPress"] },
      findings: [
        {
          ...finding(0),
          offenders: [0, 1, 2, 3].map((i) => ({
            url: `https://shop.test/img/hero-${i}.jpg`,
            wasted_kb: 400 - i * 50,
            wasted_ms: null,
            total_kb: 500,
          })),
        },
        finding(1),
      ],
    };
    mockFetch((u) => (u.pathname.endsWith("/report") ? { body: withEvidence } : undefined));
    renderReport();
    expect(await screen.findByText("WordPress")).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: /hero-\d\.jpg/ });
    expect(links.map((a) => a.textContent)).toEqual([
      "shop.test/…/hero-0.jpg",
      "shop.test/…/hero-1.jpg",
      "shop.test/…/hero-2.jpg",
    ]);
    expect(screen.getByText("−400 KB")).toBeInTheDocument();
    expect(screen.getAllByText("Worst files")).toHaveLength(1); // finding 1 has no offenders
  });
});

describe("fix panel polish", () => {
  it("renders inline code in diagnosis and warns about unverified paths", async () => {
    mockFetch((u) => {
      if (u.pathname.endsWith("/report")) return { body: report };
      if (u.pathname.endsWith("/fixes"))
        return {
          body: [
            {
              ...fix(0),
              diagnosis: "Worst is `model-lifestyle.jpg` at 135 KB.",
              unverified_urls: ["https://northwind.shop/images/model-lifestyle.webp"],
            },
          ],
        };
      return undefined;
    });
    renderReport();
    fireEvent.click((await screen.findAllByRole("button", { name: /Get AI fix/ }))[0]);
    const codeEl = await screen.findByText("model-lifestyle.jpg");
    expect(codeEl.tagName).toBe("CODE"); // no literal backticks
    expect(screen.getByText(/Check these paths before using the code/)).toBeInTheDocument();
    expect(screen.getByText("https://northwind.shop/images/model-lifestyle.webp")).toBeInTheDocument();
  });

  it("shows where the fix goes, with inline code rendered", async () => {
    mockFetch((u) => {
      if (u.pathname.endsWith("/report")) return { body: report };
      if (u.pathname.endsWith("/fixes"))
        return {
          body: [
            {
              ...fix(0),
              where: "`functions.php` in a CHILD theme, using `wp_dequeue_script`.",
            },
          ],
        };
      return undefined;
    });
    renderReport();
    fireEvent.click((await screen.findAllByRole("button", { name: /Get AI fix/ }))[0]);
    expect(await screen.findByText("Where to put it")).toBeInTheDocument();
    expect(screen.getByText("functions.php").tagName).toBe("CODE");
  });

  it("hides the where block when the server sends nothing", async () => {
    mockFetch((u) => {
      if (u.pathname.endsWith("/report")) return { body: report };
      if (u.pathname.endsWith("/fixes")) return { body: [{ ...fix(0), where: "" }] };
      return undefined;
    });
    renderReport();
    fireEvent.click((await screen.findAllByRole("button", { name: /Get AI fix/ }))[0]);
    expect(await screen.findByText("diag 0")).toBeInTheDocument();
    expect(screen.queryByText("Where to put it")).not.toBeInTheDocument();
  });

  it("shows no warning when every path checks out", async () => {
    mockFetch((u) => {
      if (u.pathname.endsWith("/report")) return { body: report };
      if (u.pathname.endsWith("/fixes")) return { body: [{ ...fix(0), unverified_urls: [] }] };
      return undefined;
    });
    renderReport();
    fireEvent.click((await screen.findAllByRole("button", { name: /Get AI fix/ }))[0]);
    expect(await screen.findByText("diag 0")).toBeInTheDocument();
    expect(screen.queryByText(/Check these paths/)).not.toBeInTheDocument();
  });
});
