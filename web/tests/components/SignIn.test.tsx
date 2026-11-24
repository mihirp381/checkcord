// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConsoleShell, AdminOnly } from "../../src/components/ConsoleShell";
import SignIn from "../../src/components/SignIn";
import { setAuthToken, setCsrf } from "../../src/lib/api";
import { AuthProvider } from "../../src/lib/auth";
import nextConfig from "../../next.config";
import pkg from "../../package.json";
import { ADMIN_ME, Fail, MEMBER_ME, fakeKey, mockApi } from "../helpers";

const router = { replace: vi.fn(), push: vi.fn() };
let search = new URLSearchParams();
let pathname = "/console";

vi.mock("next/navigation", () => ({
  useRouter: () => router,
  usePathname: () => pathname,
  useSearchParams: () => search,
}));

const ON = { sign_in_enabled: true, login_url: "/api/v1/auth/login" };
const OFF = { sign_in_enabled: false, login_url: null };

const signInPage = () =>
  render(
    <AuthProvider>
      <SignIn />
    </AuthProvider>
  );

const consolePage = (child = <p>admin content</p>) =>
  render(
    <AuthProvider>
      <ConsoleShell>
        <AdminOnly>{child}</AdminOnly>
      </ConsoleShell>
    </AuthProvider>
  );

beforeEach(() => {
  router.replace.mockReset();
  router.push.mockReset();
  search = new URLSearchParams();
  pathname = "/console";
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  setAuthToken(null);
  setCsrf(null, null);
  vi.unstubAllEnvs();
});

describe("sign-in page", () => {
  it("explains a failed sign-in", async () => {
    search = new URLSearchParams("error=unknown_person");
    mockApi({ "GET /api/v1/auth/config": ON, "GET /api/v1/auth/me": new Fail(401) });
    signInPage();
    expect(await screen.findByText(/not set up in Checkcord yet/)).toBeTruthy();
  });

  it("offers the identity provider when sign-in is configured", async () => {
    mockApi({ "GET /api/v1/auth/config": ON, "GET /api/v1/auth/me": new Fail(401) });
    signInPage();
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.getByText("Or continue with an API key")).toBeTruthy();
  });

  it("explains when sign-in is not configured and still takes a key", async () => {
    mockApi({ "GET /api/v1/auth/config": OFF, "GET /api/v1/auth/me": new Fail(401) });
    signInPage();
    expect(await screen.findByText(/not set up on this server/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(screen.getByLabelText("API key")).toBeTruthy();
  });

  it("keeps a working key for this tab only and returns to the page wanted", async () => {
    search = new URLSearchParams({ next: "/console/keys" });
    const key = fakeKey();
    const { called } = mockApi({
      "GET /api/v1/auth/config": OFF,
      "GET /api/v1/auth/me": (c) =>
        c.headers.Authorization === `Bearer ${key}` ? { ...ADMIN_ME, auth: "key", csrf_token: null, csrf_header: null } : new Fail(401),
    });
    const stored = vi.spyOn(Storage.prototype, "setItem");
    signInPage();
    fireEvent.change(await screen.findByLabelText("API key"), { target: { value: key } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/console/keys"));
    expect(window.sessionStorage.getItem("checkcord.console.key")).toBe(key);
    expect(stored.mock.contexts.every((store) => store === window.sessionStorage)).toBe(true);
    stored.mockRestore();
    expect(called("GET /api/v1/auth/me").every((c) => !String(c.url).includes(key))).toBe(true);
  });

  it("says so when the key is refused", async () => {
    mockApi({ "GET /api/v1/auth/config": OFF, "GET /api/v1/auth/me": new Fail(401) });
    signInPage();
    fireEvent.change(await screen.findByLabelText("API key"), { target: { value: fakeKey() } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("That key was not accepted.")).toBeTruthy();
    expect(window.sessionStorage.getItem("checkcord.console.key")).toBeNull();
  });

  it("does not follow a next address off the console", async () => {
    search = new URLSearchParams({ next: "//elsewhere.example" });
    mockApi({ "GET /api/v1/auth/config": ON, "GET /api/v1/auth/me": ADMIN_ME });
    signInPage();
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/console"));
  });

  it("shows an error with a retry when the API cannot be reached", async () => {
    mockApi({});
    signInPage();
    expect(await screen.findByText(/Cannot reach the Checkcord API/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });
});

describe("console sign-in states", () => {
  it("sends a signed-out visitor to sign-in with the page wanted", async () => {
    window.history.replaceState(null, "", "/console/audit?user=x");
    mockApi({ "GET /api/v1/auth/config": ON, "GET /api/v1/auth/me": new Fail(401) });
    consolePage();
    await waitFor(() =>
      expect(router.replace).toHaveBeenCalledWith(`/sign-in?next=${encodeURIComponent("/console/audit?user=x")}`)
    );
  });

  it("shows an admin every section with their name and sign-out", async () => {
    vi.stubEnv("CHECKCORD_VERSION", nextConfig.env?.CHECKCORD_VERSION);
    mockApi({ "GET /api/v1/auth/config": ON, "GET /api/v1/auth/me": ADMIN_ME });
    consolePage();
    expect(await screen.findByText("admin content")).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "Console sections" });
    for (const name of ["Overview", "Connections", "Review", "Records", "People", "API keys", "Audit", "Audiences", "Pipeline", "Graph"]) {
      expect(nav.textContent).toContain(name);
    }
    expect(screen.getByText("admin@example.com")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
    expect(screen.getByRole("contentinfo").textContent).toBe(`Checkcord ${pkg.version}`);
  });

  it("shows a member only the graph and their account", async () => {
    mockApi({ "GET /api/v1/auth/config": ON, "GET /api/v1/auth/me": MEMBER_ME });
    consolePage();
    expect(await screen.findByText("This needs admin rights")).toBeTruthy();
    expect(screen.queryByText("admin content")).toBeNull();
    const links = screen.getByRole("navigation", { name: "Console sections" }).querySelectorAll("a");
    expect([...links].map((a) => a.textContent)).toEqual(["Graph", "Your account"]);
  });

  it("returns to sign-in when the session ends mid-visit", async () => {
    window.history.replaceState(null, "", "/console/keys");
    mockApi({ "GET /api/v1/auth/config": ON, "GET /api/v1/auth/me": ADMIN_ME });
    consolePage();
    await screen.findByText("admin content");
    act(() => {
      window.dispatchEvent(new CustomEvent("auth:revoked", { detail: 401 }));
    });
    await waitFor(() =>
      expect(router.replace).toHaveBeenCalledWith(`/sign-in?next=${encodeURIComponent("/console/keys")}&ended=1`)
    );
  });

  it("signs a session out with the CSRF header", async () => {
    const { called } = mockApi({
      "GET /api/v1/auth/config": ON,
      "GET /api/v1/auth/me": ADMIN_ME,
      "POST /api/v1/auth/logout": "",
    });
    consolePage();
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(called("POST /api/v1/auth/logout")).toHaveLength(1));
    expect(called("POST /api/v1/auth/logout")[0].headers["X-CSRF-Token"]).toBe("csrf-value");
    await waitFor(() => expect(router.replace).toHaveBeenCalled());
  });
});
