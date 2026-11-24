"use client";

import React, { useEffect, useState, useRef, useSyncExternalStore } from "react";
import { useSearchParams } from "next/navigation";
import { api, setAuthToken } from "../lib/api";
import { errorDetail, errorStatus } from "../lib/errors";
import { processNetworkGraph, NetworkGraphResponse } from "../lib/graph-processor";
import Sigma from "sigma";
import forceAtlas2 from "graphology-layout-forceatlas2";
import NodeInspectorPanel from "./NodeInspectorPanel";

interface GraphEmbedProps {
  repoId: string;
  // Signed in to the console: the session or stored key authenticates, not a token in the URL.
  session?: boolean;
}

interface FetchResult {
  key: string;
  data: NetworkGraphResponse | null;
  error: string | null;
  status: number;
}

const ACCESS_DENIED = "You do not have access to this graph.";

const subscribeNoop = () => () => {};

function readFragmentToken(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.hash.slice(1)).get("token");
}

function stripFragmentToken(): void {
  const params = new URLSearchParams(window.location.hash.slice(1));
  if (!params.has("token")) return;
  params.delete("token");
  const rest = params.toString();
  const { pathname, search } = window.location;
  window.history.replaceState(window.history.state, "", `${pathname}${search}${rest ? `#${rest}` : ""}`);
}

function detectWebgl(): boolean {
  if (typeof document === "undefined") return true;
  try {
    const canvas = document.createElement("canvas");
    return !!(
      window.WebGLRenderingContext &&
      (canvas.getContext("webgl") || canvas.getContext("experimental-webgl"))
    );
  } catch {
    return false;
  }
}

export default function GraphEmbed({ repoId, session = false }: GraphEmbedProps) {
  const searchParams = useSearchParams();
  const [fragmentToken] = useState(() => (session ? null : readFragmentToken()));
  const token = session ? "session" : (fragmentToken ?? searchParams.get("token"));
  const frame = session ? "h-[calc(100vh-12rem)] min-h-[24rem] w-full rounded-lg" : "h-screen w-screen";
  const nodeIdParam = searchParams.get("node");
  const requestKey = token ? `${repoId}\n${token}` : null;

  const mounted = useSyncExternalStore(subscribeNoop, () => true, () => false);
  const [webglAvailable] = useState(detectWebgl);
  const [result, setResult] = useState<FetchResult | null>(null);
  const [revoked, setRevoked] = useState<{ key: string | null; status: number } | null>(null);
  const [selectedNode, setSelectedNode] = useState<{ id: string } | null>(null);

  const current = result && result.key === requestKey ? result : null;
  const revokedNow = revoked && revoked.key === requestKey ? revoked : null;
  const data = current?.data ?? null;
  const loading = requestKey !== null && current === null && revokedNow === null;
  const error = !token
    ? "You are not signed in."
    : revokedNow
      ? ACCESS_DENIED
      : (current?.error ?? null);
  const status = revokedNow?.status ?? current?.status ?? null;

  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const autoSelectedRef = useRef(false);

  useEffect(() => {
    if (!session) stripFragmentToken();
  }, [session]);

  // Fetch the graph.
  useEffect(() => {
    if (!mounted || !token || requestKey === null) return;

    if (!session) setAuthToken(token);
    let active = true;

    api
      .get<NetworkGraphResponse>(`/api/v1/graph/network`, {
        params: { repo_identifier: repoId },
      })
      .then(
        (response) => {
          if (active) setResult({ key: requestKey, data: response.data, error: null, status: 200 });
        },
        (err: unknown) => {
          if (!active) return;
          console.error("API error:", err);
          const statusCode = errorStatus(err) || 500;
          const message =
            statusCode === 401 || statusCode === 403
              ? ACCESS_DENIED
              : errorDetail(err, "The graph could not be loaded.");
          setResult({ key: requestKey, data: null, error: message, status: statusCode });
        }
      );

    return () => {
      active = false;
    };
  }, [repoId, token, requestKey, mounted, session]);

  // Deep-link auto-select: a PR comment or collision warning links
  // here with a `?node=` id. Once the graph has loaded, pre-select it so
  // NodeInspectorPanel opens the same way a manual click would -- no need to
  // wait for Sigma's own click handler, and no auth token is expected in the
  // URL (see core/viewer_links.py), so this only needs the node id itself.
  useEffect(() => {
    if (!mounted || !data || !nodeIdParam || autoSelectedRef.current) return;
    autoSelectedRef.current = true;
    setSelectedNode({ id: nodeIdParam });
  }, [mounted, data, nodeIdParam]);

  // Listen for global auth revocation events
  useEffect(() => {
    if (!mounted || session) return;

    const handleAuthRevoked = (e: Event) => {
      const customEvent = e as CustomEvent;
      setRevoked({ key: requestKey, status: customEvent.detail || 403 });
    };

    window.addEventListener("auth:revoked", handleAuthRevoked);
    return () => {
      window.removeEventListener("auth:revoked", handleAuthRevoked);
    };
  }, [mounted, requestKey, session]);

  // Background Polling to check auth/RLS status
  useEffect(() => {
    if (!mounted || !token || error) return;

    const intervalId = setInterval(async () => {
      try {
        await api.get("/api/v1/graph/ping", {
          params: { repo_identifier: repoId },
        });
      } catch (err: unknown) {
        console.error("Auth status poll failed:", err);
      }
    }, 60000); // Poll every 60 seconds

    return () => {
      clearInterval(intervalId);
    };
  }, [mounted, token, repoId, error]);

  // 3. Render Sigma.js WebGL Graph
  useEffect(() => {
    if (!mounted || !data || !containerRef.current || !webglAvailable) return;

    // Clean up previous instance if any
    if (sigmaRef.current) {
      sigmaRef.current.kill();
      sigmaRef.current = null;
    }

    // Process graph payload with graphology
    const graph = processNetworkGraph(data);

    // Apply ForceAtlas2 layout synchronously to detangle nodes
    if (graph.order > 0) {
      forceAtlas2.assign(graph, {
        iterations: 150,
        settings: forceAtlas2.inferSettings(graph),
      });
    }

    // Initialize Sigma.js WebGL renderer
    const renderer = new Sigma(graph, containerRef.current, {
      defaultNodeType: "circle",
      labelFont: "Inter, Roboto, sans-serif",
      labelSize: 11,
      labelColor: { color: "#94a3b8" },
      renderEdgeLabels: false,
      allowInvalidContainer: true,
    });

    sigmaRef.current = renderer;

    // Add click event handlers
    renderer.on("clickNode", ({ node }) => {
      setSelectedNode({ id: node });
    });

    renderer.on("clickStage", () => {
      setSelectedNode(null);
    });

    return () => {
      if (sigmaRef.current) {
        sigmaRef.current.kill();
        sigmaRef.current = null;
      }
    };
  }, [data, mounted, webglAvailable]);

  if (!mounted || loading) {
    return (
      <div className={`flex ${frame} flex-col items-center justify-center bg-slate-950 text-slate-200`}>
        <div className="h-16 w-16 animate-spin rounded-full border-4 border-slate-800 border-t-indigo-500"></div>
        <p className="mt-4 text-sm font-medium tracking-wide text-slate-400 animate-pulse">
          Loading graph…
        </p>
      </div>
    );
  }

  if (error) {
    const isAuthError = status === 401 || status === 403 || !token;
    return (
      <div className={`flex ${frame} items-center justify-center bg-slate-950 p-6 text-slate-200`}>
        <div className="w-full max-w-md rounded-2xl border border-red-500/20 bg-slate-900/60 p-6 backdrop-blur-xl shadow-2xl shadow-red-500/5">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-red-500/10 text-red-400">
            <svg
              className="h-6 w-6"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="2"
                d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
              />
            </svg>
          </div>
          <h2 className="mt-4 text-lg font-bold tracking-tight text-red-200">
            {isAuthError ? "No access" : "Something went wrong"}
          </h2>
          <p className="mt-2 text-sm leading-relaxed text-slate-400">{error}</p>
          {isAuthError && !session && (
            <p className="mt-4 text-xs text-slate-500">
              Check that the link holds a valid key.
            </p>
          )}
        </div>
      </div>
    );
  }

  if (!webglAvailable) {
    return (
      <div className={`flex ${frame} items-center justify-center bg-slate-950 p-6 text-slate-200`}>
        <div className="w-full max-w-md rounded-2xl border border-amber-500/20 bg-slate-900/60 p-6 backdrop-blur-xl shadow-2xl shadow-amber-500/5">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-amber-500/10 text-amber-400">
            <svg className="h-6 w-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
          </div>
          <h2 className="mt-4 text-lg font-bold tracking-tight text-amber-200">
            WebGL is turned off
          </h2>
          <p className="mt-2 text-sm leading-relaxed text-slate-400">
            The graph needs WebGL, which this browser has turned off.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className={`flex ${frame} bg-slate-950 text-slate-200 overflow-hidden font-sans`}>
      {/* Main Canvas View Area */}
      <div className="flex-1 flex flex-col relative h-full">
        {/* Header */}
        <div className="absolute top-4 left-4 z-10 flex items-center justify-between border border-slate-800 bg-slate-900/80 backdrop-blur-md px-4 py-3 rounded-2xl shadow-lg shadow-slate-950/40">
          <div>
            <h1 className="text-sm font-bold tracking-tight text-indigo-400 flex items-center gap-2">
              Checkcord
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-ping"></span>
            </h1>
            <p className="text-[10px] text-slate-500">
              Code graph for <span className="font-semibold text-slate-400">{repoId}</span>
            </p>
          </div>
        </div>

        {/* WebGL Canvas Container */}
        <div ref={containerRef} className="w-full h-full cursor-grab active:cursor-grabbing" id="sigma-container" />

        {/* Legend */}
        <div className="absolute bottom-4 left-4 z-10 flex flex-wrap gap-4 border border-slate-800 bg-slate-900/80 backdrop-blur-md px-4 py-2.5 rounded-2xl shadow-lg text-[10px]">
          <div className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-[#94a3b8]"></span>
            <span className="text-slate-400">Files</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-[#818cf8]"></span>
            <span className="text-slate-400">Symbols</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-[#fbbf24]"></span>
            <span className="text-slate-400">Records</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-[#34d399]"></span>
            <span className="text-slate-400">People</span>
          </div>
        </div>
      </div>

      {/* Details panel */}
      {selectedNode && (
        <NodeInspectorPanel
          nodeId={selectedNode.id}
          repoId={repoId}
          onClose={() => setSelectedNode(null)}
        />
      )}
    </div>
  );
}
