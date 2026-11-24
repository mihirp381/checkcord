"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { deleteRecord, getRecord, restoreRecord, retireRecord, setRecordScope, setRecordSeverity, setRecordType } from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import { plural } from "../../lib/format";
import type { RecordDetail as Detail, RecordType, ScopeRow, Severity } from "../../lib/types";
import { useApi } from "../../lib/useApi";
import { PurgePreview } from "../PurgePreview";
import {
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  Mono,
  Notice,
  PageHeader,
  Panel,
  Time,
  inputClass,
} from "../ui";
import { RECORD_TYPES, SeverityBadge } from "./Records";

const REMOVED = "[REMOVED:INSTRUCTIONS]";

const SCOPE_TYPES = [
  ["repo", "Repository"],
  ["path", "Path"],
  ["team", "Team"],
] as const;

function SeverityControl({ record, onSaved }: { record: Detail; onSaved: (s: Severity) => void }) {
  const [value, setValue] = useState<Severity>(record.severity);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      onSaved((await setRecordSeverity(record.id, value)).severity);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title="Severity">
      <p className="mb-3 text-sm text-muted">
        A pull request that touches a blocking record fails the Checkcord check. An advisory record is only cited.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Severity" className="w-44">
          <select className={inputClass} value={value} onChange={(e) => setValue(e.target.value as Severity)}>
            <option value="advisory">Advisory</option>
            <option value="blocking">Blocking</option>
          </select>
        </Field>
        <Button onClick={save} disabled={busy || value === record.severity}>{busy ? "Saving…" : "Save severity"}</Button>
      </div>
      {error && <div className="mt-3"><ErrorState message={error} /></div>}
    </Panel>
  );
}

function TypeControl({ record, onSaved }: { record: Detail; onSaved: (t: RecordType) => void }) {
  const current = (record.context_type ?? "general") as RecordType;
  const [value, setValue] = useState<RecordType>(current);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      onSaved((await setRecordType(record.id, value)).type);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title="Type">
      <p className="mb-3 text-sm text-muted">
        General is a reference record: agents can search it but it is not listed as a decision. The other types are decisions.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Type" className="w-56">
          <select className={inputClass} value={value} onChange={(e) => setValue(e.target.value as RecordType)}>
            {RECORD_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </Field>
        <Button onClick={save} disabled={busy || value === current}>{busy ? "Saving…" : "Save type"}</Button>
      </div>
      {error && <div className="mt-3"><ErrorState message={error} /></div>}
    </Panel>
  );
}

function ScopeEditor({ record, onSaved }: { record: Detail; onSaved: (rows: ScopeRow[], locked: boolean) => void }) {
  const [rows, setRows] = useState<ScopeRow[]>(record.scope_rows);
  const [locked, setLocked] = useState(record.is_locked);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const edit = (i: number, patch: Partial<ScopeRow>) => {
    setSaved(false);
    setRows((r) => r.map((row, j) => (j === i ? { ...row, ...patch } : row)));
  };

  const save = async () => {
    const clean = rows.map((r) => ({ ...r, scope_value: r.scope_value.trim() })).filter((r) => r.scope_value);
    setBusy(true);
    setError(null);
    try {
      const res = await setRecordScope(record.id, clean, locked);
      setRows(res.scope_rows);
      setSaved(true);
      onSaved(res.scope_rows, res.is_locked);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Scope">
      <p className="mb-3 text-sm text-muted">
        Where this record applies. With no entries it applies to the whole organization.
      </p>
      {rows.length === 0 && <p className="mb-3 text-sm">Whole organization</p>}
      <ul className="flex flex-col gap-2">
        {rows.map((row, i) => (
          <li key={i} className="flex flex-wrap items-end gap-2">
            <Field label="Applies to" className="w-36">
              <select className={inputClass} value={row.scope_type} onChange={(e) => edit(i, { scope_type: e.target.value })}>
                {SCOPE_TYPES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
              </select>
            </Field>
            <Field label="Value" className="min-w-[12rem] flex-1">
              <input className={inputClass} value={row.scope_value} onChange={(e) => edit(i, { scope_value: e.target.value })} placeholder={row.scope_type === "path" ? "src/billing/" : row.scope_type === "team" ? "payments" : "OWNER/NAME"} />
            </Field>
            <Button small variant="ghost" onClick={() => { setSaved(false); setRows((r) => r.filter((_, j) => j !== i)); }} aria-label={`Remove scope entry ${i + 1}`}>
              Remove
            </Button>
          </li>
        ))}
      </ul>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button small onClick={() => { setSaved(false); setRows((r) => [...r, { scope_type: "repo", scope_value: "" }]); }}>Add entry</Button>
        <Checkbox label="Lock the scope against automatic changes" checked={locked} onChange={(v) => { setSaved(false); setLocked(v); }} />
      </div>
      <div className="mt-3 flex items-center gap-3">
        <Button onClick={save} disabled={busy}>{busy ? "Saving…" : "Save scope"}</Button>
        {saved && <span role="status" className="text-sm text-ok">Saved.</span>}
      </div>
      {error && <div className="mt-3"><ErrorState message={error} /></div>}
    </Panel>
  );
}

function Lifecycle({ record, onChanged }: { record: Detail; onChanged: (notice: string) => void }) {
  const [retiring, setRetiring] = useState(false);
  const [replacedBy, setReplacedBy] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restore = async () => {
    setBusy(true);
    setError(null);
    try {
      await restoreRecord(record.id);
      onChanged("Restored. Agents, context files and pull-request checks see the record again.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title="Lifecycle" className="lg:col-span-3">
      {record.state === "retired" ? (
        <>
          <p className="text-sm">
            Retired <Time iso={record.retired_at} /> by {record.retired_by ?? "unknown"}.
            {record.replaced_by && (
              <> Replaced by <Link className="underline" href={`/console/records/${record.replaced_by}`}><Mono>{record.replaced_by}</Mono></Link>.</>
            )}
          </p>
          {record.retired_reason && <p className="mt-1 text-sm text-muted [overflow-wrap:anywhere]">{record.retired_reason}</p>}
          <p className="mt-2 text-sm text-muted">Agents, context files and pull-request checks do not see it.</p>
          <div className="mt-3"><Button onClick={restore} disabled={busy}>{busy ? "Restoring…" : "Restore record"}</Button></div>
        </>
      ) : (
        <>
          <p className="text-sm text-muted">
            Retire a record that no longer holds. Agents, context files and pull-request checks stop seeing it. You can restore it later.
          </p>
          {record.suggested_replacements.length > 0 && (
            <ul className="mt-2 flex flex-col gap-1 text-sm">
              {record.suggested_replacements.map((s) => (
                <li key={s.id} className="[overflow-wrap:anywhere]">
                  Suggested replacement: <Link className="underline" href={`/console/records/${s.replacement_id}`}><Mono>{s.replacement_id}</Mono></Link>{" "}
                  ({s.basis === "stated" ? "the newer record says so" : "a model judged so"}). Decide on the{" "}
                  <Link className="underline" href="/console/replacements">Replacements</Link> page.
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3"><Button variant="danger" onClick={() => setRetiring(true)}>Retire record</Button></div>
        </>
      )}
      {error && <div className="mt-3"><ErrorState message={error} /></div>}
      {retiring && (
        <ConfirmDialog
          title="Retire this record?"
          confirmLabel="Retire record"
          onClose={() => setRetiring(false)}
          onConfirm={async () => {
            await retireRecord(record.id, { replaced_by: replacedBy.trim() || undefined, reason: reason.trim() || undefined });
            onChanged("Retired. Agents, context files and pull-request checks no longer see the record.");
          }}
        >
          <p>Agents, context files and pull-request checks stop seeing <Mono>{record.id}</Mono>. Nothing is deleted, and you can restore it.</p>
          <Field label="Replaced by (optional record id)">
            <input className={inputClass} value={replacedBy} onChange={(e) => setReplacedBy(e.target.value)} />
          </Field>
          <Field label="Reason (optional)">
            <input className={inputClass} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        </ConfirmDialog>
      )}
    </Panel>
  );
}

export default function RecordDetail({ id }: { id: string }) {
  const router = useRouter();
  const record = useApi(() => getRecord(id), [id]);
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  if (record.loading && !record.data) return <Loading />;
  if (record.error) {
    return (
      <>
        <PageHeader title="Record" />
        {record.status === 404 ? (
          <EmptyState title="No record with this id">
            It may have been deleted, or it is held for review. See <Link className="underline" href="/console/review">Review</Link>.
          </EmptyState>
        ) : (
          <ErrorState message={record.error} onRetry={record.reload} />
        )}
      </>
    );
  }
  const r = record.data;
  if (!r) return null;

  return (
    <>
      <p className="mb-2 text-sm"><Link className="underline" href="/console/records">All records</Link></p>
      <PageHeader
        title={`${r.context_type ?? "general"} record`}
        description={<Mono>{r.id}</Mono>}
        actions={<Button variant="danger" onClick={() => setDeleting(true)}>Delete record</Button>}
      />
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="Summary" className="lg:col-span-2">
          {r.may_be_replaced_by && (
            <p role="note" className="mb-3 rounded border border-line px-3 py-2 text-sm [overflow-wrap:anywhere]">
              A newer record from <Time iso={r.may_be_replaced_by.created_at} /> may replace this; an admin has not confirmed it yet:{" "}
              <Link className="underline" href={`/console/records/${r.may_be_replaced_by.record_id}`}>{r.may_be_replaced_by.summary}</Link>
            </p>
          )}
          <p className="whitespace-pre-wrap text-sm [overflow-wrap:anywhere]">{r.summary}</p>
          {[r.summary, ...r.updates.map((u) => u.text)].some((t) => t.includes(REMOVED)) && (
            <p role="note" className="mt-3 text-xs text-muted">
              <Mono>{REMOVED}</Mono> marks text that tried to give orders to AI tools. It was removed before agents could read it; <Mono>checkcord records show --raw</Mono> prints the original.
            </p>
          )}
          <p className="mt-3 text-xs text-muted">Shown with the same redaction agents get. The summary is the first event&apos;s text; later events that agree with it are listed as updates.</p>
          {r.updates.length > 0 && (
            <section aria-label="Updates" className="mt-4">
              <h3 className="text-sm font-medium">Updates</h3>
              <ul className="mt-2 flex flex-col gap-2 text-sm">
                {r.updates.map((u, i) => (
                  <li key={i} className="[overflow-wrap:anywhere]">
                    <span className="text-muted"><Time iso={u.happened_at} /> · {u.source.platform ?? "unknown"} {u.source.type ?? ""}{u.audience ? ` · ${u.audience === "public" ? "readable by everyone" : `readable by ${u.audience}`}` : ""}</span>
                    <p className="whitespace-pre-wrap">{u.text}</p>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </Panel>
        <Panel title="Details">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 text-sm">
            <dt className="text-muted">State</dt><dd>{r.state === "retired" ? <Badge>Retired</Badge> : <Badge tone="ok">Live</Badge>}</dd>
            <dt className="text-muted">Type</dt><dd>{r.context_type ?? "general"}</dd>
            <dt className="text-muted">Severity</dt><dd><SeverityBadge severity={r.severity} /></dd>
            <dt className="text-muted">Who can read it</dt><dd>{r.audience === "public" ? "Everyone in the organization" : r.audience}</dd>
            <dt className="text-muted">Scope</dt><dd className="[overflow-wrap:anywhere]">{r.scope.join(", ")}</dd>
            <dt className="text-muted" title="A weighted count of supporting events by source type that decays with age, not a probability">Evidence score</dt><dd>{r.confidence_score === null ? "–" : r.confidence_score.toFixed(2)}</dd>
            <dt className="text-muted">Created</dt><dd><Time iso={r.created_at} /></dd>
            <dt className="text-muted">Events</dt>
            <dd>
              {plural(r.supporting_events.count, "event")}
              {r.supporting_events.first && <>, <Time iso={r.supporting_events.first} /> to <Time iso={r.supporting_events.last} /></>}
            </dd>
          </dl>
        </Panel>
        <Panel title="Sources" className="lg:col-span-3">
          {r.sources.length === 0 ? (
            <p className="text-sm text-muted">No source events are linked.</p>
          ) : (
            <ul className="flex flex-col gap-1 text-sm">
              {r.sources.map((s, i) => (
                <li key={i} className="[overflow-wrap:anywhere]">
                  <span className="font-medium">{s.platform ?? "unknown"}</span>
                  {s.container && <> · {s.container}</>} · {plural(s.events, "event")}
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <Lifecycle
          record={r}
          onChanged={(text) => {
            setNotice(text);
            record.reload();
          }}
        />
        <SeverityControl
          record={r}
          onSaved={(severity) => {
            record.setData((prev) => prev && { ...prev, severity });
            setNotice(`Severity set to ${severity}.`);
          }}
        />
        <TypeControl
          record={r}
          onSaved={(type) => {
            record.setData((prev) => prev && { ...prev, context_type: type });
            setNotice(`Type set to ${type}.`);
          }}
        />
        <div className="lg:col-span-2">
          <ScopeEditor
            record={r}
            onSaved={(rows, locked) =>
              record.setData((prev) => prev && {
                ...prev,
                scope_rows: rows,
                is_locked: locked,
                scope: rows.length ? rows.map((x) => `${x.scope_type}:${x.scope_value}`) : ["org"],
              })
            }
          />
        </div>
      </div>
      {deleting && (
        <ConfirmDialog
          title="Delete this record?"
          confirmLabel="Delete record"
          onClose={() => setDeleting(false)}
          onConfirm={async () => {
            await deleteRecord(r.id);
            router.push("/console/records");
          }}
        >
          <p>Deletes the record <Mono>{r.id}</Mono> and the events linked only to it. Agents stop seeing it at once. This cannot be undone.</p>
          <PurgePreview load={() => deleteRecord(r.id, true)} />
        </ConfirmDialog>
      )}
    </>
  );
}
