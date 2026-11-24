// @vitest-environment jsdom
import { AxiosError, type InternalAxiosRequestConfig } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  api,
  apiBaseUrl,
  getHealth,
  getMe,
  listDeadLetters,
  listKeys,
  listRecords,
  loginUrl,
  releaseHeld,
  revokeKey,
  setAuthToken,
  setCsrf,
} from "../../src/lib/api";
import { Fail, bodyOf, fakeKey, mockApi, page } from "../helpers";

const rejectWith = (status: number) => (config: InternalAxiosRequestConfig) =>
  Promise.reject(
    new AxiosError("failed", "ERR_BAD_REQUEST", config, null, {
      status,
      statusText: "",
      data: {},
      headers: {},
      config,
    })
  );

describe("api auth revocation", () => {
  const listener = vi.fn();

  afterEach(() => {
    window.removeEventListener("auth:revoked", listener);
    listener.mockReset();
  });

  it("announces a revoked session on 403 by default", async () => {
    window.addEventListener("auth:revoked", listener);
    await expect(api.get("/x", { adapter: rejectWith(403) })).rejects.toBeInstanceOf(AxiosError);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("leaves the session alone when the caller handles the 403", async () => {
    window.addEventListener("auth:revoked", listener);
    await expect(
      api.patch("/x", {}, { adapter: rejectWith(403), skipAuthRevoked: true })
    ).rejects.toBeInstanceOf(AxiosError);
    expect(listener).not.toHaveBeenCalled();
  });
});

describe("api client", () => {
  afterEach(() => {
    setAuthToken(null);
    setCsrf(null, null);
    document.head.innerHTML = "";
  });

  it("sends cookies and reads the API address from the page", async () => {
    document.head.innerHTML = '<meta name="checkcord-api-url" content="https://checkcord.example.test/">';
    const { calls } = mockApi({ "GET /api/v1/admin/health": { ok: true, checks: [] } });
    await getHealth();
    expect(calls[0].withCredentials).toBe(true);
    expect(calls[0].baseURL).toBe("https://checkcord.example.test");
    expect(apiBaseUrl()).toBe("https://checkcord.example.test");
  });

  it("adds the CSRF header only to requests that change state", async () => {
    setCsrf("X-CSRF-Token", "csrf-1");
    const { called } = mockApi({
      "GET /api/v1/admin/keys": page([]),
      "POST /api/v1/admin/keys/k1/revoke": { id: "k1", revoked: true },
    });
    await listKeys();
    await revokeKey("k1");
    expect(called("GET /api/v1/admin/keys")[0].headers["X-CSRF-Token"]).toBeUndefined();
    expect(called("POST /api/v1/admin/keys/k1/revoke")[0].headers["X-CSRF-Token"]).toBe("csrf-1");
  });

  it("uses a bearer key instead of the CSRF header when a key is set", async () => {
    const key = fakeKey();
    setAuthToken(key);
    setCsrf("X-CSRF-Token", "csrf-1");
    const { calls } = mockApi({ "POST /api/v1/admin/keys/k1/revoke": { id: "k1", revoked: true } });
    await revokeKey("k1");
    expect(calls[0].headers.Authorization).toBe(`Bearer ${key}`);
    expect(calls[0].headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("drops empty filters and encodes path parts", async () => {
    const { calls } = mockApi({
      "GET /api/v1/admin/records": page([]),
      "GET /api/v1/admin/dlq/checkcord%3Aevents.dlq/entries": page([]),
    });
    await listRecords({ q: "", type: "adr", severity: undefined });
    await listDeadLetters("checkcord:events.dlq");
    expect(calls[0].params).toEqual({ type: "adr", limit: 50 });
    expect(calls).toHaveLength(2);
  });

  it("sends a batch review selection as record_ids", async () => {
    const { calls } = mockApi({ "POST /api/v1/admin/review/release": { events: 1, records: 2, record_ids: ["a", "b"] } });
    await releaseHeld({ record_ids: ["a", "b"] });
    expect(bodyOf(calls[0])).toEqual({ record_ids: ["a", "b"] });
  });

  it("builds the sign-in link against the API with an absolute return address", () => {
    document.head.innerHTML = '<meta name="checkcord-api-url" content="https://api.example.test">';
    const url = new URL(loginUrl({ sign_in_enabled: true, login_url: "/api/v1/auth/login" }, "https://web.example.test/console/keys"));
    expect(url.origin).toBe("https://api.example.test");
    expect(url.pathname).toBe("/api/v1/auth/login");
    expect(url.searchParams.get("return_to")).toBe("https://web.example.test/console/keys");
  });

  it("announces 401 and keeps the sign-in check itself quiet", async () => {
    const listener = vi.fn();
    window.addEventListener("auth:revoked", listener);
    mockApi({ "GET /api/v1/admin/keys": new Fail(401), "GET /api/v1/auth/me": new Fail(401) });
    await expect(listKeys()).rejects.toBeInstanceOf(AxiosError);
    await expect(getMe()).rejects.toBeInstanceOf(AxiosError);
    window.removeEventListener("auth:revoked", listener);
    expect(listener).toHaveBeenCalledTimes(1);
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toBe(401);
  });
});
