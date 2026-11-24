"use client";

import { useState } from "react";
import {
  dropDeadLetters,
  listDeadLetters,
  listDeadLetterStreams,
  listFailedEvents,
  replayDeadLetters,
  retryEvents,
} from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import { formatAge, formatNumber, plural } from "../../lib/format";
import type { DeadLetterStream, FailedEvents } from "../../lib/types";
import { useApi, usePaged } from "../../lib/useApi";
import {
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Loading,
  More,
  Mono,
  Notice,
  PageHeader,
  Panel,
  Table,
  Td,
  Th,
  Time,
} from "../ui";

const useSelection = () => {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const toggle = (id: string, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  return { selected, toggle, clear: () => setSelected(new Set()) };
};

function FailedEventsPanel({ onNotice }: { onNotice: (text: string) => void }) {
  const [retrying, setRetrying] = useState<number | null>(null);
  const list = usePaged(async (cursor?: string) => {
    const page: FailedEvents = await listFailedEvents(cursor);
    if (!cursor) setRetrying(page.retrying);
    return page;
  }, []);
  const sel = useSelection();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const retry = async (target: { event_ids: string[] } | { all: true }) => {
    setBusy(true);
    setError(null);
    try {
      const res = await retryEvents(target);
      onNotice(`${plural(res.retried.length, "event")} queued again.${res.not_failed.length ? ` ${plural(res.not_failed.length, "event was", "events were")} no longer failed.` : ""}`);
      sel.clear();
      list.reload();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const ids = [...sel.selected];
  return (
    <Panel
      title="Failed events"
      actions={
        <div className="flex flex-wrap gap-2">
          {ids.length > 0 && <Button small variant="primary" disabled={busy} onClick={() => retry({ event_ids: ids })}>Retry {formatNumber(ids.length)} selected</Button>}
          <Button small disabled={busy || list.items.length === 0} onClick={() => retry({ all: true })}>Retry all</Button>
        </div>
      }
    >
      <p className="mb-3 text-sm text-muted">
        Events the worker gave up on after its retries.{retrying ? ` ${plural(retrying, "event is", "events are")} still being retried on their own.` : ""}
      </p>
      {error && <div className="mb-3"><ErrorState message={error} /></div>}
      {list.loading ? (
        <Loading />
      ) : list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : list.items.length === 0 ? (
        <EmptyState title="No failed events" />
      ) : (
        <>
          <Table label="Failed events">
            <thead><tr><Th className="w-8"><span className="sr-only">Select</span></Th><Th>Event</Th><Th>Source</Th><Th>Received</Th><Th className="text-right">Attempts</Th><Th>Last error</Th></tr></thead>
            <tbody>
              {list.items.map((e) => (
                <tr key={e.event_id}>
                  <Td><input type="checkbox" aria-label={`Select event ${e.event_id}`} checked={sel.selected.has(e.event_id)} onChange={(x) => sel.toggle(e.event_id, x.target.checked)} /></Td>
                  <Td><Mono>{e.event_id}</Mono></Td>
                  <Td>{e.source}</Td>
                  <Td><Time iso={e.ingested_at} /></Td>
                  <Td className="text-right tabular-nums">{e.attempts}</Td>
                  <Td className="max-w-[24rem] text-xs text-danger">{e.last_error ?? "–"}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <More list={list} />
        </>
      )}
    </Panel>
  );
}

function StreamEntries({ stream, onNotice, onChanged }: { stream: DeadLetterStream; onNotice: (text: string) => void; onChanged: () => void }) {
  const list = usePaged((cursor?: string) => listDeadLetters(stream.name, cursor), [stream.name]);
  const sel = useSelection();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dropping, setDropping] = useState<{ entry_ids: string[] } | { all: true } | null>(null);

  const after = (verb: string, n: number) => {
    onNotice(`${verb} ${plural(n, "entry", "entries")} in ${stream.name}.`);
    sel.clear();
    list.reload();
    onChanged();
  };

  const replay = async (target: { entry_ids: string[] } | { all: true }) => {
    setBusy(true);
    setError(null);
    try {
      after("Replayed", (await replayDeadLetters(stream.name, target)).done.length);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const ids = [...sel.selected];
  return (
    <Panel
      title={stream.name}
      actions={
        <div className="flex flex-wrap gap-2">
          {ids.length > 0 && (
            <>
              <Button small variant="primary" disabled={busy} onClick={() => replay({ entry_ids: ids })}>Replay {formatNumber(ids.length)} selected</Button>
              <Button small variant="danger" onClick={() => setDropping({ entry_ids: ids })}>Drop {formatNumber(ids.length)} selected</Button>
            </>
          )}
          <Button small disabled={busy || list.items.length === 0} onClick={() => replay({ all: true })}>Replay all</Button>
          <Button small variant="danger" disabled={list.items.length === 0} onClick={() => setDropping({ all: true })}>Drop all</Button>
        </div>
      }
    >
      {error && <div className="mb-3"><ErrorState message={error} /></div>}
      {list.loading ? (
        <Loading />
      ) : list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : list.items.length === 0 ? (
        <EmptyState title="This stream is empty" />
      ) : (
        <>
          <Table label={`Entries in ${stream.name}`}>
            <thead><tr><Th className="w-8"><span className="sr-only">Select</span></Th><Th>Entry</Th><Th>Event</Th><Th>Age</Th><Th>Error</Th></tr></thead>
            <tbody>
              {list.items.map((d) => (
                <tr key={d.entry_id}>
                  <Td><input type="checkbox" aria-label={`Select entry ${d.entry_id}`} checked={sel.selected.has(d.entry_id)} onChange={(x) => sel.toggle(d.entry_id, x.target.checked)} /></Td>
                  <Td><Mono>{d.entry_id}</Mono></Td>
                  <Td>{d.event_id ? <Mono>{d.event_id}</Mono> : "–"}</Td>
                  <Td className="whitespace-nowrap">{formatAge(d.age_seconds)}</Td>
                  <Td className="max-w-[24rem] text-xs text-danger">{d.error ?? "–"}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <More list={list} />
        </>
      )}
      {dropping && (
        <ConfirmDialog
          title={"all" in dropping ? `Drop every entry in ${stream.name}?` : `Drop ${plural(dropping.entry_ids.length, "entry", "entries")}?`}
          confirmLabel="Drop"
          onClose={() => setDropping(null)}
          onConfirm={async () => after("Dropped", (await dropDeadLetters(stream.name, dropping)).done.length)}
        >
          <p>
            Dropped entries are deleted from <Mono>{stream.name}</Mono> and the step that failed is not run for them again. This cannot be undone.
          </p>
        </ConfirmDialog>
      )}
    </Panel>
  );
}

function DeadLetters({ onNotice }: { onNotice: (text: string) => void }) {
  const streams = useApi(listDeadLetterStreams, []);
  const [open, setOpen] = useState<string | null>(null);
  const current = streams.data?.items.find((s) => s.name === open) ?? null;
  return (
    <div className="flex flex-col gap-4">
      <Panel title="Dead letters">
        <p className="mb-3 text-sm text-muted">Messages a worker could not handle after its retries. Replay sends them back to the stream they came from.</p>
        {streams.loading && !streams.data ? (
          <Loading />
        ) : streams.error ? (
          <ErrorState message={streams.status === 503 ? `Dead letters live in Redis, which is not reachable. ${streams.error}` : streams.error} onRetry={streams.reload} />
        ) : !streams.data || streams.data.items.length === 0 ? (
          <EmptyState title="No dead-letter streams" />
        ) : (
          <Table label="Dead-letter streams">
            <thead><tr><Th>Stream</Th><Th className="text-right">Entries</Th><Th>Oldest</Th><Th><span className="sr-only">Actions</span></Th></tr></thead>
            <tbody>
              {streams.data.items.map((s) => (
                <tr key={s.name}>
                  <Td><Mono>{s.name}</Mono></Td>
                  <Td className="text-right tabular-nums">{formatNumber(s.entries)}</Td>
                  <Td>{formatAge(s.oldest_age_seconds)}</Td>
                  <Td className="text-right">
                    <Button small aria-expanded={open === s.name} onClick={() => setOpen(open === s.name ? null : s.name)}>
                      {open === s.name ? "Hide entries" : "Show entries"}
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>
      {current && <StreamEntries key={current.name} stream={current} onNotice={onNotice} onChanged={streams.reload} />}
    </div>
  );
}

export default function Pipeline() {
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <>
      <PageHeader title="Pipeline" description="Work that did not finish. Retry or replay it once the cause is fixed." />
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      <div className="flex flex-col gap-4">
        <FailedEventsPanel onNotice={setNotice} />
        <DeadLetters onNotice={setNotice} />
      </div>
    </>
  );
}
