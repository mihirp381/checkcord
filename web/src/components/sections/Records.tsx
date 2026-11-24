"use client";

import Link from "next/link";
import { useState } from "react";
import { listRecords } from "../../lib/api";
import type { RecordFilters, RecordKind, RecordState, Severity } from "../../lib/types";
import { usePaged } from "../../lib/useApi";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  More,
  PageHeader,
  Table,
  Td,
  Th,
  Time,
  inputClass,
} from "../ui";

export const RECORD_TYPES = ["general", "adr", "constraint", "risk", "business_requirement"];
const DEFAULT_FILTERS: RecordFilters = { kind: "decision" };

export function SeverityBadge({ severity }: { severity: Severity }) {
  return severity === "blocking" ? <Badge tone="danger">Blocking</Badge> : <Badge>Advisory</Badge>;
}

export default function Records() {
  const [draft, setDraft] = useState<RecordFilters>(DEFAULT_FILTERS);
  const [filters, setFilters] = useState<RecordFilters>(DEFAULT_FILTERS);
  const list = usePaged((cursor?: string) => listRecords(filters, cursor), [filters]);

  const set = (patch: Partial<RecordFilters>) => setDraft((d) => ({ ...d, ...patch }));
  const filtered = Object.entries(filters).some(([k, v]) => v && !(k === "kind" && v === DEFAULT_FILTERS.kind));

  return (
    <>
      <PageHeader title="Records" description="Decisions are the ADRs, constraints, requirements and risks Checkcord extracted, and any record marked blocking. Reference records hold the text of tickets, pull requests, pages and alerts. Summaries are shown redacted." />
      <form
        className="mb-4 grid grid-cols-1 gap-3 rounded-lg border border-line bg-panel p-3 sm:grid-cols-2 lg:grid-cols-7"
        onSubmit={(e) => {
          e.preventDefault();
          setFilters({ ...draft, q: draft.q?.trim() || undefined, repository: draft.repository?.trim() || undefined });
        }}
      >
        <Field label="Search" className="lg:col-span-2">
          <input type="search" className={inputClass} value={draft.q ?? ""} onChange={(e) => set({ q: e.target.value })} placeholder="Words in the summary" />
        </Field>
        <Field label="Repository">
          <input className={inputClass} value={draft.repository ?? ""} onChange={(e) => set({ repository: e.target.value })} placeholder="OWNER/NAME" />
        </Field>
        <Field label="Show">
          <select className={inputClass} value={draft.kind ?? ""} onChange={(e) => set({ kind: (e.target.value || undefined) as RecordKind | undefined })}>
            <option value="decision">Decisions</option>
            <option value="reference">Reference</option>
            <option value="">All</option>
          </select>
        </Field>
        <Field label="Type">
          <select className={inputClass} value={draft.type ?? ""} onChange={(e) => set({ type: e.target.value || undefined })}>
            <option value="">Any</option>
            {RECORD_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </Field>
        <Field label="Severity">
          <select className={inputClass} value={draft.severity ?? ""} onChange={(e) => set({ severity: (e.target.value || undefined) as Severity | undefined })}>
            <option value="">Any</option>
            <option value="advisory">Advisory</option>
            <option value="blocking">Blocking</option>
          </select>
        </Field>
        <Field label="State">
          <select className={inputClass} value={draft.state ?? ""} onChange={(e) => set({ state: (e.target.value || undefined) as RecordState | undefined })}>
            <option value="">Live or held</option>
            <option value="live">Live</option>
            <option value="held">Held for review</option>
            <option value="retired">Retired</option>
          </select>
        </Field>
        <div className="flex gap-2 sm:col-span-2 lg:col-span-7">
          <Button type="submit" variant="primary">Apply</Button>
          {filtered && <Button onClick={() => { setDraft(DEFAULT_FILTERS); setFilters(DEFAULT_FILTERS); }}>Clear filters</Button>}
        </div>
      </form>
      {list.loading ? (
        <Loading />
      ) : list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : list.items.length === 0 ? (
        <EmptyState title={filtered ? "No records match these filters" : "No records yet"}>
          {filtered ? "Change or clear the filters." : "Records appear once connected systems send content."}
        </EmptyState>
      ) : (
        <>
          <Table label="Records">
            <thead>
              <tr><Th>Summary</Th><Th>Type</Th><Th>Severity</Th><Th>State</Th><Th>Created</Th></tr>
            </thead>
            <tbody>
              {list.items.map((r) => (
                <tr key={r.id}>
                  <Td className="min-w-[18rem]">
                    {r.state !== "held" ? (
                      <Link className="underline decoration-line underline-offset-2 hover:decoration-current" href={`/console/records/${r.id}`}>{r.summary}</Link>
                    ) : (
                      <span>{r.summary}</span>
                    )}
                  </Td>
                  <Td>{r.context_type ?? "general"}</Td>
                  <Td><SeverityBadge severity={r.severity} /></Td>
                  <Td>
                    {r.state === "held" ? (
                      <Link href={`/console/review?connection=${encodeURIComponent(r.held_connection ?? "")}`}><Badge tone="warn">Held</Badge></Link>
                    ) : r.state === "retired" ? (
                      <Badge>Retired</Badge>
                    ) : (
                      <Badge tone="ok">Live</Badge>
                    )}
                  </Td>
                  <Td><Time iso={r.created_at} /></Td>
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
