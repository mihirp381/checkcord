"use client";

import { useState } from "react";
import { listAudiences, purgeSource, refreshAudience } from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import { formatNumber, plural } from "../../lib/format";
import type { Audience, AudienceRefreshOutcome } from "../../lib/types";
import { useApi } from "../../lib/useApi";
import { PurgePreview } from "../PurgePreview";
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
  Table,
  Td,
  Th,
  Time,
} from "../ui";

const keyOf = (a: { platform: string; container: string }) => `${a.platform}:${a.container}`;

const summarise = (outcomes: AudienceRefreshOutcome[]) => {
  const failed = outcomes.filter((o) => o.error);
  const changed = outcomes.reduce((n, o) => n + o.records_updated, 0);
  const parts = [`Refreshed ${plural(outcomes.length - failed.length, "audience")}`, `${plural(changed, "record")} updated`];
  if (failed.length) parts.push(`${failed.length} failed and kept their last known members`);
  return `${parts.join(", ")}.`;
};

const matched = (a: Audience) => {
  const total = a.member_count + (a.unmatched_count ?? 0);
  return `${formatNumber(a.member_count)} of ${formatNumber(total)} members matched`;
};

export default function Audiences() {
  const list = useApi(listAudiences, []);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; tone: "ok" | "warn" } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [purging, setPurging] = useState<Audience | null>(null);

  const refresh = async (target: Audience | null) => {
    setBusy(target ? keyOf(target) : "all");
    setError(null);
    try {
      const res = await refreshAudience(target ? { platform: target.platform, container: target.container } : { all: true });
      setNotice({ text: summarise(res.items), tone: res.items.some((o) => o.error) ? "warn" : "ok" });
      list.reload();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <PageHeader
        title="Audiences"
        description="Who can see each channel, project or repository Checkcord reads from. Records reach only people in their audience."
        actions={<Button onClick={() => refresh(null)} disabled={busy !== null}>{busy === "all" ? "Refreshing…" : "Refresh all"}</Button>}
      />
      {notice && <Notice tone={notice.tone} onDismiss={() => setNotice(null)}>{notice.text}</Notice>}
      {error && <div className="mb-4"><ErrorState message={error} /></div>}
      {list.loading && !list.data ? (
        <Loading />
      ) : list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : !list.data || list.data.items.length === 0 ? (
        <EmptyState title="No audiences stored yet">Checkcord stores an audience when a connected system first sends content from a private place.</EmptyState>
      ) : (
        <Table label="Audiences">
          <thead>
            <tr><Th>Source</Th><Th>Visibility</Th><Th className="text-right">Members</Th><Th>Last refreshed</Th><Th>Last problem</Th><Th><span className="sr-only">Actions</span></Th></tr>
          </thead>
          <tbody>
            {list.data.items.map((a) => (
              <tr key={keyOf(a)}>
                <Td>
                  <div className="font-medium">{a.platform}</div>
                  <div className="text-xs text-muted"><Mono>{a.container}</Mono></div>
                </Td>
                <Td>{a.is_public ? <Badge>Public</Badge> : <Badge tone="accent">Private</Badge>}</Td>
                <Td className="text-right tabular-nums">{a.is_public ? "Everyone" : matched(a)}</Td>
                <Td><Time iso={a.refreshed_at} /></Td>
                <Td className="max-w-[18rem] text-xs">
                  {a.last_error ? (
                    <>
                      <span className="text-danger">{a.last_error}</span>
                      {a.attempted_at && <div className="text-muted">tried <Time iso={a.attempted_at} /></div>}
                    </>
                  ) : "–"}
                </Td>
                <Td>
                  <div className="flex flex-wrap justify-end gap-2">
                    <Button small onClick={() => refresh(a)} disabled={busy !== null}>{busy === keyOf(a) ? "Refreshing…" : "Refresh"}</Button>
                    <Button small variant="danger" onClick={() => setPurging(a)}>Delete content</Button>
                  </div>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {purging && (
        <ConfirmDialog
          title={`Delete everything from ${purging.container}?`}
          confirmLabel="Delete content"
          onClose={() => setPurging(null)}
          onConfirm={async () => {
            const res = await purgeSource(purging.platform, purging.container);
            setNotice({ text: `Deleted ${plural(res.events, "event")} and ${plural(res.records_deleted, "record")} from ${purging.container}.`, tone: "ok" });
            list.reload();
          }}
        >
          <p>
            Deletes every event Checkcord stored from <Mono>{purging.container}</Mono> on {purging.platform}, the records made only from them and the stored audience. The connection stays, so new content still arrives. This cannot be undone.
          </p>
          <PurgePreview load={() => purgeSource(purging.platform, purging.container, true)} />
        </ConfirmDialog>
      )}
    </>
  );
}
