import { describe, expect, it, vi } from "vitest";
import { ApiError, NETWORK_ERROR, createApi, errorMessage } from "./api";
import { jsonResponse } from "./test/http";

function setup(response: Response | Error | DOMException) {
  const fetchFn = vi.fn(async () => {
    if (!(response instanceof Response)) throw response;
    return response;
  });
  return { api: createApi("https://api.test/", fetchFn), fetchFn };
}

describe("createApi", () => {
  it("POSTs a JSON body to /scans and strips the trailing slash from the base", async () => {
    const { api, fetchFn } = setup(jsonResponse({ id: "1", status: "queued" }, 202));
    const { scan, reused } = await api.createScan("https://a.com");
    expect(scan.id).toBe("1");
    expect(reused).toBe(false); // 202: a new scan was queued
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/scans");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ url: "https://a.com" });
    expect((init.headers as Record<string, string>)["content-type"]).toBe("application/json");
  });

  it("reports a 200 from /scans as a reused scan, and only sends force when asked", async () => {
    const { api, fetchFn } = setup(jsonResponse({ id: "1", status: "done" }, 200));
    const { reused } = await api.createScan("https://a.com");
    expect(reused).toBe(true); // 200: nothing was re-measured
    const [, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ url: "https://a.com" });
  });

  it("sends force:true when the caller insists on a fresh measurement", async () => {
    const { api, fetchFn } = setup(jsonResponse({ id: "2", status: "queued" }, 202));
    await api.createScan("https://a.com", { force: true });
    const [, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ url: "https://a.com", force: true });
  });

  it("builds every GET path, encoding ids and URLs", async () => {
    const { api, fetchFn } = setup(jsonResponse([]));
    await api.getScan("a/b");
    await api.getReport("x");
    await api.getFixes("x", 3);
    await api.siteHistory("https://a.com/?q=1");
    const urls = fetchFn.mock.calls.map((c) => (c as unknown as [string])[0]);
    expect(urls).toEqual([
      "https://api.test/scans/a%2Fb",
      "https://api.test/scans/x/report",
      "https://api.test/scans/x/fixes?limit=3",
      "https://api.test/sites/scans?url=https%3A%2F%2Fa.com%2F%3Fq%3D1&limit=100",
    ]);
  });

  it("turns HTTP errors into ApiError with status and FastAPI's detail", async () => {
    const { api } = setup(jsonResponse({ detail: "scan is running, no report yet" }, 409));
    const err = await api.getReport("x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.message).toBe("scan is running, no report yet");
  });

  it("handles non-JSON error bodies", async () => {
    const { api } = setup(new Response("<html>bad gateway</html>", { status: 502 }));
    const err = await api.getScan("x").catch((e) => e);
    expect(err.status).toBe(502);
    expect(err.message).toBe("Request failed (HTTP 502).");
  });

  it("reports network failures as status 0 with the API URL", async () => {
    const { api } = setup(new TypeError("Failed to fetch"));
    const err = await api.getScan("x").catch((e) => e);
    expect(err.status).toBe(NETWORK_ERROR);
    expect(err.message).toContain("https://api.test");
  });

  it("lets aborts through untouched", async () => {
    const { api } = setup(new DOMException("aborted", "AbortError"));
    const err = await api.getScan("x").catch((e) => e);
    expect(err.name).toBe("AbortError");
  });
});

describe("errorMessage", () => {
  it("reads string and validation-list details", () => {
    expect(errorMessage({ detail: "nope" }, "f")).toBe("nope");
    expect(errorMessage({ detail: [{ msg: "Input should be a valid URL", loc: ["body", "url"] }] }, "f")).toBe(
      "Input should be a valid URL",
    );
    expect(errorMessage(null, "fallback")).toBe("fallback");
  });
});
