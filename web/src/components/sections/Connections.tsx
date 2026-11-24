"use client";

import Link from "next/link";
import { useState } from "react";
import { createInstallLink, disconnect, listConnections, setConnectionAudience, setConnectionReview, setConnectionScope } from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import { formatNumber } from "../../lib/format";
import type { AudienceMode, Connection, ConnectionAudience, ConnectorStatus, InstallLink, OAuthProvider } from "../../lib/types";
import { useApi } from "../../lib/useApi";
import { PurgeCountsList, PurgePreview } from "../PurgePreview";
import {
  Badge,
  Button,
  ConfirmDialog,
  CopyField,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  Mono,
  Notice,
  PageHeader,
  Panel,
  Table,
  Td,
  Th,
  Time,
  inputClass,
} from "../ui";

const PROVIDERS: [OAuthProvider, string][] = [
  ["slack", "Slack"],
  ["teams", "Microsoft Teams"],
  ["notion", "Notion"],
  ["linear", "Linear"],
];

const STATUS_TITLE: Record<ConnectorStatus, string> = {
  beta: "Reads the audience from the source, keeps it current, honours the scope, and has unit and integration tests. Not yet verified against the live system.",
  experimental: "Misses one or more of the beta criteria. Not yet verified against the live system.",
};

function StatusBadge({ status }: { status: ConnectorStatus }) {
  return (
    <span title={STATUS_TITLE[status]}>
      <Badge tone={status === "beta" ? "accent" : "neutral"}>{status}</Badge>
    </span>
  );
}

function InstallLinkPanel() {
  const [provider, setProvider] = useState<OAuthProvider>("slack");
  const [link, setLink] = useState<InstallLink | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    setError(null);
    setLink(null);
    try {
      setLink(await createInstallLink(provider));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Install link">
      <p className="mb-3 text-sm text-muted">
        Slack, Microsoft Teams, Notion and Linear are added by an admin of that system approving an
        install. Create a link and send it to them.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <Field label="System" className="w-48">
          <select className={inputClass} value={provider} onChange={(e) => setProvider(e.target.value as OAuthProvider)}>
            {PROVIDERS.map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </Field>
        <Button onClick={create} disabled={busy}>{busy ? "Creating…" : "Create install link"}</Button>
      </div>
      {error && <div className="mt-3"><ErrorState message={error} /></div>}
      {link && (
        <div className="mt-4 flex flex-col gap-2 rounded-md border border-warn/40 bg-warn-soft p-3">
          <CopyField label={`Install link for ${PROVIDERS.find(([p]) => p === link.provider)?.[1] ?? link.provider}`} value={link.url} />
          <p className="text-sm text-warn">
            Works once and expires in {Math.round(link.expires_in_seconds / 60)} minutes. Whoever
            opens it can connect their workspace, so send it only to the person who will approve it.
          </p>
          <div><Button small onClick={() => setLink(null)}>Hide link</Button></div>
        </div>
      )}
    </Panel>
  );
}

const CONTAINERS: Record<string, string> = {
  slack: "Slack channel ids, such as C0123ABCD",
  teams: "Teams team or channel ids",
  github: "The repository, as OWNER/NAME",
  jira: "Jira project keys, such as PAY",
  notion: "Notion database or page ids",
  linear: "Linear team ids or keys, such as ENG",
};

const lines = (text: string) => text.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

function ScopeDialog({ connection, onClose, onDone }: { connection: Connection; onClose: () => void; onDone: (c: Connection, msg: string) => void }) {
  const [include, setInclude] = useState((connection.scope?.include ?? []).join("\n"));
  const [exclude, setExclude] = useState((connection.scope?.exclude ?? []).join("\n"));
  const only = lines(include).length > 0;
  return (
    <ConfirmDialog
      title={`Scope of ${connection.connection}`}
      confirmLabel="Save scope"
      danger={false}
      onClose={onClose}
      onConfirm={async () => {
        const res = await setConnectionScope(connection.connection, lines(include), lines(exclude));
        onDone(
          { ...connection, scope: { include: res.include, exclude: res.exclude } },
          `Scope of ${connection.connection} saved. It applies to what arrives from now on.`
        );
      }}
    >
      <p>
        {CONTAINERS[connection.provider]}. One per line. Excluded ones are never ingested.
        If you list any to include, only those are ingested.
      </p>
      <Field label="Include only">
        <textarea className={inputClass} rows={3} value={include} onChange={(e) => setInclude(e.target.value)} placeholder="Everything" />
      </Field>
      <Field label="Exclude">
        <textarea className={inputClass} rows={3} value={exclude} onChange={(e) => setExclude(e.target.value)} placeholder="Nothing" />
      </Field>
      <p className="text-muted">
        {only ? "Only what is listed is ingested." : "Everything not excluded is ingested."} What was
        already ingested stays. To delete it, use <Mono>checkcord purge source</Mono>.
      </p>
    </ConfirmDialog>
  );
}

function scopeText(c: Connection) {
  if (!c.scope) return "-";
  if (c.scope.include.length) return `Only ${c.scope.include.length}`;
  if (c.scope.exclude.length) return `All but ${c.scope.exclude.length}`;
  return "Everything";
}

const AUDIENCE_FROM: Record<string, string> = { source: "From the source", event: "Set by each event" };

function settingText(a: ConnectionAudience) {
  if (a.mode === "everyone") return "Everyone";
  if (a.mode === "nobody") return "Nobody";
  const names = a.mode === "groups" ? a.groups : a.people;
  return `${names.join(", ")} (${a.members} ${a.members === 1 ? "person" : "people"})`;
}

function recomputeText(a: ConnectionAudience) {
  const r = a.recompute;
  if (r.state === "pending") return "Applying to stored content…";
  if (r.state === "running") return `Applying: ${formatNumber(r.done)} done`;
  return null;
}

function AudienceCell({ c }: { c: Connection }) {
  if (c.audience_from !== "admin") return <span className="text-muted">{AUDIENCE_FROM[c.audience_from ?? ""] ?? "-"}</span>;
  const rows = c.audience ?? [];
  if (rows.length === 0) return <Badge tone="warn">Nobody</Badge>;
  return (
    <div className="flex flex-col gap-0.5 text-xs">
      {rows.map((a) => (
        <div key={a.container ?? ""}>
          <span className="font-medium">{a.container ?? "All"}:</span> {settingText(a)}
          {recomputeText(a) && <div className="text-muted">{recomputeText(a)}</div>}
        </div>
      ))}
    </div>
  );
}

const MODES: [AudienceMode, string][] = [
  ["nobody", "Nobody"],
  ["everyone", "Everyone in the organisation"],
  ["groups", "Members of SCIM groups"],
  ["people", "These people"],
];

function AudienceDialog({ connection, onClose, onDone }: { connection: Connection; onClose: () => void; onDone: (msg: string) => void }) {
  const [container, setContainer] = useState("");
  const current = (connection.audience ?? []).find((a) => (a.container ?? "") === container.trim().toLowerCase());
  const [mode, setMode] = useState<AudienceMode>(current?.mode ?? "nobody");
  const [names, setNames] = useState("");
  return (
    <ConfirmDialog
      title={`Audience of ${connection.connection}`}
      confirmLabel="Save audience"
      danger={false}
      onClose={onClose}
      onConfirm={async () => {
        const listed = lines(names);
        await setConnectionAudience({
          connection: connection.connection,
          container: container.trim() || null,
          mode,
          groups: mode === "groups" ? listed : [],
          people: mode === "people" ? listed : [],
        });
        onDone(`Audience of ${connection.connection}${container.trim() ? ` for ${container.trim()}` : ""} saved. Stored events and records are being brought in line.`);
      }}
    >
      <p>
        {connection.provider} does not say who may read what it sends, so you decide. Until you do, nobody can read its records.
      </p>
      <Field label="For" hint={`Leave empty for the whole connection. ${CONTAINERS[connection.provider] ?? ""}`}>
        <input className={inputClass} value={container} onChange={(e) => setContainer(e.target.value)} placeholder="The whole connection" />
      </Field>
      <fieldset className="mt-2 flex flex-col gap-2">
        <legend className="mb-1 font-medium">Who can read it</legend>
        {MODES.map(([value, label]) => (
          <label key={value} className="flex items-start gap-2">
            <input type="radio" name="mode" checked={mode === value} onChange={() => setMode(value)} className="mt-1" />
            <span>{label}</span>
          </label>
        ))}
      </fieldset>
      {(mode === "groups" || mode === "people") && (
        <Field label={mode === "groups" ? "Group names" : "Emails"} hint="One per line">
          <textarea className={inputClass} rows={3} value={names} onChange={(e) => setNames(e.target.value)} />
        </Field>
      )}
    </ConfirmDialog>
  );
}

function DisconnectDialog({ connection, onClose, onDone }: { connection: Connection; onClose: () => void; onDone: (msg: React.ReactNode) => void }) {
  const [purge, setPurge] = useState(false);
  const name = connection.connection;
  return (
    <ConfirmDialog
      title={`Disconnect ${name}?`}
      confirmLabel={purge ? "Disconnect and delete content" : "Disconnect"}
      onClose={onClose}
      onConfirm={async () => {
        const res = await disconnect(name, purge);
        onDone(
          res.purged ? (
            <>Disconnected {name}. Deleted: <PurgeCountsList counts={res.purged} /></>
          ) : (
            <>Disconnected {name}. Its stored content was kept.</>
          )
        );
      }}
    >
      <p>
        Checkcord stops receiving from <Mono>{name}</Mono> and removes its stored credentials.
      </p>
      <fieldset className="mt-2 flex flex-col gap-2">
        <legend className="mb-1 font-medium">Content already stored</legend>
        <label className="flex items-start gap-2">
          <input type="radio" name="purge" checked={!purge} onChange={() => setPurge(false)} className="mt-1" />
          <span>Keep it. Records made from it stay available to agents.</span>
        </label>
        <label className="flex items-start gap-2">
          <input type="radio" name="purge" checked={purge} onChange={() => setPurge(true)} className="mt-1" />
          <span>Delete it: the events from this connection and the records made only from them. This cannot be undone.</span>
        </label>
      </fieldset>
      {purge && <PurgePreview load={() => disconnect(name, true, true).then((r) => r.purged ?? {})} />}
    </ConfirmDialog>
  );
}

export default function Connections() {
  const list = useApi(listConnections, []);
  const [notice, setNotice] = useState<React.ReactNode>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState<Connection | null>(null);
  const [scoping, setScoping] = useState<Connection | null>(null);
  const [audience, setAudience] = useState<Connection | null>(null);

  const toggleReview = async (c: Connection) => {
    setBusy(c.connection);
    setRowError(null);
    try {
      const res = await setConnectionReview(c.connection, c.state !== "review");
      list.setData((prev) => prev && { ...prev, items: prev.items.map((x) => (x.connection === c.connection ? { ...x, state: res.state as Connection["state"] } : x)) });
      setNotice(res.state === "review" ? `${c.connection} is now in review. New records from it are held.` : `${c.connection} is live. Records already held stay held until you release or reject them.`);
    } catch (err) {
      setRowError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <PageHeader title="Connections" description="The systems Checkcord receives from." />
      <Notice tone="neutral">
        New connections are created on the server with <Mono>checkcord connect</Mono>, for example{" "}
        <Mono>docker compose exec api checkcord connect github --repo OWNER/NAME</Mono>. See{" "}
        <Mono>docs/connectors.md</Mono> for every system.
      </Notice>
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {rowError && <div className="mb-4"><ErrorState message={rowError} /></div>}
      <div className="flex flex-col gap-4">
        <Panel title="Connected systems">
          {list.loading && !list.data ? (
            <Loading />
          ) : list.error ? (
            <ErrorState message={list.error} onRetry={list.reload} />
          ) : !list.data || list.data.items.length === 0 ? (
            <EmptyState title="Nothing is connected yet">Run <Mono>checkcord connect</Mono> on the server to add the first system.</EmptyState>
          ) : (
            <Table label="Connections">
              <thead>
                <tr><Th>Connection</Th><Th>State</Th><Th>Last event</Th><Th className="text-right">Events</Th><Th className="text-right">Held</Th><Th>Scope</Th><Th>Audience</Th><Th><span className="sr-only">Actions</span></Th></tr>
              </thead>
              <tbody>
                {list.data.items.map((c) => (
                  <tr key={c.connection}>
                    <Td>
                      <div className="flex items-center gap-1.5 font-medium">
                        {c.provider}
                        {c.status && <StatusBadge status={c.status} />}
                      </div>
                      <div className="text-xs text-muted [overflow-wrap:anywhere]">{c.identifier}</div>
                    </Td>
                    <Td>{c.state === "review" ? <Badge tone="warn">In review</Badge> : <Badge tone="ok">Live</Badge>}</Td>
                    <Td><Time iso={c.last_event_at} empty="No events yet" /></Td>
                    <Td className="text-right tabular-nums">{formatNumber(c.events)}</Td>
                    <Td className="text-right tabular-nums">
                      {c.held_records > 0 ? <Link className="underline" href={`/console/review?connection=${encodeURIComponent(c.connection)}`}>{formatNumber(c.held_records)}</Link> : "0"}
                    </Td>
                    <Td>
                      <div>{scopeText(c)}</div>
                      {c.scope_skipped > 0 && <div className="text-xs text-muted">{formatNumber(c.scope_skipped)} skipped in 7 days</div>}
                    </Td>
                    <Td><AudienceCell c={c} /></Td>
                    <Td>
                      <div className="flex flex-wrap justify-end gap-2">
                        {c.scope && <Button small onClick={() => setScoping(c)}>Scope</Button>}
                        {c.audience_from === "admin" && <Button small onClick={() => setAudience(c)}>Audience</Button>}
                        <Button small onClick={() => toggleReview(c)} disabled={busy === c.connection}>
                          {c.state === "review" ? "Turn review off" : "Turn review on"}
                        </Button>
                        {c.provider !== "custom" && <Button small variant="danger" onClick={() => setDisconnecting(c)}>Disconnect</Button>}
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Panel>
        <InstallLinkPanel />
      </div>
      {scoping && (
        <ScopeDialog
          connection={scoping}
          onClose={() => setScoping(null)}
          onDone={(updated, msg) => {
            list.setData((prev) => prev && { ...prev, items: prev.items.map((x) => (x.connection === updated.connection ? updated : x)) });
            setNotice(msg);
          }}
        />
      )}
      {audience && (
        <AudienceDialog
          connection={audience}
          onClose={() => setAudience(null)}
          onDone={(msg) => {
            setNotice(msg);
            list.reload();
          }}
        />
      )}
      {disconnecting && (
        <DisconnectDialog
          connection={disconnecting}
          onClose={() => setDisconnecting(null)}
          onDone={(msg) => {
            setNotice(msg);
            list.reload();
          }}
        />
      )}
    </>
  );
}
