// @vitest-environment jsdom
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, cleanup } from "@testing-library/react";
import NodeInspectorPanel from "../../src/components/NodeInspectorPanel";
import { api } from "../../src/lib/api";

// Mock the api
vi.mock("../../src/lib/api", () => ({
  api: {
    get: vi.fn(),
    patch: vi.fn(),
  },
}));

describe("NodeInspectorPanel component tests", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("should show loading spinner initially", async () => {
    vi.mocked(api.get).mockReturnValue(new Promise(() => {})); // never resolves
    render(<NodeInspectorPanel nodeId="file:src/detail.py" repoId="test-repo" onClose={() => {}} />);
    expect(screen.getByTestId("loading-spinner")).toBeDefined();
  });

  it("should render file node details successfully", async () => {
    const mockData = {
      type: "file",
      id: "file:src/detail.py",
      file_path: "src/detail.py",
      repo_identifier: "test-repo",
      symbols: [
        { id: "sym-1", symbol_name: "detail_func", node_type: "function" }
      ]
    };
    vi.mocked(api.get).mockResolvedValue({ data: mockData });

    render(<NodeInspectorPanel nodeId="file:src/detail.py" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.queryByTestId("loading-spinner")).toBeNull();
    });

    expect(screen.getAllByText("src/detail.py").length).toBeGreaterThan(0);
    expect(screen.getByText("detail_func")).toBeDefined();
    expect(screen.getByText("function")).toBeDefined();
  });

  it("should render error state and handle retry", async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error("API Error"));

    const mockData = {
      type: "developer",
      id: "developer:Alice",
      display_name: "Alice",
      authored_events_count: 5
    };
    vi.mocked(api.get).mockResolvedValueOnce({ data: mockData });

    render(<NodeInspectorPanel nodeId="developer:Alice" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.getByTestId("error-message")).toBeDefined();
    });

    const retryBtn = screen.getByText("Retry");
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(screen.getByText("Alice")).toBeDefined();
    });
    expect(screen.getByText("5")).toBeDefined();
  });

  it("should show an Organization-wide badge and no lock icon for an org-global context", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-1",
      summary: "An org-wide decision",
      confidence_score: 8.5,
      scope_repo_identifiers: [],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockResolvedValue({ data: mockData });

    render(<NodeInspectorPanel nodeId="context:ctx-1" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.queryByTestId("loading-spinner")).toBeNull();
    });

    expect(screen.getByTestId("context-scope-badge")).toBeDefined();
    expect(screen.getByText("Organization-wide")).toBeDefined();
    expect(screen.queryByTestId("context-scope-lock-icon")).toBeNull();
  });

  it("should show a Scoped-to badge and a lock icon for a locked repo-scoped context", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-2",
      summary: "A repo-scoped, locked decision",
      confidence_score: 9.0,
      scope_repo_identifiers: ["acme/service-a"],
      is_locked: true,
      sources: [],
    };
    vi.mocked(api.get).mockResolvedValue({ data: mockData });

    render(<NodeInspectorPanel nodeId="context:ctx-2" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.queryByTestId("loading-spinner")).toBeNull();
    });

    expect(screen.getByText("Scoped to acme/service-a")).toBeDefined();
    expect(screen.getByTestId("context-scope-lock-icon")).toBeDefined();
    expect(screen.queryByTestId("context-scope-expand-toggle")).toBeNull();
  });

  it("should show a +N more badge for a multi-repo-scoped context, expandable to the full list", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-6",
      summary: "A shared-library ADR referenced from three services",
      confidence_score: 7.5,
      scope_repo_identifiers: ["acme/service-a", "acme/service-b", "acme/service-c"],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockResolvedValue({ data: mockData });

    render(<NodeInspectorPanel nodeId="context:ctx-6" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.queryByTestId("loading-spinner")).toBeNull();
    });

    expect(screen.getByText("Scoped to acme/service-a +2 more")).toBeDefined();
    expect(screen.queryByTestId("context-scope-full-list")).toBeNull();

    fireEvent.click(screen.getByTestId("context-scope-expand-toggle"));

    const fullList = screen.getByTestId("context-scope-full-list");
    expect(fullList.textContent).toContain("acme/service-a");
    expect(fullList.textContent).toContain("acme/service-b");
    expect(fullList.textContent).toContain("acme/service-c");
  });

  it("should render content_hash and last_verified_at without a redaction badge when nothing was redacted", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-3",
      summary: "A clean, unredacted decision",
      confidence_score: 7.0,
      scope_repo_identifiers: [],
      is_locked: false,
      content_hash: "abc123def456",
      last_verified_at: "2026-07-30T12:00:00Z",
      summary_redacted: false,
      sources: [
        {
          event_id: "ev-1",
          source_platform: "slack",
          source_type: "message",
          author: { display_name: "Alice" },
          happened_at: "2026-07-30T11:00:00Z",
        },
      ],
    };
    vi.mocked(api.get).mockResolvedValue({ data: mockData });

    render(<NodeInspectorPanel nodeId="context:ctx-3" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.queryByTestId("loading-spinner")).toBeNull();
    });

    expect(screen.getByTestId("context-content-integrity")).toBeDefined();
    expect(screen.getByText("abc123def456")).toBeDefined();
    expect(screen.queryByTestId("summary-redacted-badge")).toBeNull();
  });

  it("should show the redacted-summary badge and updates, and no source text", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-4",
      summary: "Contact [REDACTED:EMAIL] about this",
      confidence_score: 6.0,
      scope_repo_identifiers: [],
      is_locked: false,
      content_hash: "deadbeef",
      last_verified_at: "2026-07-30T12:00:00Z",
      summary_redacted: true,
      sources: [
        {
          event_id: "ev-2",
          source_platform: "slack",
          source_type: "message",
          author: { display_name: "Bob" },
          happened_at: "2026-07-30T11:00:00Z",
          chat_source: { channel_id: "C1", channel_name: "payments", message_count: 3 },
        },
      ],
      updates: [
        {
          happened_at: "2026-07-31T09:00:00Z",
          source: { platform: "slack", type: "message" },
          text: "Now applies to refunds too",
        },
      ],
    };
    vi.mocked(api.get).mockResolvedValue({ data: mockData });

    render(<NodeInspectorPanel nodeId="context:ctx-4" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.queryByTestId("loading-spinner")).toBeNull();
    });

    expect(screen.getByTestId("summary-redacted-badge")).toBeDefined();
    expect(screen.getByTestId("context-updates").textContent).toContain("Now applies to refunds too");
    expect(screen.getByTestId("source-chat-channel").textContent).toContain("payments (3 messages)");
  });

  it("should render a fallback dash when content_hash/last_verified_at are absent", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-5",
      summary: "Legacy context without hash fields",
      confidence_score: 5.0,
      scope_repo_identifiers: [],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockResolvedValue({ data: mockData });

    render(<NodeInspectorPanel nodeId="context:ctx-5" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.queryByTestId("loading-spinner")).toBeNull();
    });

    const integrityBlock = screen.getByTestId("context-content-integrity");
    expect(integrityBlock.textContent).toContain("—");
  });

  it("should show a redacted badge on a linked context decision for a symbol node", async () => {
    const mockData = {
      type: "symbol",
      id: "symbol:sym-1",
      symbol_name: "login",
      node_type: "function",
      file_path: "src/auth.py",
      merkle_hash: "hash1",
      contexts: [
        {
          id: "ctx-redacted",
          summary: "Contact [REDACTED:EMAIL] before touching auth",
          confidence_score: 8.0,
          summary_redacted: true,
        },
        {
          id: "ctx-clean",
          summary: "Use dependency injection here",
          confidence_score: 6.0,
          summary_redacted: false,
        },
      ],
    };
    vi.mocked(api.get).mockResolvedValue({ data: mockData });

    render(<NodeInspectorPanel nodeId="symbol:sym-1" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.queryByTestId("loading-spinner")).toBeNull();
    });

    expect(screen.getAllByTestId("linked-context-redacted-badge").length).toBe(1);
  });

  // -------------------------------------------------------------------------
  // Scope-edit form
  // -------------------------------------------------------------------------

  it("should show the Edit button for a context node and enter edit mode on click", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-edit-1",
      summary: "An editable decision",
      confidence_score: 7.0,
      scope_repo_identifiers: ["acme/service-a"],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes("/scope/history")) {
        return Promise.resolve({ data: { changed_by: null, changed_at: null } });
      }
      return Promise.resolve({ data: mockData });
    });

    render(<NodeInspectorPanel nodeId="context:ctx-edit-1" repoId="test-repo" onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.queryByTestId("loading-spinner")).toBeNull();
    });

    expect(screen.queryByTestId("scope-edit-form")).toBeNull();
    fireEvent.click(screen.getByTestId("scope-edit-toggle"));

    await waitFor(() => {
      expect(screen.getByTestId("scope-edit-form")).toBeDefined();
    });
    // Existing repo scope pre-populates as a "repo" row.
    expect(screen.getByTestId("scope-row-value-0")).toHaveProperty("value", "acme/service-a");
  });

  it("should show the last-changed-by history when entering edit mode", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-edit-2",
      summary: "A decision with history",
      confidence_score: 7.0,
      scope_repo_identifiers: [],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes("/scope/history")) {
        return Promise.resolve({
          data: { changed_by: "alice@example.com", changed_at: "2026-08-04T12:00:00Z" },
        });
      }
      return Promise.resolve({ data: mockData });
    });

    render(<NodeInspectorPanel nodeId="context:ctx-edit-2" repoId="test-repo" onClose={() => {}} />);
    await waitFor(() => expect(screen.queryByTestId("loading-spinner")).toBeNull());

    fireEvent.click(screen.getByTestId("scope-edit-toggle"));

    await waitFor(() => {
      expect(screen.getByTestId("scope-last-changed")).toBeDefined();
    });
    expect(screen.getByTestId("scope-last-changed").textContent).toContain("alice@example.com");
  });

  it("should add and remove scope rows in edit mode", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-edit-3",
      summary: "Org-wide, becoming scoped",
      confidence_score: 7.0,
      scope_repo_identifiers: [],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes("/scope/history")) {
        return Promise.resolve({ data: { changed_by: null, changed_at: null } });
      }
      return Promise.resolve({ data: mockData });
    });

    render(<NodeInspectorPanel nodeId="context:ctx-edit-3" repoId="test-repo" onClose={() => {}} />);
    await waitFor(() => expect(screen.queryByTestId("loading-spinner")).toBeNull());
    fireEvent.click(screen.getByTestId("scope-edit-toggle"));
    await waitFor(() => expect(screen.getByTestId("scope-edit-form")).toBeDefined());

    expect(screen.queryByTestId("scope-row-0")).toBeNull();
    fireEvent.click(screen.getByTestId("scope-add-row"));
    expect(screen.getByTestId("scope-row-0")).toBeDefined();

    fireEvent.click(screen.getByTestId("scope-remove-row-0"));
    expect(screen.queryByTestId("scope-row-0")).toBeNull();
  });

  it("should save a scope edit successfully and exit edit mode", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-edit-4",
      summary: "About to be scoped to a team",
      confidence_score: 7.0,
      scope_repo_identifiers: [],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes("/scope/history")) {
        return Promise.resolve({ data: { changed_by: null, changed_at: null } });
      }
      return Promise.resolve({ data: mockData });
    });
    vi.mocked(api.patch).mockResolvedValue({
      data: {
        id: "ctx-edit-4",
        scope_repo_identifiers: [],
        scope_rows: [{ scope_type: "team", scope_value: "backend" }],
        is_locked: false,
      },
    });

    render(<NodeInspectorPanel nodeId="context:ctx-edit-4" repoId="test-repo" onClose={() => {}} />);
    await waitFor(() => expect(screen.queryByTestId("loading-spinner")).toBeNull());
    fireEvent.click(screen.getByTestId("scope-edit-toggle"));
    await waitFor(() => expect(screen.getByTestId("scope-edit-form")).toBeDefined());

    fireEvent.click(screen.getByTestId("scope-add-row"));
    fireEvent.change(screen.getByTestId("scope-row-type-0"), { target: { value: "team" } });
    fireEvent.change(screen.getByTestId("scope-row-value-0"), { target: { value: "backend" } });

    fireEvent.click(screen.getByTestId("scope-save-button"));

    await waitFor(() => {
      expect(screen.queryByTestId("scope-edit-form")).toBeNull();
    });
    expect(api.patch).toHaveBeenCalledWith(
      "/api/v1/admin/records/ctx-edit-4/scope",
      {
        scope_rows: [{ scope_type: "team", scope_value: "backend" }],
        is_locked: false,
      },
      { skipAuthRevoked: true }
    );
  });

  it("should explain that an admin key is required when the save is forbidden", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-edit-5",
      summary: "Ordinary key tries to re-scope",
      confidence_score: 7.0,
      scope_repo_identifiers: [],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes("/scope/history")) {
        return Promise.resolve({ data: { changed_by: null, changed_at: null } });
      }
      return Promise.resolve({ data: mockData });
    });
    vi.mocked(api.patch).mockRejectedValue({
      response: { status: 403, data: { detail: "Forbidden: admin key required" } },
    });

    render(<NodeInspectorPanel nodeId="context:ctx-edit-5" repoId="test-repo" onClose={() => {}} />);
    await waitFor(() => expect(screen.queryByTestId("loading-spinner")).toBeNull());
    fireEvent.click(screen.getByTestId("scope-edit-toggle"));
    await waitFor(() => expect(screen.getByTestId("scope-edit-form")).toBeDefined());
    expect(screen.queryByTestId("scope-changed-by-input")).toBeNull();

    fireEvent.click(screen.getByTestId("scope-save-button"));

    await waitFor(() => {
      expect(screen.getByTestId("scope-save-error").textContent).toContain("Only an admin can change the scope.");
    });
    expect(screen.getByTestId("scope-edit-form")).toBeDefined();
  });

  it("should show an inline error and stay in edit mode when the save request fails", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-edit-6",
      summary: "Save will fail",
      confidence_score: 7.0,
      scope_repo_identifiers: [],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes("/scope/history")) {
        return Promise.resolve({ data: { changed_by: null, changed_at: null } });
      }
      return Promise.resolve({ data: mockData });
    });
    vi.mocked(api.patch).mockRejectedValue({
      response: { data: { detail: "Context not found or access denied" } },
    });

    render(<NodeInspectorPanel nodeId="context:ctx-edit-6" repoId="test-repo" onClose={() => {}} />);
    await waitFor(() => expect(screen.queryByTestId("loading-spinner")).toBeNull());
    fireEvent.click(screen.getByTestId("scope-edit-toggle"));
    await waitFor(() => expect(screen.getByTestId("scope-edit-form")).toBeDefined());

    fireEvent.click(screen.getByTestId("scope-save-button"));

    await waitFor(() => {
      expect(screen.getByTestId("scope-save-error").textContent).toContain(
        "Context not found or access denied"
      );
    });
    // Still in edit mode -- the user's draft isn't lost on a failed save.
    expect(screen.getByTestId("scope-edit-form")).toBeDefined();
  });

  it("should cancel editing without saving", async () => {
    const mockData = {
      type: "context",
      id: "context:ctx-edit-7",
      summary: "Cancel this edit",
      confidence_score: 7.0,
      scope_repo_identifiers: ["acme/service-a"],
      is_locked: false,
      sources: [],
    };
    vi.mocked(api.get).mockImplementation((url: string) => {
      if (url.includes("/scope/history")) {
        return Promise.resolve({ data: { changed_by: null, changed_at: null } });
      }
      return Promise.resolve({ data: mockData });
    });

    render(<NodeInspectorPanel nodeId="context:ctx-edit-7" repoId="test-repo" onClose={() => {}} />);
    await waitFor(() => expect(screen.queryByTestId("loading-spinner")).toBeNull());
    fireEvent.click(screen.getByTestId("scope-edit-toggle"));
    await waitFor(() => expect(screen.getByTestId("scope-edit-form")).toBeDefined());

    fireEvent.click(screen.getByTestId("scope-cancel-button"));

    expect(screen.queryByTestId("scope-edit-form")).toBeNull();
    expect(api.patch).not.toHaveBeenCalled();
  });
});
