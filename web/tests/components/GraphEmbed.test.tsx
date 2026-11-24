// @vitest-environment jsdom

// Mock WebGL globals for Sigma compatibility in jsdom
if (typeof window !== "undefined") {
  (window as unknown as Record<string, unknown>).WebGL2RenderingContext = class {};
  (window as unknown as Record<string, unknown>).WebGLRenderingContext = class {};
}
if (typeof globalThis !== "undefined") {
  (globalThis as unknown as Record<string, unknown>).WebGL2RenderingContext = class {};
  (globalThis as unknown as Record<string, unknown>).WebGLRenderingContext = class {};
}
if (typeof global !== "undefined") {
  (global as unknown as Record<string, unknown>).WebGL2RenderingContext = class {};
  (global as unknown as Record<string, unknown>).WebGLRenderingContext = class {};
}

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import type { AxiosAdapter } from "axios";
import { api } from "../../src/lib/api";

type AdapterConfig = { url?: string };

// Mutable so individual tests (e.g. deep-linking) can add a `node`
// param without disturbing the default `token`-only behavior every other
// test in this file relies on.
let mockSearchParamsValue: Record<string, string | null> = { token: "test-token-123" };

// Mock the next/navigation hooks
vi.mock("next/navigation", () => ({
  useSearchParams: () => ({
    get: (key: string) => mockSearchParamsValue[key] ?? null,
  }),
}));

// Sigma's real constructor requires actual WebGL, which jsdom doesn't
// provide -- mock it so tests can exercise the code path past the
// `webglAvailable` gate without a real canvas context.
vi.mock("sigma", () => ({
  default: class MockSigma {
    on() {}
    kill() {}
  },
}));

describe("GraphEmbed RBAC & Polling tests", () => {
  let GraphEmbedComponent: React.ComponentType<{ repoId: string }>;
  const mockAdapter = vi.fn();

  beforeEach(async () => {
    vi.clearAllMocks();

    // Configure the real api instance to use our mock adapter
    mockAdapter.mockImplementation((config: AdapterConfig) => {
      console.log("mockAdapter called for URL:", config.url);
      return Promise.reject(new Error("Unmocked adapter call: " + config.url));
    });
    api.defaults.adapter = mockAdapter as unknown as AxiosAdapter;

    // Dynamically import GraphEmbed after WebGL globals are set up in the process
    const mod = await import("../../src/components/GraphEmbed");
    GraphEmbedComponent = mod.default;
  });

  afterEach(() => {
    cleanup();
    mockAdapter.mockReset();
    mockSearchParamsValue = { token: "test-token-123" };
  });

  it("should display No access if initial load returns 403", async () => {
    const errorResponse = {
      response: {
        status: 403,
        data: { detail: "Access Denied" },
      },
    };
    mockAdapter.mockRejectedValueOnce(errorResponse);

    render(<GraphEmbedComponent repoId="test-repo" />);

    await waitFor(() => {
      expect(screen.getByText("No access")).toBeDefined();
      expect(
        screen.getByText("You do not have access to this graph.")
      ).toBeDefined();
    });
  });

  it("should trigger background polling and handle auth revocation mid-session", async () => {
    // Spy on setInterval to capture the callback
    const setIntervalSpy = vi.spyOn(global, "setInterval");

    // 1. Initial graph fetch succeeds
    const mockGraphData = { nodes: [], edges: [] };
    mockAdapter.mockResolvedValueOnce({
      status: 200,
      data: mockGraphData,
      headers: {},
      config: {},
    });

    render(<GraphEmbedComponent repoId="test-repo" />);

    // Fast-forward initial loading state
    await waitFor(() => {
      expect(screen.getByText("WebGL is turned off")).toBeDefined();
    });

    expect(mockAdapter).toHaveBeenCalled();

    // Verify that setInterval was set up for 60 seconds
    expect(setIntervalSpy).toHaveBeenCalled();
    const pollCall = setIntervalSpy.mock.calls.find(call => call[1] === 60000);
    expect(pollCall).toBeDefined();
    const triggerPoll = pollCall![0] as () => Promise<void>;

    // 2. Mock response for subsequent background polling ping -> returns 403
    const pollErrorResponse = {
      response: {
        status: 403,
        data: { detail: "Revoked" },
      },
    };
    mockAdapter.mockRejectedValueOnce(pollErrorResponse);

    // Manually trigger the polling callback
    try {
      await triggerPoll();
    } catch {
      // expected
    }

    // 3. Verify that the UI updates to No access
    await waitFor(() => {
      expect(screen.getByText("No access")).toBeDefined();
    });

    setIntervalSpy.mockRestore();
  });
});

describe("GraphEmbed deep-linking via ?node=", () => {
  let GraphEmbedComponent: React.ComponentType<{ repoId: string }>;
  const mockAdapter = vi.fn();

  beforeEach(async () => {
    vi.clearAllMocks();
    mockSearchParamsValue = { token: "test-token-123", node: "context:ctx-99" };

    mockAdapter.mockImplementation((config: AdapterConfig) => {
      return Promise.reject(new Error("Unmocked adapter call: " + config.url));
    });
    api.defaults.adapter = mockAdapter as unknown as AxiosAdapter;

    const mod = await import("../../src/components/GraphEmbed");
    GraphEmbedComponent = mod.default;
  });

  afterEach(() => {
    cleanup();
    mockAdapter.mockReset();
    mockSearchParamsValue = { token: "test-token-123" };
  });

  it("auto-selects the cited node once the graph loads, opening NodeInspectorPanel pre-selected", async () => {
    // GraphEmbed's own WebGL-detection effect calls canvas.getContext
    // directly (independent of the mocked Sigma class above); jsdom
    // returns null by default, so stub it truthy for this test only --
    // other tests in this file rely on the real (falsy) jsdom behavior.
    const getContextSpy = vi
      .spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockReturnValue({} as unknown as RenderingContext);

    try {
      const mockGraphData = {
        nodes: [
          { id: "context:ctx-99", type: "context", label: "A cited decision", confidence_score: 5 },
        ],
        edges: [],
      };
      const mockNodeDetails = {
        type: "context",
        id: "context:ctx-99",
        summary: "A cited decision",
        confidence_score: 5,
        sources: [],
      };

      mockAdapter.mockImplementation((config: AdapterConfig) => {
        if (config.url === "/api/v1/graph/network") {
          return Promise.resolve({ status: 200, data: mockGraphData, headers: {}, config });
        }
        if (typeof config.url === "string" && config.url.startsWith("/api/v1/graph/node/")) {
          return Promise.resolve({ status: 200, data: mockNodeDetails, headers: {}, config });
        }
        return Promise.reject(new Error("Unmocked adapter call: " + config.url));
      });

      render(<GraphEmbedComponent repoId="test-repo" />);

      await waitFor(() => {
        expect(screen.getByTestId("node-inspector-panel")).toBeDefined();
      });

      // Confirms NodeInspectorPanel was opened with exactly the node id from
      // the URL, not merely that some panel rendered.
      const nodeDetailCall = mockAdapter.mock.calls.find(
        (call) =>
          typeof call[0].url === "string" && call[0].url.startsWith("/api/v1/graph/node/")
      );
      expect(nodeDetailCall).toBeDefined();
      expect(nodeDetailCall![0].url).toBe("/api/v1/graph/node/context%3Actx-99");
    } finally {
      getContextSpy.mockRestore();
    }
  });

  it("does not auto-open NodeInspectorPanel when no ?node= param is present", async () => {
    mockSearchParamsValue = { token: "test-token-123" };
    const getContextSpy = vi
      .spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockReturnValue({} as unknown as RenderingContext);

    try {
      const mockGraphData = { nodes: [], edges: [] };
      mockAdapter.mockImplementation((config: AdapterConfig) => {
        if (config.url === "/api/v1/graph/network") {
          return Promise.resolve({ status: 200, data: mockGraphData, headers: {}, config });
        }
        return Promise.reject(new Error("Unmocked adapter call: " + config.url));
      });

      render(<GraphEmbedComponent repoId="test-repo" />);

      await waitFor(() => {
        expect(screen.getByText("Checkcord")).toBeDefined();
      });

      expect(screen.queryByTestId("node-inspector-panel")).toBeNull();
    } finally {
      getContextSpy.mockRestore();
    }
  });
});

describe("GraphEmbed token from the URL fragment", () => {
  let GraphEmbedComponent: React.ComponentType<{ repoId: string }>;
  const mockAdapter = vi.fn();

  beforeEach(async () => {
    vi.clearAllMocks();
    mockAdapter.mockImplementation((config: AdapterConfig) =>
      Promise.resolve({ status: 200, data: { nodes: [], edges: [] }, headers: {}, config })
    );
    api.defaults.adapter = mockAdapter as unknown as AxiosAdapter;
    const mod = await import("../../src/components/GraphEmbed");
    GraphEmbedComponent = mod.default;
  });

  afterEach(() => {
    cleanup();
    mockAdapter.mockReset();
    mockSearchParamsValue = { token: "test-token-123" };
    window.history.replaceState(null, "", "/");
  });

  const authHeaderOfFirstCall = () =>
    (mockAdapter.mock.calls[0][0] as { headers: Record<string, string> }).headers.Authorization;

  it("uses the #token= fragment and removes it from the address bar", async () => {
    mockSearchParamsValue = { node: "context:1" };
    window.history.replaceState(null, "", "/embed/graph/o%2Fr?node=context:1#token=frag-token");
    const replaceSpy = vi.spyOn(window.history, "replaceState");

    render(<GraphEmbedComponent repoId="o/r" />);

    await waitFor(() => expect(mockAdapter).toHaveBeenCalled());
    expect(authHeaderOfFirstCall()).toBe("Bearer frag-token");
    expect(replaceSpy).toHaveBeenCalled();
    expect(window.location.hash).toBe("");
    expect(window.location.search).toBe("?node=context:1");
    replaceSpy.mockRestore();
  });

  it("keeps other fragment parameters when removing the token", async () => {
    window.history.replaceState(null, "", "/embed/graph/o%2Fr#token=frag-token&view=full");

    render(<GraphEmbedComponent repoId="o/r" />);

    await waitFor(() => expect(mockAdapter).toHaveBeenCalled());
    expect(window.location.hash).toBe("#view=full");
  });

  it("prefers the fragment over ?token=", async () => {
    mockSearchParamsValue = { token: "query-token" };
    window.history.replaceState(null, "", "/embed/graph/o%2Fr#token=frag-token");

    render(<GraphEmbedComponent repoId="o/r" />);

    await waitFor(() => expect(mockAdapter).toHaveBeenCalled());
    expect(authHeaderOfFirstCall()).toBe("Bearer frag-token");
  });

  it("falls back to ?token= when there is no fragment", async () => {
    mockSearchParamsValue = { token: "query-token" };
    const replaceSpy = vi.spyOn(window.history, "replaceState");

    render(<GraphEmbedComponent repoId="o/r" />);

    await waitFor(() => expect(mockAdapter).toHaveBeenCalled());
    expect(authHeaderOfFirstCall()).toBe("Bearer query-token");
    expect(replaceSpy).not.toHaveBeenCalled();
    replaceSpy.mockRestore();
  });
});
