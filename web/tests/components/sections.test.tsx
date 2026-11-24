// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import type React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Account from "../../src/components/sections/Account";
import Audiences from "../../src/components/sections/Audiences";
import Audit from "../../src/components/sections/Audit";
import Connections from "../../src/components/sections/Connections";
import Graph from "../../src/components/sections/Graph";
import Keys from "../../src/components/sections/Keys";
import Overview from "../../src/components/sections/Overview";
import People, { PersonDetail } from "../../src/components/sections/People";
import Pipeline from "../../src/components/sections/Pipeline";
import RecordDetail from "../../src/components/sections/RecordDetail";
import Records from "../../src/components/sections/Records";
import Replacements from "../../src/components/sections/Replacements";
import Review from "../../src/components/sections/Review";
import { AuthProvider } from "../../src/lib/auth";
import * as f from "../fixtures";
import { ADMIN_ME, Fail, MEMBER_ME, mockApi, page, type Reply } from "../helpers";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/console",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("sigma", () => ({ default: class {} }));

afterEach(cleanup);

const A = "/api/v1/admin";

interface Case {
  name: string;
  ui: () => React.ReactElement;
  loaded: Record<string, Reply>;
  loadedText: (string | RegExp)[];
  empty: Record<string, Reply>;
  emptyText: (string | RegExp)[];
  failing: string;
}

const CASES: Case[] = [
  {
    name: "Overview",
    ui: () => <Overview />,
    loaded: { [`GET ${A}/health`]: f.health, [`GET ${A}/stats`]: f.stats, [`GET ${A}/usage`]: f.usage },
    loadedText: ["All checks pass", "Database", "slack", "17", "model-a", "$0.42", /1 with conflicts, 0 blocked, 1 errors; 2 of Checkcord's own skipped/, "Reference", "Records with removed instructions: 1"],
    empty: { [`GET ${A}/health`]: { ok: true, checks: [] }, [`GET ${A}/stats`]: f.emptyStats, [`GET ${A}/usage`]: f.emptyUsage },
    emptyText: ["No checks reported", "No events in this period", "No records yet", "No model calls in this period"],
    failing: `GET ${A}/health`,
  },
  {
    name: "Connections",
    ui: () => <Connections />,
    loaded: { [`GET ${A}/connections`]: page([f.connection]) },
    loadedText: ["acme/billing", "beta", "In review", "12"],
    empty: { [`GET ${A}/connections`]: page([]) },
    emptyText: ["Nothing is connected yet"],
    failing: `GET ${A}/connections`,
  },
  {
    name: "Review",
    ui: () => <Review />,
    loaded: { [`GET ${A}/review`]: page([f.held("r1", "Card numbers never reach logs")]) },
    loadedText: ["Card numbers never reach logs", /jira:acme.example.net · 1 held record/, "jira · PAY (1)"],
    empty: { [`GET ${A}/review`]: page([]) },
    emptyText: ["Nothing is waiting for review"],
    failing: `GET ${A}/review`,
  },
  {
    name: "Records",
    ui: () => <Records />,
    loaded: { [`GET ${A}/records`]: page([f.record], "next") },
    loadedText: ["Retries stay idempotent", "Advisory", "Load more"],
    empty: { [`GET ${A}/records`]: page([]) },
    emptyText: ["No records yet"],
    failing: `GET ${A}/records`,
  },
  {
    name: "Record page",
    ui: () => <RecordDetail id={f.record.id} />,
    loaded: { [`GET ${A}/records/${f.record.id}`]: f.recordDetail },
    loadedText: ["Retries stay idempotent", "3 people", "repo:acme/billing", "slack"],
    empty: { [`GET ${A}/records/${f.record.id}`]: new Fail(404, "no such live record") },
    emptyText: ["No record with this id"],
    failing: `GET ${A}/records/${f.record.id}`,
  },
  {
    name: "Replacements",
    ui: () => <Replacements />,
    loaded: { [`GET ${A}/records/suggestions`]: page([f.suggestion]) },
    loadedText: ["Quotes run in a nightly batch", /This supersedes the nightly batch/, /says it replaces/],
    empty: { [`GET ${A}/records/suggestions`]: page([]) },
    emptyText: ["No suggested replacements"],
    failing: `GET ${A}/records/suggestions`,
  },
  {
    name: "People",
    ui: () => <People />,
    loaded: { [`GET ${A}/users`]: page([f.person]) },
    loadedText: ["dev@example.com", "Active"],
    empty: { [`GET ${A}/users`]: page([]) },
    emptyText: ["No people yet"],
    failing: `GET ${A}/users`,
  },
  {
    name: "Person page",
    ui: () => <PersonDetail id={f.person.id} />,
    loaded: {
      [`GET ${A}/users/${f.person.id}`]: { ...f.person, keys: [f.apiKey], active_sessions: 1 },
      [`GET ${A}/sessions`]: page([f.session]),
    },
    loadedText: ["dev@example.com", "laptop agent", "Firefox on Linux"],
    empty: {
      [`GET ${A}/users/${f.person.id}`]: { ...f.person, keys: [], active_sessions: 0 },
      [`GET ${A}/sessions`]: page([]),
    },
    emptyText: ["No keys", "No active sessions"],
    failing: `GET ${A}/users/${f.person.id}`,
  },
  {
    name: "API keys",
    ui: () => <Keys />,
    loaded: { [`GET ${A}/keys`]: page([f.apiKey]) },
    loadedText: ["laptop agent", "dev@example.com"],
    empty: { [`GET ${A}/keys`]: page([]) },
    emptyText: ["No keys yet"],
    failing: `GET ${A}/keys`,
  },
  {
    name: "Audit",
    ui: () => <Audit />,
    loaded: { [`GET ${A}/audit`]: page([f.auditEntry]) },
    loadedText: ["graph.context", "dev@example.com", "key: laptop agent", "1 redacted"],
    empty: { [`GET ${A}/audit`]: page([]) },
    emptyText: ["No audit entries yet"],
    failing: `GET ${A}/audit`,
  },
  {
    name: "Audiences",
    ui: () => <Audiences />,
    loaded: { [`GET ${A}/audiences`]: page([f.audience]) },
    loadedText: ["C0PRIVATE", "Private", "4 of 5 members matched"],
    empty: { [`GET ${A}/audiences`]: page([]) },
    emptyText: ["No audiences stored yet"],
    failing: `GET ${A}/audiences`,
  },
  {
    name: "Pipeline",
    ui: () => <Pipeline />,
    loaded: { [`GET ${A}/events/failed`]: { ...page([f.failedEvent]), retrying: 2 }, [`GET ${A}/dlq`]: page([f.stream]) },
    loadedText: ["embedding call timed out", "checkcord:events.dlq", "2 hours", /2 events are still being retried/],
    empty: { [`GET ${A}/events/failed`]: { ...page([]), retrying: 0 }, [`GET ${A}/dlq`]: page([]) },
    emptyText: ["No failed events", "No dead-letter streams"],
    failing: `GET ${A}/events/failed`,
  },
  {
    name: "Graph",
    ui: () => <Graph />,
    loaded: { "GET /api/v1/graph/repositories": { items: [{ repo_identifier: "acme/a", files: 3 }, { repo_identifier: "acme/b", files: 1 }] } },
    loadedText: ["Choose a repository to open its graph", "acme/a (3 files)"],
    empty: { "GET /api/v1/graph/repositories": { items: [] } },
    emptyText: ["No repository has a code graph yet"],
    failing: "GET /api/v1/graph/repositories",
  },
];

const expectText = async (texts: (string | RegExp)[]) => {
  for (const t of texts) expect((await screen.findAllByText(t)).length).toBeGreaterThan(0);
};

describe.each(CASES)("$name", (c) => {
  it("shows what it loaded", async () => {
    mockApi(c.loaded);
    render(c.ui());
    await expectText(c.loadedText);
  });

  it("says when there is nothing", async () => {
    mockApi(c.empty);
    render(c.ui());
    await expectText(c.emptyText);
  });

  it("shows the error with a retry", async () => {
    mockApi({ ...c.loaded, [c.failing]: new Fail(500, "database unavailable") });
    render(c.ui());
    expect((await screen.findAllByRole("alert"))[0].textContent).toContain("Database unavailable");
    expect(screen.getAllByRole("button", { name: "Try again" }).length).toBeGreaterThan(0);
  });
});

describe("Connections status", () => {
  it("labels each connector and explains the label", async () => {
    const datadog = { ...f.connection, connection: "datadog:d1", provider: "datadog", identifier: "d1", status: "experimental" };
    const unknown = { ...f.connection, connection: "other:x", provider: "other", identifier: "x", status: null };
    mockApi({ [`GET ${A}/connections`]: page([f.connection, datadog, unknown]) });
    render(<Connections />);
    const badge = await screen.findByText("experimental");
    expect(badge.parentElement?.getAttribute("title")).toMatch(/Not yet verified against the live system/);
    expect(screen.getByText("beta").parentElement?.getAttribute("title")).toMatch(/keeps it current/);
    expect(screen.getAllByText(/^(beta|experimental)$/)).toHaveLength(2);
  });
});

describe("rendering server text", () => {
  it("shows record text as text, never as markup", async () => {
    const summary = '<img src="x" onerror="window.__owned=1"> Use <b>idempotent</b> retries';
    mockApi({ [`GET ${A}/records/${f.record.id}`]: { ...f.recordDetail, summary } });
    const { container } = render(<RecordDetail id={f.record.id} />);
    expect(await screen.findByText(summary)).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });
});

describe("Records list", () => {
  it("shows decisions by default and can switch to reference records", async () => {
    const { called } = mockApi({ [`GET ${A}/records`]: page([f.record]) });
    render(<Records />);
    expect(await screen.findByText("Retries stay idempotent")).toBeTruthy();
    const show = screen.getByLabelText("Show") as HTMLSelectElement;
    expect(show.value).toBe("decision");
    expect(called(`GET ${A}/records`)[0].params.kind).toBe("decision");
  });
});

describe("Record type", () => {
  it("lets an admin change the type and notes removed instructions", async () => {
    const summary = "Deploys need two approvals. [REMOVED:INSTRUCTIONS]";
    mockApi({
      [`GET ${A}/records/${f.record.id}`]: { ...f.recordDetail, summary },
      [`PATCH ${A}/records/${f.record.id}/type`]: { id: f.record.id, type: "constraint" },
    });
    render(<RecordDetail id={f.record.id} />);
    expect(await screen.findByText(/marks text that tried to give orders to AI tools/)).toBeTruthy();
    expect(screen.getByText("Save type")).toBeTruthy();
  });
});

describe("Pipeline without Redis", () => {
  it("explains that dead letters need Redis", async () => {
    mockApi({ [`GET ${A}/events/failed`]: { ...page([]), retrying: 0 }, [`GET ${A}/dlq`]: new Fail(503, "Redis is not available") });
    render(<Pipeline />);
    expect(await screen.findByText(/Dead letters live in Redis/)).toBeTruthy();
  });
});

describe("Account", () => {
  const withAuth = () =>
    render(
      <AuthProvider>
        <Account />
      </AuthProvider>
    );

  it("shows an admin's identity and session", async () => {
    mockApi({ "GET /api/v1/auth/config": { sign_in_enabled: true, login_url: "/api/v1/auth/login" }, "GET /api/v1/auth/me": ADMIN_ME });
    withAuth();
    expect(await screen.findByText("admin@example.com")).toBeTruthy();
    expect(screen.getByText("Admin")).toBeTruthy();
    expect(screen.getByText("Your identity provider")).toBeTruthy();
  });

  it("tells a member what they can open", async () => {
    mockApi({ "GET /api/v1/auth/config": { sign_in_enabled: true, login_url: "/api/v1/auth/login" }, "GET /api/v1/auth/me": MEMBER_ME });
    withAuth();
    expect(await screen.findByText("member@example.com")).toBeTruthy();
    expect(screen.getByText("Member")).toBeTruthy();
    expect(screen.getByText(/Admin sections need admin rights/)).toBeTruthy();
  });
});
