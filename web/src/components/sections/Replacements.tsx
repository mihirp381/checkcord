"use client";

import Link from "next/link";
import { useState } from "react";
import { decideSuggestion, listSuggestions } from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import type { Suggestion } from "../../lib/types";
import { usePaged } from "../../lib/useApi";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Loading,
  More,
  Notice,
  PageHeader,
  Panel,
  Time,
} from "../ui";

const BASIS = {
  stated: "The newer record says it replaces an earlier decision",
  model: "A model judged that the newer record replaces this one",
};

function Side({ label, id, summary, created }: { label: string; id: string; summary: string; created: string }) {
  return (
    <div className="flex-1">
      <p className="mb-1 text-xs font-medium uppercase text-muted">{label}</p>
      <Link className="underline decoration-line underline-offset-2 hover:decoration-current" href={`/console/records/${id}`}>
        <span className="whitespace-pre-wrap text-sm [overflow-wrap:anywhere]">{summary}</span>
      </Link>
      <p className="mt-1 text-xs text-muted"><Time iso={created} /></p>
    </div>
  );
}

export default function Replacements() {
  const list = usePaged((cursor?: string) => listSuggestions(cursor), []);
  const [accepting, setAccepting] = useState<Suggestion | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const drop = (id: string) => list.update((items) => items.filter((s) => s.id !== id));

  const dismiss = async (s: Suggestion) => {
    setBusy(s.id);
    setError(null);
    try {
      await decideSuggestion(s.id, "dismiss");
      drop(s.id);
      setNotice("Dismissed. Both records stay live.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <PageHeader
        title="Replacements"
        description="Newer records that may replace older ones. Nothing changes until you accept: the older record is then retired."
      />
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {error && <div className="mb-4"><ErrorState message={error} /></div>}
      {list.loading ? (
        <Loading />
      ) : list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : list.items.length === 0 ? (
        <EmptyState title="No suggested replacements">
          Checkcord suggests one when a new record says it replaces an earlier decision in the same scope.
        </EmptyState>
      ) : (
        <div className="flex flex-col gap-4">
          {list.items.map((s) => (
            <Panel
              key={s.id}
              title={BASIS[s.basis]}
              actions={
                <div className="flex flex-wrap gap-2">
                  <Button small variant="primary" onClick={() => setAccepting(s)}>Accept</Button>
                  <Button small disabled={busy === s.id} onClick={() => dismiss(s)}>Keep both</Button>
                </div>
              }
            >
              <div className="flex flex-col gap-4 sm:flex-row">
                <Side label="Older" id={s.record_id} summary={s.record_summary} created={s.record_created_at} />
                <Side label="Newer" id={s.replacement_id} summary={s.replacement_summary} created={s.replacement_created_at} />
              </div>
              <p className="mt-3 text-xs text-muted">
                <Badge>{s.basis}</Badge> Suggested <Time iso={s.created_at} />
              </p>
            </Panel>
          ))}
          <More list={list} />
        </div>
      )}
      {accepting && (
        <ConfirmDialog
          title="Retire the older record?"
          confirmLabel="Accept and retire"
          onClose={() => setAccepting(null)}
          onConfirm={async () => {
            await decideSuggestion(accepting.id, "accept");
            drop(accepting.id);
            setNotice("Accepted. The older record is retired and points to the newer one.");
          }}
        >
          <p>Agents, context files and pull-request checks stop seeing the older record. Nothing is deleted, and you can restore it from its record page.</p>
          <p className="whitespace-pre-wrap [overflow-wrap:anywhere] text-muted">{accepting.record_summary}</p>
        </ConfirmDialog>
      )}
    </>
  );
}
