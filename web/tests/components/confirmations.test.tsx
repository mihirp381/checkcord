// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Audiences from "../../src/components/sections/Audiences";
import Connections from "../../src/components/sections/Connections";
import Keys from "../../src/components/sections/Keys";
import People, { PersonDetail } from "../../src/components/sections/People";
import Pipeline from "../../src/components/sections/Pipeline";
import RecordDetail from "../../src/components/sections/RecordDetail";
import Records from "../../src/components/sections/Records";
import Replacements from "../../src/components/sections/Replacements";
import Review from "../../src/components/sections/Review";
import * as f from "../fixtures";
import { bodyOf, mockApi, page } from "../helpers";

const router = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  usePathname: () => "/console",
  useSearchParams: () => new URLSearchParams(),
}));

const A = "/api/v1/admin";
const nativeDialogs = ["alert", "confirm", "prompt"] as const;

beforeEach(() => {
  router.push.mockReset();
  for (const name of nativeDialogs) vi.spyOn(window, name).mockImplementation(() => undefined as never);
});

afterEach(() => {
  for (const name of nativeDialogs) expect(window[name]).not.toHaveBeenCalled();
  vi.restoreAllMocks();
  cleanup();
});

const dialog = () => screen.getByRole("dialog");

const cancelThenConfirm = async (open: () => void, confirmLabel: string, check: (box: HTMLElement) => void) => {
  open();
  const first = await screen.findByRole("dialog");
  check(first);
  fireEvent.click(within(first).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  open();
  const box = await screen.findByRole("dialog");
  fireEvent.click(within(box).getByRole("button", { name: confirmLabel }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
};

describe("dialogs", () => {
  it("move focus inside and close on Escape without acting", async () => {
    const { called } = mockApi({ [`GET ${A}/keys`]: page([f.apiKey]) });
    render(<Keys />);
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    const box = await screen.findByRole("dialog");
    expect(box.getAttribute("aria-modal")).toBe("true");
    expect(box.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(called(`POST ${A}/keys/${f.apiKey.id}/revoke`)).toHaveLength(0);
  });
});

describe("destructive actions ask first", () => {
  it("revoking a key names it", async () => {
    const revoke = `POST ${A}/keys/${f.apiKey.id}/revoke`;
    const { called } = mockApi({ [`GET ${A}/keys`]: page([f.apiKey]), [revoke]: { id: f.apiKey.id, revoked: true } });
    render(<Keys />);
    await screen.findByText("laptop agent");
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Revoke" })),
      "Revoke key",
      (box) => {
        expect(box.textContent).toContain('Revoke the key "laptop agent"?');
        expect(box.textContent).toContain("dev@example.com");
        expect(called(revoke)).toHaveLength(0);
      }
    );
    expect(called(revoke)).toHaveLength(1);
    expect(await screen.findByText('Revoked the key "laptop agent".')).toBeTruthy();
  });

  it("deleting a record shows what goes with it", async () => {
    const del = `DELETE ${A}/records/${f.record.id}`;
    const { called } = mockApi({
      [`GET ${A}/records/${f.record.id}`]: f.recordDetail,
      [del]: (c) => ({ ...f.purgeCounts, dry_run: Boolean(c.params?.dry_run) }),
    });
    render(<RecordDetail id={f.record.id} />);
    await screen.findByText("Retries stay idempotent");
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Delete record" })),
      "Delete record",
      (box) => expect(box.textContent).toContain(f.record.id)
    );
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/console/records"));
    const real = called(del).filter((c) => !c.params?.dry_run);
    expect(real).toHaveLength(1);
    expect(called(del).length).toBeGreaterThan(1);
  });

  it("retiring a record asks first and sends the replacement and reason", async () => {
    const retire = `POST ${A}/records/${f.record.id}/retire`;
    const other = f.suggestion.replacement_id;
    let retired = false;
    const { called } = mockApi({
      [`GET ${A}/records/${f.record.id}`]: () =>
        retired ? { ...f.recordDetail, state: "retired", retired_at: f.recordDetail.created_at, retired_by: "admin@example.com", replaced_by: other } : f.recordDetail,
      [retire]: () => {
        retired = true;
        return { id: f.record.id, state: "retired" };
      },
    });
    render(<RecordDetail id={f.record.id} />);
    await screen.findByText("Retries stay idempotent");
    fireEvent.click(screen.getByRole("button", { name: "Retire record" }));
    const box = await screen.findByRole("dialog");
    expect(called(retire)).toHaveLength(0);
    fireEvent.change(within(box).getByLabelText("Replaced by (optional record id)"), { target: { value: ` ${other} ` } });
    fireEvent.change(within(box).getByLabelText("Reason (optional)"), { target: { value: "moved to live quotes" } });
    fireEvent.click(within(box).getByRole("button", { name: "Retire record" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(bodyOf(called(retire)[0])).toEqual({ replaced_by: other, reason: "moved to live quotes" });
    expect(await screen.findByRole("button", { name: "Restore record" })).toBeTruthy();
    expect(screen.getAllByText("Retired").length).toBeGreaterThan(0);
  });

  it("a retired record restores without a dialog", async () => {
    const restore = `POST ${A}/records/${f.record.id}/restore`;
    const { called } = mockApi({
      [`GET ${A}/records/${f.record.id}`]: { ...f.recordDetail, state: "retired", retired_at: f.recordDetail.created_at, retired_by: "key:ops", retired_reason: "stale" },
      [restore]: { id: f.record.id, state: "live" },
    });
    render(<RecordDetail id={f.record.id} />);
    expect(await screen.findByText("stale")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Restore record" }));
    await waitFor(() => expect(called(restore)).toHaveLength(1));
    expect(await screen.findByText(/Restored\./)).toBeTruthy();
  });

  it("a record page links its suggested replacement", async () => {
    mockApi({
      [`GET ${A}/records/${f.record.id}`]: {
        ...f.recordDetail,
        suggested_replacements: [{ id: f.suggestion.id, replacement_id: f.suggestion.replacement_id, basis: "stated" }],
      },
    });
    render(<RecordDetail id={f.record.id} />);
    expect(await screen.findByText(f.suggestion.replacement_id)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Replacements" }).getAttribute("href")).toBe("/console/replacements");
  });

  it("a record page lists updates and notes a newer record that may replace it", async () => {
    mockApi({
      [`GET ${A}/records/${f.record.id}`]: {
        ...f.recordDetail,
        confidence_score: 7.5,
        updates: [
          { happened_at: f.recordDetail.created_at, source: { platform: "jira", type: "ticket_update" }, text: "Refunds follow the same rule." },
          { happened_at: f.recordDetail.created_at, source: { platform: "slack", type: "message" }, text: "Shipped in the retry worker." },
        ],
        may_be_replaced_by: { record_id: f.suggestion.replacement_id, created_at: f.recordDetail.created_at, basis: "model", summary: "Retries move to the queue" },
      },
    });
    render(<RecordDetail id={f.record.id} />);
    const updates = await screen.findByRole("region", { name: "Updates" });
    expect(within(updates).getAllByRole("listitem")).toHaveLength(2);
    expect(within(updates).getByText("Refunds follow the same rule.")).toBeTruthy();
    const note = screen.getByRole("note");
    expect(note.textContent).toMatch(/may replace this; an admin has not confirmed it yet/);
    expect(within(note).getByRole("link", { name: "Retries move to the queue" }).getAttribute("href")).toBe(
      `/console/records/${f.suggestion.replacement_id}`,
    );
    expect(screen.getByText("7.50")).toBeTruthy();
  });

  it("a record page without updates or a note shows neither", async () => {
    mockApi({ [`GET ${A}/records/${f.record.id}`]: f.recordDetail });
    render(<RecordDetail id={f.record.id} />);
    await screen.findByText(f.recordDetail.summary);
    expect(screen.queryByRole("region", { name: "Updates" })).toBeNull();
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("accepting a replacement asks first; dismissing does not", async () => {
    const accept = `POST ${A}/records/suggestions/${f.suggestion.id}/accept`;
    const dismiss = `POST ${A}/records/suggestions/${f.suggestion.id}/dismiss`;
    const { called } = mockApi({
      [`GET ${A}/records/suggestions`]: page([f.suggestion, { ...f.suggestion, id: "s2", record_summary: "Second older" }]),
      [accept]: { suggestion: f.suggestion.id, decision: "accept" },
      [`POST ${A}/records/suggestions/s2/dismiss`]: { suggestion: "s2", decision: "dismiss" },
    });
    render(<Replacements />);
    await screen.findByText("Second older");
    await cancelThenConfirm(
      () => fireEvent.click(screen.getAllByRole("button", { name: "Accept" })[0]),
      "Accept and retire",
      (box) => {
        expect(box.textContent).toContain("Quotes run in a nightly batch");
        expect(called(accept)).toHaveLength(0);
      }
    );
    expect(called(accept)).toHaveLength(1);
    expect(await screen.findByText(/older record is retired/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Keep both" }));
    await waitFor(() => expect(called(`POST ${A}/records/suggestions/s2/dismiss`)).toHaveLength(1));
    expect(called(dismiss)).toHaveLength(0);
    expect(await screen.findByText("No suggested replacements")).toBeTruthy();
  });

  it("the records list filters by retired", async () => {
    const { called } = mockApi({
      [`GET ${A}/records`]: (c) =>
        page(c.params?.state === "retired" ? [{ ...f.record, state: "retired", summary: "Old rule" }] : [f.record]),
    });
    render(<Records />);
    await screen.findByText("Retries stay idempotent");
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "retired" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText("Old rule")).toBeTruthy();
    expect(screen.getAllByText("Retired").length).toBe(2);
    expect(screen.getByRole("link", { name: "Old rule" }).getAttribute("href")).toBe(`/console/records/${f.record.id}`);
    expect(called(`GET ${A}/records`).at(-1)?.params?.state).toBe("retired");
  });

  it("the preview lists the counts the API reports", async () => {
    mockApi({
      [`GET ${A}/records/${f.record.id}`]: f.recordDetail,
      [`DELETE ${A}/records/${f.record.id}`]: f.purgeCounts,
    });
    render(<RecordDetail id={f.record.id} />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete record" }));
    expect(await within(dialog()).findByText("3 events")).toBeTruthy();
    expect(within(dialog()).getByText("2 records deleted")).toBeTruthy();
  });

  it("deleting and deactivating a person name them", async () => {
    const del = `DELETE ${A}/users/${f.person.id}`;
    const deactivate = `POST ${A}/users/${f.person.id}/deactivate`;
    const { called } = mockApi({
      [`GET ${A}/users/${f.person.id}`]: { ...f.person, keys: [f.apiKey], active_sessions: 1 },
      [`GET ${A}/sessions`]: page([f.session]),
      [del]: f.purgeCounts,
      [deactivate]: { ...f.person, active: false, sessions_revoked: 1 },
    });
    render(<PersonDetail id={f.person.id} />);
    await screen.findByText("laptop agent");
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Deactivate" })),
      "Deactivate",
      (box) => expect(box.textContent).toContain("Deactivate dev@example.com?")
    );
    expect(called(deactivate)).toHaveLength(1);
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Delete" })),
      "Delete person",
      (box) => expect(box.textContent).toContain("Delete dev@example.com?")
    );
    expect(called(del).filter((c) => !c.params?.dry_run)).toHaveLength(1);
  });

  it("revoking a session says whose and where", async () => {
    const revoke = `POST ${A}/sessions/${f.session.id}/revoke`;
    const { called } = mockApi({
      [`GET ${A}/users/${f.person.id}`]: { ...f.person, keys: [], active_sessions: 1 },
      [`GET ${A}/sessions`]: page([f.session]),
      [revoke]: { id: f.session.id, revoked: true },
    });
    render(<PersonDetail id={f.person.id} />);
    await screen.findByText("Firefox on Linux");
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Revoke" })),
      "Revoke session",
      (box) => expect(box.textContent).toMatch(/dev@example.com.*Firefox on Linux/)
    );
    expect(called(revoke)).toHaveLength(1);
  });

  it("disconnecting offers to keep or delete content", async () => {
    const disc = `POST ${A}/connections/disconnect`;
    const { called } = mockApi({
      [`GET ${A}/connections`]: page([f.connection]),
      [disc]: (c) => ({ disconnected: !bodyOf(c).dry_run, purged: bodyOf(c).purge ? f.purgeCounts : null }),
    });
    render(<Connections />);
    await screen.findByText("acme/billing");
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Disconnect" })),
      "Disconnect",
      (box) => expect(box.textContent).toContain("Disconnect github:acme/billing?")
    );
    expect(bodyOf(called(disc)[0])).toEqual({ connection: "github:acme/billing", purge: false, dry_run: false });

    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByLabelText(/Delete it/));
    expect(await within(dialog()).findByText("3 events")).toBeTruthy();
    fireEvent.click(within(dialog()).getByRole("button", { name: "Disconnect and delete content" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const bodies = called(disc).map(bodyOf);
    expect(bodies).toContainEqual({ connection: "github:acme/billing", purge: true, dry_run: true });
    expect(bodies.at(-1)).toEqual({ connection: "github:acme/billing", purge: true, dry_run: false });
  });

  it("the push API has nothing to disconnect or scope", async () => {
    mockApi({ [`GET ${A}/connections`]: page([{ ...f.connection, connection: "custom:push", provider: "custom", identifier: "push", scope: null }]) });
    render(<Connections />);
    await screen.findByText("push");
    expect(screen.queryByRole("button", { name: "Disconnect" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Scope" })).toBeNull();
  });

  it("sets a connection's scope and says that stored content stays", async () => {
    const put = `PUT ${A}/connections/scope`;
    const { called } = mockApi({
      [`GET ${A}/connections`]: page([{ ...f.connection, scope_skipped: 4 }]),
      [put]: (c) => ({ connection: f.connection.connection, ...bodyOf(c), only_listed: false, skipped: 4, days: 7 }),
    });
    render(<Connections />);
    expect(await screen.findByText("Everything")).toBeTruthy();
    expect(screen.getByText("4 skipped in 7 days")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Scope" }));
    const box = await screen.findByRole("dialog");
    expect(box.textContent).toContain("What was already ingested stays");
    fireEvent.change(within(box).getByLabelText("Exclude"), { target: { value: "acme/billing\n" } });
    fireEvent.click(within(box).getByRole("button", { name: "Save scope" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(bodyOf(called(put)[0])).toEqual({ connection: "github:acme/billing", include: [], exclude: ["acme/billing"] });
    expect(screen.getByText("All but 1")).toBeTruthy();
    expect(screen.getByText(/applies to what arrives from now on/)).toBeTruthy();
  });

  it("rejecting a record, a selection or a connection says what is deleted", async () => {
    const reject = `POST ${A}/review/reject`;
    const { called } = mockApi({
      [`GET ${A}/review`]: page([f.held("r1", "First held"), f.held("r2", "Second held")]),
      [reject]: { events: 1, records_deleted: 1, records_updated: 0, meeting_segments: 0, code_symbols: 0, not_held: [] },
    });
    render(<Review />);
    await screen.findByText("First held");

    await cancelThenConfirm(
      () => fireEvent.click(screen.getAllByRole("button", { name: "Reject" })[0]),
      "Reject and delete",
      (box) => expect(box.textContent).toContain("Reject the record?")
    );
    await screen.findByText("Second held");
    fireEvent.click(screen.getByLabelText("Select record First held"));
    fireEvent.click(screen.getByLabelText("Select record Second held"));
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Reject selected" })),
      "Reject and delete",
      (box) => expect(box.textContent).toContain("Reject 2 selected records?")
    );
    await screen.findByText("First held");
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Reject all" })),
      "Reject and delete",
      (box) => expect(box.textContent).toContain("Reject everything held from jira:acme.example.net?")
    );
    expect(called(reject).map(bodyOf)).toEqual([
      { record_id: "r1" },
      { record_ids: ["r1", "r2"] },
      { connection: "jira:acme.example.net" },
    ]);
  });

  it("dropping dead letters names the stream", async () => {
    const drop = `POST ${A}/dlq/checkcord%3Aevents.dlq/drop`;
    const { called } = mockApi({
      [`GET ${A}/events/failed`]: { ...page([]), retrying: 0 },
      [`GET ${A}/dlq`]: page([f.stream]),
      [`GET ${A}/dlq/checkcord%3Aevents.dlq/entries`]: page([f.deadLetter]),
      [drop]: { stream: f.stream.name, done: [f.deadLetter.entry_id] },
    });
    render(<Pipeline />);
    fireEvent.click(await screen.findByRole("button", { name: "Show entries" }));
    await screen.findByText(f.deadLetter.entry_id);
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Drop all" })),
      "Drop",
      (box) => expect(box.textContent).toContain("Drop every entry in checkcord:events.dlq?")
    );
    expect(called(drop).map(bodyOf)).toEqual([{ all: true }]);
  });

  it("deleting an audience's content names the source", async () => {
    const purge = `POST ${A}/purge/source`;
    const { called } = mockApi({
      [`GET ${A}/audiences`]: page([f.audience]),
      [purge]: (c) => ({ ...f.purgeCounts, dry_run: bodyOf(c).dry_run }),
    });
    render(<Audiences />);
    await screen.findByText("C0PRIVATE");
    await cancelThenConfirm(
      () => fireEvent.click(screen.getByRole("button", { name: "Delete content" })),
      "Delete content",
      (box) => expect(box.textContent).toContain("Delete everything from C0PRIVATE?")
    );
    expect(called(purge).map(bodyOf).filter((b) => !b.dry_run)).toEqual([
      { platform: "slack", container: "C0PRIVATE", dry_run: false },
    ]);
  });
});

describe("accounts", () => {
  it("matching an account sends the email and says so", async () => {
    const link = `POST ${A}/accounts/link`;
    const account = {
      platform: "github",
      account: "rosa-gh",
      display_name: "Rosa",
      source_email: null,
      user_id: null,
      person_email: null,
      link_source: null,
      linked_at: null,
      linked_by: null,
      unmatched_in: 2,
    };
    const { called } = mockApi({
      [`GET ${A}/users`]: page([f.person]),
      [`GET ${A}/accounts`]: page([account]),
      [link]: { platform: "github", account: "rosa-gh", person_email: "rosa@example.com" },
    });
    render(<People />);
    fireEvent.click(await screen.findByRole("button", { name: "Match to a person" }));
    const box = await screen.findByRole("dialog");
    expect(box.textContent).toContain("gave no email");
    fireEvent.change(within(box).getByLabelText("Person's email"), { target: { value: " rosa@example.com " } });
    fireEvent.click(within(box).getByRole("button", { name: "Match" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(called(link).map(bodyOf)).toEqual([{ platform: "github", account: "rosa-gh", email: "rosa@example.com" }]);
    expect(await screen.findByText(/GitHub account rosa-gh is now rosa@example.com/)).toBeTruthy();
  });
});

describe("keys", () => {
  it("shows a new key once with a copy control", async () => {
    const secret = ["chk", "new", Math.random().toString(36).slice(2)].join("_");
    const { called } = mockApi({
      [`GET ${A}/keys`]: page([]),
      [`POST ${A}/keys`]: { id: "k9", name: "ci", is_admin: false, user_email: null, key: secret },
    });
    render(<Keys />);
    fireEvent.click(await screen.findByRole("button", { name: "Create key" }));
    fireEvent.change(within(dialog()).getByLabelText("Name"), { target: { value: "ci" } });
    fireEvent.click(within(dialog()).getByRole("button", { name: "Create key" }));
    const shown = await screen.findByRole("dialog", { name: 'Key "ci" created' });
    expect((within(shown).getByLabelText("API key") as HTMLInputElement).value).toBe(secret);
    expect(shown.textContent).toContain("shown only this once");
    expect(within(shown).getByRole("button", { name: "Copy" })).toBeTruthy();
    expect(bodyOf(called(`POST ${A}/keys`)[0])).toEqual({ name: "ci", admin: false, user_email: null, create_user: false });
    fireEvent.click(within(shown).getByRole("button", { name: "I have stored the key" }));
    await waitFor(() => expect(screen.queryByDisplayValue(secret)).toBeNull());
  });
});
