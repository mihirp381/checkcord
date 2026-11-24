"use client";

import React, { useEffect, useState } from "react";
import { api } from "../lib/api";
import { errorDetail, errorStatus, isCanceled } from "../lib/errors";

interface SymbolSummary {
  id: string;
  symbol_name?: string | null;
  node_type?: string | null;
}

interface ContextSummary {
  id: string;
  summary?: string | null;
  summary_redacted?: boolean;
  confidence_score: number;
}

interface SourceEvent {
  event_id: string;
  source_platform?: string | null;
  source_type?: string | null;
  happened_at: string;
  author?: { display_name?: string | null } | null;
  chat_source?: {
    channel_id: string;
    channel_name?: string | null;
    message_count?: number;
    url?: string | null;
  } | null;
}

interface RecordUpdate {
  happened_at?: string | null;
  source?: { platform?: string | null; type?: string | null } | null;
  text: string;
}

interface ScopeRowItem {
  scope_type: string;
  scope_value: string;
}

interface NodeDetails {
  type: string;
  id: string;
  file_path?: string | null;
  symbols?: SymbolSummary[] | null;
  symbol_name?: string | null;
  node_type?: string | null;
  merkle_hash?: string | null;
  confidence_score?: number | null;
  summary?: string | null;
  summary_redacted?: boolean;
  sources?: SourceEvent[] | null;
  updates?: RecordUpdate[] | null;
  contexts?: ContextSummary[] | null;
  display_name?: string | null;
  authored_events_count?: number | null;
  scope_repo_identifiers?: string[] | null;
  scope_rows?: ScopeRowItem[] | null;
  is_locked?: boolean | null;
  content_hash?: string | null;
  last_verified_at?: string | null;
}

const TYPE_LABELS: Record<string, string> = {
  context: "record",
  developer: "person",
};

const SCOPE_HINTS: Record<string, string> = {
  team: "Team name, such as payments",
  repo: "Repository, such as acme/billing",
  path: "Path, such as src/billing/",
};

interface NodeInspectorPanelProps {
  nodeId: string;
  repoId: string;
  onClose: () => void;
}

export default function NodeInspectorPanel({
  nodeId,
  repoId,
  onClose,
}: NodeInspectorPanelProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [details, setDetails] = useState<NodeDetails | null>(null);
  const [scopeExpanded, setScopeExpanded] = useState(false);

  // Scope-edit UI.
  const [editingScope, setEditingScope] = useState(false);
  const [scopeRowsDraft, setScopeRowsDraft] = useState<
    { scope_type: string; scope_value: string }[]
  >([]);
  const [isLockedDraft, setIsLockedDraft] = useState(false);
  const [savingScope, setSavingScope] = useState(false);
  const [scopeSaveError, setScopeSaveError] = useState<string | null>(null);
  const [scopeHistory, setScopeHistory] = useState<
    { changed_by: string | null; changed_at: string | null } | null
  >(null);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();

    const fetchDetails = async () => {
      setLoading(true);
      setError(null);
      setDetails(null);
      setScopeExpanded(false);
      try {
        const response = await api.get<NodeDetails>(`/api/v1/graph/node/${encodeURIComponent(nodeId)}`, {
          params: { repo_identifier: repoId },
          signal: controller.signal,
        });
        if (active) {
          setDetails(response.data);
        }
      } catch (err: unknown) {
        if (isCanceled(err)) {
          return;
        }
        if (active) {
          console.error("Failed to fetch node details:", err);
          setError(errorDetail(err, "The details could not be loaded."));
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    };

    fetchDetails();

    return () => {
      active = false;
      controller.abort();
    };
  }, [nodeId, repoId]);

  // Helper to format timestamps
  const formatTime = (timeStr: string) => {
    if (!timeStr) return "";
    try {
      const date = new Date(timeStr);
      return date.toLocaleString();
    } catch {
      return timeStr;
    }
  };

  // context_id extraction: details.id for a context node is "context:<uuid>"
  // (see storage/postgres.py::get_node_details) -- the scope PATCH/history
  // endpoints need just the raw uuid.
  const getContextId = () => {
    if (!details || details.type !== "context") return null;
    const id: string = details.id;
    return id.startsWith("context:") ? id.slice("context:".length) : id;
  };

  const startEditingScope = async () => {
    if (!details) return;
    setScopeSaveError(null);
    const initialRows: { scope_type: string; scope_value: string }[] = (
      details.scope_repo_identifiers || []
    ).map((repo: string) => ({ scope_type: "repo", scope_value: repo }));
    setScopeRowsDraft(initialRows);
    setIsLockedDraft(!!details.is_locked);
    setScopeHistory(null);
    setEditingScope(true);

    const contextId = getContextId();
    if (contextId) {
      try {
        const res = await api.get(`/api/v1/graph/context/${contextId}/scope/history`);
        setScopeHistory(res.data);
      } catch {
        // Non-fatal: the edit form still works without a history preview.
        setScopeHistory(null);
      }
    }
  };

  const cancelEditingScope = () => {
    setEditingScope(false);
    setScopeSaveError(null);
  };

  const addScopeRow = () => {
    setScopeRowsDraft((prev) => [...prev, { scope_type: "repo", scope_value: "" }]);
  };

  const removeScopeRow = (index: number) => {
    setScopeRowsDraft((prev) => prev.filter((_, i) => i !== index));
  };

  const updateScopeRow = (index: number, field: "scope_type" | "scope_value", value: string) => {
    setScopeRowsDraft((prev) =>
      prev.map((row, i) => (i === index ? { ...row, [field]: value } : row))
    );
  };

  const saveScope = async () => {
    const contextId = getContextId();
    if (!contextId) return;
    setSavingScope(true);
    setScopeSaveError(null);
    try {
      const res = await api.patch(
        `/api/v1/admin/records/${contextId}/scope`,
        {
          scope_rows: scopeRowsDraft.filter((r) => r.scope_value.trim()),
          is_locked: isLockedDraft,
        },
        { skipAuthRevoked: true }
      );
      setDetails((prev) => prev && ({
        ...prev,
        scope_repo_identifiers: res.data.scope_repo_identifiers,
        scope_rows: res.data.scope_rows,
        is_locked: res.data.is_locked,
      }));
      setEditingScope(false);
    } catch (err: unknown) {
      setScopeSaveError(
        errorStatus(err) === 403
          ? "Only an admin can change the scope."
          : errorDetail(err, "The scope could not be saved.")
      );
    } finally {
      setSavingScope(false);
    }
  };

  return (
    <div className="w-80 border-l border-slate-800 bg-slate-900/30 backdrop-blur-xl h-full flex flex-col p-6 animate-in slide-in-from-right duration-250 ease-out z-20" data-testid="node-inspector-panel">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-slate-800 pb-4">
        <h3 className="text-xs font-bold tracking-wider text-slate-500 uppercase">
          Details
        </h3>
        <button
          onClick={onClose}
          className="text-slate-400 hover:text-slate-200 transition-colors"
          aria-label="Close details"
        >
          <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto py-6 flex flex-col gap-6 text-sm">
        {loading && (
          <div className="flex flex-col items-center justify-center py-12" data-testid="loading-spinner">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-slate-800 border-t-indigo-500"></div>
            <p className="mt-2 text-xs text-slate-500">Loading details…</p>
          </div>
        )}

        {error && (
          <div className="border border-red-500/20 bg-red-500/5 rounded-xl p-4" data-testid="error-message">
            <div className="text-xs font-semibold text-red-400">Could not load details</div>
            <p className="mt-1 text-xs text-slate-400">{error}</p>
            <button
              onClick={() => {
                setDetails(null);
                setError(null);
                setLoading(true);
                api.get<NodeDetails>(`/api/v1/graph/node/${encodeURIComponent(nodeId)}`, {
                  params: { repo_identifier: repoId }
                }).then(
                  (res) => { setDetails(res.data); setLoading(false); },
                  (err: unknown) => { setError(errorDetail(err, "The details could not be loaded.")); setLoading(false); }
                );
              }}
              className="mt-3 px-3 py-1.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg text-xs font-semibold transition-all border border-red-500/20"
            >
              Retry
            </button>
          </div>
        )}

        {!loading && !error && details && (
          <div className="flex flex-col gap-6" data-testid="inspector-content">
            {/* Top Badge */}
            <div>
              <span
                className={`inline-block px-2.5 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider ${
                  details.type === "file"
                    ? "bg-slate-500/10 text-slate-400 border border-slate-500/20"
                    : details.type === "symbol"
                    ? "bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
                    : details.type === "context"
                    ? "bg-amber-500/10 text-amber-400 border border-amber-500/20"
                    : "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                }`}
              >
                {TYPE_LABELS[details.type] ?? details.type}
              </span>
              <h2 className="text-lg font-bold tracking-tight text-slate-100 mt-2 break-all">
                {details.type === "file"
                  ? details.file_path
                  : details.type === "symbol"
                  ? details.symbol_name
                  : details.type === "context"
                  ? details.summary
                  : details.display_name}
              </h2>
            </div>

            {/* Type Specific Layouts */}
            {details.type === "file" && (
              <div className="flex flex-col gap-4">
                <div className="border border-slate-800 bg-slate-950/40 rounded-xl p-4">
                  <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide">
                    File
                  </div>
                  <div className="mt-1 text-xs font-mono text-slate-300 break-all">
                    {details.file_path}
                  </div>
                </div>

                <div className="border border-slate-800 bg-slate-950/40 rounded-xl p-4">
                  <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide mb-2">
                    Symbols in this file ({details.symbols?.length || 0})
                  </div>
                  {details.symbols && details.symbols.length > 0 ? (
                    <div className="flex flex-col gap-2 max-h-48 overflow-y-auto pr-1">
                      {details.symbols.map((sym) => (
                        <div
                          key={sym.id}
                          className="flex items-center justify-between p-2 rounded-lg bg-slate-900/60 border border-slate-800/60"
                        >
                          <span className="font-mono text-xs text-indigo-300 truncate mr-2">
                            {sym.symbol_name}
                          </span>
                          <span className="text-[9px] uppercase font-bold text-slate-500 bg-slate-800/80 px-1.5 py-0.5 rounded">
                            {sym.node_type}
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-slate-500">No symbols found in this file.</p>
                  )}
                </div>
              </div>
            )}

            {details.type === "symbol" && (
              <div className="flex flex-col gap-4">
                <div className="border border-slate-800 bg-slate-950/40 rounded-xl p-4">
                  <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide">
                    Symbol
                  </div>
                  <div className="mt-2 flex flex-col gap-1.5 text-xs text-slate-400">
                    <div>
                      File: <span className="font-mono text-slate-300 break-all">{details.file_path}</span>
                    </div>
                    <div>
                      Type: <span className="font-mono text-indigo-300">{details.node_type}</span>
                    </div>
                    <div>
                      Hash: <span className="font-mono text-[10px] text-slate-500 break-all">{details.merkle_hash}</span>
                    </div>
                  </div>
                </div>

                <div className="border border-slate-800 bg-slate-950/40 rounded-xl p-4">
                  <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide mb-2">
                    Linked decisions ({details.contexts?.length || 0})
                  </div>
                  {details.contexts && details.contexts.length > 0 ? (
                    <div className="flex flex-col gap-2">
                      {details.contexts.map((ctx) => (
                        <div
                          key={ctx.id}
                          className="p-3 rounded-lg bg-slate-900/60 border border-slate-800/60 flex flex-col gap-1.5"
                        >
                          <p className="text-xs text-slate-300 leading-relaxed">
                            {ctx.summary}
                          </p>
                          <div className="flex items-center gap-1.5">
                            <div className="text-[10px] font-bold text-amber-400 bg-amber-400/5 px-2 py-0.5 rounded border border-amber-400/10 self-start">
                              Evidence score {ctx.confidence_score.toFixed(1)}
                            </div>
                            {ctx.summary_redacted && (
                              <div
                                data-testid="linked-context-redacted-badge"
                                className="text-[10px] font-bold text-rose-400 bg-rose-500/10 px-2 py-0.5 rounded border border-rose-500/20 self-start"
                              >
                                Personal data redacted
                              </div>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-slate-500">No live decisions are linked to this symbol.</p>
                  )}
                </div>
              </div>
            )}

            {details.type === "context" && (
              <div className="flex flex-col gap-4">
                <div className="border border-slate-800 bg-slate-950/40 rounded-xl p-4">
                  <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide">
                    Evidence score
                  </div>
                  <div className="mt-2 text-lg font-bold text-amber-400">
                    {details.confidence_score?.toFixed(1)}
                  </div>
                </div>

                <div
                  className="border border-slate-800 bg-slate-950/40 rounded-xl p-4"
                  data-testid="context-scope-badge"
                >
                  <div className="flex items-center justify-between">
                    <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide">
                      Scope
                    </div>
                    {!editingScope && (
                      <button
                        data-testid="scope-edit-toggle"
                        onClick={startEditingScope}
                        className="text-[10px] text-indigo-400 hover:text-indigo-300 font-semibold"
                      >
                        Edit
                      </button>
                    )}
                  </div>
                  <div className="mt-2 flex items-center gap-1.5">
                    {(() => {
                      const scopeRepos: string[] = details.scope_repo_identifiers || [];
                      return (
                        <span
                          className="inline-block px-2.5 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-sky-500/10 text-sky-400 border border-sky-500/20"
                          title={scopeRepos.length > 1 ? scopeRepos.join(", ") : undefined}
                        >
                          {scopeRepos.length === 0
                            ? "Organization-wide"
                            : scopeRepos.length === 1
                            ? `Scoped to ${scopeRepos[0]}`
                            : `Scoped to ${scopeRepos[0]} +${scopeRepos.length - 1} more`}
                        </span>
                      );
                    })()}
                    {(details.scope_repo_identifiers || []).length > 1 && (
                      <button
                        data-testid="context-scope-expand-toggle"
                        onClick={() => setScopeExpanded((v) => !v)}
                        className="text-[10px] text-indigo-400 hover:text-indigo-300 font-semibold"
                        aria-label={scopeExpanded ? "Hide repositories" : "Show all repositories"}
                      >
                        {scopeExpanded ? "Hide" : "Show all"}
                      </button>
                    )}
                    {details.is_locked && (
                      <svg
                        data-testid="context-scope-lock-icon"
                        className="h-3.5 w-3.5 text-slate-400"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                      >
                        <title>Locked</title>
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth="2"
                          d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"
                        />
                      </svg>
                    )}
                  </div>
                  {scopeExpanded && (details.scope_repo_identifiers || []).length > 1 && (
                    <ul data-testid="context-scope-full-list" className="mt-1.5 flex flex-col gap-0.5 pl-1">
                      {(details.scope_repo_identifiers || []).map((repo: string) => (
                        <li key={repo} className="text-[10px] font-mono text-slate-400">
                          {repo}
                        </li>
                      ))}
                    </ul>
                  )}

                  {editingScope && (
                    <div
                      data-testid="scope-edit-form"
                      className="mt-3 pt-3 border-t border-slate-800 flex flex-col gap-3"
                    >
                      {scopeHistory && scopeHistory.changed_by && (
                        <div data-testid="scope-last-changed" className="text-[10px] text-slate-500">
                          Last changed by{" "}
                          <span className="text-slate-300">{scopeHistory.changed_by}</span> at{" "}
                          {scopeHistory.changed_at ? formatTime(scopeHistory.changed_at) : "—"}
                        </div>
                      )}

                      <div className="flex flex-col gap-2">
                        {scopeRowsDraft.map((row, i) => (
                          <div
                            key={i}
                            data-testid={`scope-row-${i}`}
                            className="flex items-center gap-1.5"
                          >
                            <select
                              data-testid={`scope-row-type-${i}`}
                              value={row.scope_type}
                              onChange={(e) => updateScopeRow(i, "scope_type", e.target.value)}
                              className="text-[10px] bg-slate-900 border border-slate-700 rounded px-1.5 py-1 text-slate-300"
                            >
                              <option value="team">Team</option>
                              <option value="repo">Repository</option>
                              <option value="path">Path</option>
                            </select>
                            <input
                              data-testid={`scope-row-value-${i}`}
                              type="text"
                              value={row.scope_value}
                              onChange={(e) => updateScopeRow(i, "scope_value", e.target.value)}
                              placeholder={SCOPE_HINTS[row.scope_type] ?? ""}
                              className="flex-1 text-[10px] bg-slate-900 border border-slate-700 rounded px-1.5 py-1 text-slate-300"
                            />
                            <button
                              data-testid={`scope-remove-row-${i}`}
                              onClick={() => removeScopeRow(i)}
                              aria-label="Remove scope row"
                              className="text-rose-400 hover:text-rose-300 text-[10px] font-semibold"
                            >
                              ✕
                            </button>
                          </div>
                        ))}
                        <button
                          data-testid="scope-add-row"
                          onClick={addScopeRow}
                          className="self-start text-[10px] text-indigo-400 hover:text-indigo-300 font-semibold"
                        >
                          + Add scope row
                        </button>
                        {scopeRowsDraft.length === 0 && (
                          <p className="text-[10px] text-slate-500">
                            With no rows, the record applies to the whole organization.
                          </p>
                        )}
                      </div>

                      <label className="flex items-center gap-1.5 text-[10px] text-slate-400">
                        <input
                          data-testid="scope-locked-checkbox"
                          type="checkbox"
                          checked={isLockedDraft}
                          onChange={(e) => setIsLockedDraft(e.target.checked)}
                        />
                        Locked: automatic updates will not change it
                      </label>

                      {scopeSaveError && (
                        <p data-testid="scope-save-error" className="text-[10px] text-rose-400">
                          {scopeSaveError}
                        </p>
                      )}

                      <div className="flex items-center gap-2">
                        <button
                          data-testid="scope-save-button"
                          onClick={saveScope}
                          disabled={savingScope}
                          className="px-2.5 py-1 bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-400 rounded text-[10px] font-semibold border border-indigo-500/20 disabled:opacity-50"
                        >
                          {savingScope ? "Saving…" : "Save"}
                        </button>
                        <button
                          data-testid="scope-cancel-button"
                          onClick={cancelEditingScope}
                          disabled={savingScope}
                          className="px-2.5 py-1 text-slate-400 hover:text-slate-200 rounded text-[10px] font-semibold"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </div>

                <div
                  className="border border-slate-800 bg-slate-950/40 rounded-xl p-4"
                  data-testid="context-content-integrity"
                >
                  <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide flex items-center gap-1.5">
                    Integrity
                    {details.summary_redacted && (
                      <span
                        data-testid="summary-redacted-badge"
                        className="inline-block px-2 py-0.5 rounded-full text-[9px] font-bold uppercase tracking-wider bg-rose-500/10 text-rose-400 border border-rose-500/20"
                      >
                        Personal data redacted
                      </span>
                    )}
                  </div>
                  <div className="mt-2 flex flex-col gap-1.5 text-xs text-slate-400">
                    <div>
                      Content hash:{" "}
                      <span
                        className="font-mono text-[10px] text-slate-500 break-all"
                        title={details.content_hash || undefined}
                      >
                        {details.content_hash || "—"}
                      </span>
                    </div>
                    <div>
                      Last verified:{" "}
                      <span className="text-slate-300">
                        {details.last_verified_at ? formatTime(details.last_verified_at) : "—"}
                      </span>
                    </div>
                  </div>
                </div>

                {details.updates && details.updates.length > 0 && (
                  <div
                    className="border border-slate-800 bg-slate-950/40 rounded-xl p-4"
                    data-testid="context-updates"
                  >
                    <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide mb-2">
                      Updates ({details.updates.length})
                    </div>
                    <div className="flex flex-col gap-2">
                      {details.updates.map((u, i) => (
                        <div key={i} className="text-xs text-slate-300 leading-relaxed">
                          <span className="text-[10px] text-slate-500 mr-1.5">
                            {u.happened_at ? formatTime(u.happened_at) : "—"}
                            {u.source?.platform ? ` · ${u.source.platform}` : ""}
                          </span>
                          <span className="whitespace-pre-wrap">{u.text}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <div className="border border-slate-800 bg-slate-950/40 rounded-xl p-4">
                  <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide mb-2">
                    Sources ({details.sources?.length || 0})
                  </div>
                  <p className="text-[10px] text-slate-500 mb-2">
                    Source messages are not shown here; admins can read them in the audited raw view.
                  </p>
                  {details.sources && details.sources.length > 0 ? (
                    <div className="flex flex-col gap-3">
                      {details.sources.map((src) => {
                        return (
                          <div
                            key={src.event_id}
                            className="p-3.5 rounded-xl bg-slate-900/70 border border-slate-800/80 flex flex-col gap-2 shadow-inner"
                          >
                            <div className="flex items-center justify-between text-[10px]">
                              <span className="uppercase font-bold text-slate-500 px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700/50">
                                {src.source_platform} {src.source_type}
                              </span>
                              <span className="text-slate-500 font-medium">
                                {formatTime(src.happened_at)}
                              </span>
                            </div>
                            {src.chat_source && (
                              <div data-testid="source-chat-channel" className="text-[10px] text-slate-400">
                                Channel:{" "}
                                {src.chat_source.url ? (
                                  <a
                                    href={src.chat_source.url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="font-semibold text-indigo-400 hover:text-indigo-300"
                                  >
                                    {src.chat_source.channel_name || src.chat_source.channel_id}
                                  </a>
                                ) : (
                                  <span className="font-semibold text-slate-300">
                                    {src.chat_source.channel_name || src.chat_source.channel_id}
                                  </span>
                                )}
                                {src.chat_source.message_count ? ` (${src.chat_source.message_count} messages)` : ""}
                              </div>
                            )}
                            {src.author?.display_name && (
                              <div className="border-t border-slate-850 pt-2 flex items-center gap-1.5 text-[10px] text-slate-400 mt-1">
                                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
                                Author: <span className="font-semibold text-slate-300">{src.author.display_name}</span>
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <p className="text-xs text-slate-500">No sources.</p>
                  )}
                </div>
              </div>
            )}

            {details.type === "developer" && (
              <div className="flex flex-col gap-4">
                <div className="border border-slate-800 bg-slate-950/40 rounded-xl p-4">
                  <div className="text-xs text-slate-500 font-semibold uppercase tracking-wide">
                    Events by this person
                  </div>
                  <div className="mt-2 text-2xl font-bold text-emerald-400">
                    {details.authored_events_count}
                  </div>
                  <p className="mt-1 text-[10px] text-slate-500 leading-relaxed">
                    Messages, tickets and other events from this person in this organization.
                  </p>
                </div>
              </div>
            )}

            {/* General ID bottom footer */}
            <div className="text-xs text-slate-500 leading-relaxed border-t border-slate-850 pt-4">
              ID: <span className="font-mono text-[10px] text-slate-400 break-all">{details.id}</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
