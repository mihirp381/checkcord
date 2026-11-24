"use client";

import Link from "next/link";
import { useState } from "react";
import { listHeld, rejectHeld, releaseHeld } from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import { plural } from "../../lib/format";
import type { HeldRecord, ReviewTarget } from "../../lib/types";
import { useApi } from "../../lib/useApi";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Loading,
  Mono,
  Notice,
  PageHeader,
  Panel,
  Table,
  Td,
  Th,
  Time,
} from "../ui";

interface Pending {
  action: "release" | "reject";
  target: ReviewTarget;
  what: string;
}

export const sourcesText = (r: { sources: { platform: string | null; container: string | null; events: number }[] }) =>
  r.sources.map((s) => `${s.platform ?? "unknown"}${s.container ? ` · ${s.container}` : ""} (${s.events})`).join(", ");

export default function Review({ connection }: { connection?: string }) {
  const held = useApi(() => listHeld(connection), [connection]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState<Pending | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const groups = new Map<string, HeldRecord[]>();
  for (const r of held.data?.items ?? []) {
    groups.set(r.connection, [...(groups.get(r.connection) ?? []), r]);
  }

  const toggle = (id: string, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const run = async ({ action, target, what }: Pending) => {
    if (action === "release") {
      const res = await releaseHeld(target);
      const skipped = res.not_held?.length ? ` ${plural(res.not_held.length, "record was", "records were")} no longer held.` : "";
      setNotice(`Released ${what}: ${plural(res.records, "record")} now reach agents.${skipped}`);
    } else {
      const res = await rejectHeld(target);
      setNotice(`Rejected ${what}: ${plural(res.records_deleted ?? 0, "record")} and ${plural(res.events ?? 0, "event")} deleted.`);
    }
    setSelected(new Set());
    held.reload();
  };

  const releaseNow = async (p: Pending) => {
    setBusy(true);
    setError(null);
    try {
      await run(p);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const ids = [...selected];

  return (
    <>
      <PageHeader
        title="Review"
        description="Records from connections in review wait here. Agents cannot read them until you release them."
      />
      {connection && (
        <Notice tone="neutral">
          Showing <Mono>{connection}</Mono> only. <Link className="underline" href="/console/review">Show every connection</Link>
        </Notice>
      )}
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {error && <div className="mb-4"><ErrorState message={error} /></div>}
      {ids.length > 0 && (
        <div className="sticky top-0 z-10 mb-4 flex flex-wrap items-center gap-2 rounded-md border border-line bg-panel p-2 text-sm">
          <span className="mr-2">{plural(ids.length, "record")} selected</span>
          <Button small variant="primary" disabled={busy} onClick={() => releaseNow({ action: "release", target: { record_ids: ids }, what: plural(ids.length, "selected record") })}>
            Release selected
          </Button>
          <Button small variant="danger" onClick={() => setPending({ action: "reject", target: { record_ids: ids }, what: plural(ids.length, "selected record") })}>
            Reject selected
          </Button>
          <Button small variant="ghost" onClick={() => setSelected(new Set())}>Clear selection</Button>
        </div>
      )}
      {held.loading && !held.data ? (
        <Loading />
      ) : held.error ? (
        <ErrorState message={held.error} onRetry={held.reload} />
      ) : groups.size === 0 ? (
        <EmptyState title="Nothing is waiting for review">
          Turn review on for a connection on the <Link className="underline" href="/console/connections">Connections</Link> page to hold what it brings.
        </EmptyState>
      ) : (
        <div className="flex flex-col gap-4">
          {[...groups.entries()].map(([name, records]) => {
            const allOn = records.every((r) => selected.has(r.id));
            return (
              <Panel
                key={name}
                title={`${name} · ${plural(records.length, "held record")}`}
                actions={
                  <div className="flex flex-wrap gap-2">
                    <Button small onClick={() => setPending({ action: "release", target: { connection: name }, what: `everything held from ${name}` })}>Release all</Button>
                    <Button small variant="danger" onClick={() => setPending({ action: "reject", target: { connection: name }, what: `everything held from ${name}` })}>Reject all</Button>
                  </div>
                }
              >
                <Table label={`Held records from ${name}`}>
                  <thead>
                    <tr>
                      <Th className="w-8">
                        <input type="checkbox" aria-label={`Select every record from ${name}`} checked={allOn} onChange={(e) => records.forEach((r) => toggle(r.id, e.target.checked))} />
                      </Th>
                      <Th>Summary</Th><Th>Type</Th><Th>Sources</Th><Th>Created</Th><Th><span className="sr-only">Actions</span></Th>
                    </tr>
                  </thead>
                  <tbody>
                    {records.map((r) => (
                      <tr key={r.id}>
                        <Td>
                          <input type="checkbox" aria-label={`Select record ${r.summary.slice(0, 60)}`} checked={selected.has(r.id)} onChange={(e) => toggle(r.id, e.target.checked)} />
                        </Td>
                        <Td className="min-w-[16rem] whitespace-pre-wrap">{r.summary}</Td>
                        <Td>
                          <div className="flex flex-col items-start gap-1">
                            <span>{r.context_type ?? "general"}</span>
                            {r.severity === "blocking" && <Badge tone="danger">Blocking</Badge>}
                          </div>
                        </Td>
                        <Td className="text-xs">{sourcesText(r) || "–"}</Td>
                        <Td><Time iso={r.created_at} /></Td>
                        <Td>
                          <div className="flex flex-wrap justify-end gap-2">
                            <Button small disabled={busy} onClick={() => releaseNow({ action: "release", target: { record_id: r.id }, what: "the record" })}>Release</Button>
                            <Button small variant="danger" onClick={() => setPending({ action: "reject", target: { record_id: r.id }, what: "the record" })}>Reject</Button>
                          </div>
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </Panel>
            );
          })}
        </div>
      )}
      {pending && (
        <ConfirmDialog
          title={pending.action === "reject" ? `Reject ${pending.what}?` : `Release ${pending.what}?`}
          confirmLabel={pending.action === "reject" ? "Reject and delete" : "Release"}
          danger={pending.action === "reject"}
          onClose={() => setPending(null)}
          onConfirm={() => run(pending)}
        >
          {pending.action === "reject" ? (
            <>
            <p>
              This deletes {pending.what} and the events {"connection" in pending.target ? "held with them" : "they were made from"}. This cannot be undone.
            </p>
            {"connection" in pending.target && <p>The connection stays in review.</p>}
            </>
          ) : (
            <>
              <p>Agents and reviewers will be able to read {pending.what}, filtered by each person&apos;s access as usual.</p>
              {"connection" in pending.target && <p>This also turns review off, so new records from this connection go live straight away.</p>}
            </>
          )}
        </ConfirmDialog>
      )}
    </>
  );
}
