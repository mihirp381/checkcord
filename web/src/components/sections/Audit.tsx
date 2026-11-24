"use client";

import { useState } from "react";
import { exportAudit, listAudit } from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import { formatNumber } from "../../lib/format";
import type { AuditChannel, AuditFilters } from "../../lib/types";
import { usePaged } from "../../lib/useApi";
import {
  Button,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  More,
  Mono,
  PageHeader,
  Table,
  Td,
  Th,
  Time,
  inputClass,
} from "../ui";

interface Draft {
  since: string;
  until: string;
  user: string;
  key: string;
  action: string;
  channel: string;
  record_id: string;
}

const EMPTY: Draft = { since: "", until: "", user: "", key: "", action: "", channel: "", record_id: "" };

const toFilters = (d: Draft): AuditFilters => ({
  since: d.since ? new Date(d.since).toISOString() : undefined,
  until: d.until ? new Date(d.until).toISOString() : undefined,
  user: d.user.trim() || undefined,
  key: d.key.trim() || undefined,
  action: d.action.trim() || undefined,
  channel: (d.channel || undefined) as AuditChannel | undefined,
  record_id: d.record_id.trim() || undefined,
});

const download = (blob: Blob, name: string) => {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

export default function Audit() {
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [filters, setFilters] = useState<AuditFilters>({});
  const list = usePaged((cursor?: string) => listAudit(filters, cursor), [filters]);
  const [exporting, setExporting] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));
  const filtered = Object.values(filters).some(Boolean);

  const exportAs = async (format: "csv" | "jsonl") => {
    setExporting(format);
    setExportError(null);
    try {
      const blob = await exportAudit(format, filters);
      download(blob, `checkcord-audit-${new Date().toISOString().slice(0, 10)}.${format}`);
    } catch (err) {
      setExportError(errorMessage(err));
    } finally {
      setExporting(null);
    }
  };

  return (
    <>
      <PageHeader
        title="Audit"
        description="Every read by an agent or person and every admin change, newest first. Reading this page is audited too."
        actions={
          <>
            <Button onClick={() => exportAs("csv")} disabled={exporting !== null}>{exporting === "csv" ? "Exporting…" : "Export CSV"}</Button>
            <Button onClick={() => exportAs("jsonl")} disabled={exporting !== null}>{exporting === "jsonl" ? "Exporting…" : "Export JSON Lines"}</Button>
          </>
        }
      />
      {exportError && <div className="mb-4"><ErrorState message={exportError} /></div>}
      <form
        className="mb-4 grid grid-cols-1 gap-3 rounded-lg border border-line bg-panel p-3 sm:grid-cols-2 lg:grid-cols-4"
        onSubmit={(e) => {
          e.preventDefault();
          setFilters(toFilters(draft));
        }}
      >
        <Field label="From"><input type="datetime-local" className={inputClass} value={draft.since} onChange={(e) => set({ since: e.target.value })} /></Field>
        <Field label="Until"><input type="datetime-local" className={inputClass} value={draft.until} onChange={(e) => set({ until: e.target.value })} /></Field>
        <Field label="Person"><input className={inputClass} value={draft.user} onChange={(e) => set({ user: e.target.value })} placeholder="Email or id" /></Field>
        <Field label="Key"><input className={inputClass} value={draft.key} onChange={(e) => set({ key: e.target.value })} placeholder="Key name or id" /></Field>
        <Field label="Action"><input className={inputClass} value={draft.action} onChange={(e) => set({ action: e.target.value })} placeholder="graph.context" /></Field>
        <Field label="Channel">
          <select className={inputClass} value={draft.channel} onChange={(e) => set({ channel: e.target.value })}>
            <option value="">Any</option>
            <option value="mcp">MCP</option>
            <option value="api">API</option>
            <option value="cli">CLI</option>
            <option value="scim">SCIM</option>
          </select>
        </Field>
        <Field label="Record id" className="sm:col-span-2"><input className={`${inputClass} font-mono`} value={draft.record_id} onChange={(e) => set({ record_id: e.target.value })} /></Field>
        <div className="flex gap-2 sm:col-span-2 lg:col-span-4">
          <Button type="submit" variant="primary">Apply</Button>
          {filtered && <Button onClick={() => { setDraft(EMPTY); setFilters({}); }}>Clear filters</Button>}
        </div>
      </form>
      <p className="mb-3 text-xs text-muted">Exports use the filters above.</p>
      {list.loading ? (
        <Loading />
      ) : list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : list.items.length === 0 ? (
        <EmptyState title={filtered ? "No entries match these filters" : "No audit entries yet"} />
      ) : (
        <>
          <Table label="Audit entries">
            <thead>
              <tr><Th>When</Th><Th>Who</Th><Th>Channel</Th><Th>Action</Th><Th>Records</Th><Th className="text-right">Results</Th><Th>Details</Th></tr>
            </thead>
            <tbody>
              {list.items.map((e) => (
                <tr key={e.id}>
                  <Td className="whitespace-nowrap"><Time iso={e.created_at} /></Td>
                  <Td className="min-w-[10rem]">
                    {e.user_email ?? <span className="text-muted">No person</span>}
                    {e.key_name && <div className="text-xs text-muted">key: {e.key_name}</div>}
                  </Td>
                  <Td>{e.channel}</Td>
                  <Td><Mono>{e.action}</Mono></Td>
                  <Td className="max-w-[16rem] text-xs">
                    {e.record_ids && e.record_ids.length > 0 ? (
                      <details>
                        <summary className="cursor-pointer">{formatNumber(e.record_ids.length)}</summary>
                        <ul>{e.record_ids.map((id) => <li key={id}><Mono>{id}</Mono></li>)}</ul>
                      </details>
                    ) : "–"}
                  </Td>
                  <Td className="text-right tabular-nums">
                    {e.result_count ?? "–"}
                    {e.redacted_count ? <div className="text-xs text-muted">{formatNumber(e.redacted_count)} redacted</div> : null}
                  </Td>
                  <Td className="max-w-[20rem] text-xs">
                    {e.params && Object.keys(e.params).length > 0 ? (
                      <details>
                        <summary className="cursor-pointer">Parameters</summary>
                        <pre className="mt-1 whitespace-pre-wrap font-mono [overflow-wrap:anywhere]">{JSON.stringify(e.params, null, 2)}</pre>
                      </details>
                    ) : "–"}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <More list={list} />
        </>
      )}
    </>
  );
}
