"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  deactivatePerson,
  deletePerson,
  getPerson,
  linkAccount,
  listAccounts,
  listPeople,
  listSessions,
  reactivatePerson,
  revokeSession,
  setPersonAdmin,
} from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import { plural } from "../../lib/format";
import type { Account, Session } from "../../lib/types";
import { useApi, usePaged } from "../../lib/useApi";
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
  More,
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
import { KeyTable } from "./Keys";

export default function People() {
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const [activeOnly, setActiveOnly] = useState(false);
  const list = usePaged((cursor?: string) => listPeople({ q: q || undefined, active: activeOnly ? true : undefined, cursor }), [q, activeOnly]);

  return (
    <>
      <PageHeader title="People" description="Everyone Checkcord knows: from sign-in, SCIM, connected systems or keys bound to them." />
      <form
        className="mb-4 flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          setQ(draft.trim());
        }}
      >
        <Field label="Search" className="w-72 max-w-full">
          <input type="search" className={inputClass} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Email or name" />
        </Field>
        <Button type="submit">Search</Button>
        <Checkbox label="Active only" checked={activeOnly} onChange={setActiveOnly} />
      </form>
      {list.loading ? (
        <Loading />
      ) : list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : list.items.length === 0 ? (
        <EmptyState title={q ? "Nobody matches this search" : "No people yet"}>
          {q ? "Try part of an email address." : "People appear when they sign in, are provisioned by SCIM, or get a key bound to them."}
        </EmptyState>
      ) : (
        <>
          <Table label="People">
            <thead><tr><Th>Person</Th><Th>Admin</Th><Th>State</Th><Th>Added</Th></tr></thead>
            <tbody>
              {list.items.map((p) => (
                <tr key={p.id}>
                  <Td>
                    <Link className="font-medium underline decoration-line underline-offset-2 hover:decoration-current" href={`/console/people/${p.id}`}>{p.email}</Link>
                    {p.display_name && <div className="text-xs text-muted">{p.display_name}</div>}
                  </Td>
                  <Td>{p.is_admin ? <Badge tone="accent">Admin</Badge> : <span className="text-muted">No</span>}</Td>
                  <Td>{p.active ? <Badge tone="ok">Active</Badge> : <Badge>Deactivated</Badge>}</Td>
                  <Td><Time iso={p.created_at} /></Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <More list={list} />
        </>
      )}
      <div className="mt-6"><UnmatchedAccounts /></div>
    </>
  );
}

const PLATFORM_NAMES: Record<string, string> = { slack: "Slack", ms_teams: "Microsoft Teams", github: "GitHub" };

function LinkDialog({ account, onClose, onDone }: { account: Account; onClose: () => void; onDone: (msg: string) => void }) {
  const [email, setEmail] = useState(account.source_email ?? "");
  const matches = useApi(() => (email.trim().length >= 2 ? listPeople({ q: email.trim(), active: true, limit: 10 }) : Promise.resolve({ items: [] })), [email]);
  const listId = `people-${account.platform}-${account.account}`;
  return (
    <ConfirmDialog
      title={`Match ${account.display_name ?? account.account} to a person`}
      confirmLabel="Match"
      danger={false}
      onClose={onClose}
      onConfirm={async () => {
        const res = await linkAccount(account.platform, account.account, email.trim());
        onDone(`${PLATFORM_NAMES[res.platform] ?? res.platform} account ${res.account} is now ${res.person_email}. Audiences that list it were refreshed.`);
      }}
    >
      <p>
        <Mono>{account.account}</Mono> on {PLATFORM_NAMES[account.platform] ?? account.platform}
        {account.source_email ? <> gave the email <Mono>{account.source_email}</Mono></> : " gave no email"}. A directory sync never changes a match made here.
      </p>
      <Field label="Person's email">
        <input type="email" list={listId} className={inputClass} value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@company.com" />
        <datalist id={listId}>
          {(matches.data?.items ?? []).map((p) => (
            <option key={p.id} value={p.email}>{p.display_name ?? p.email}</option>
          ))}
        </datalist>
      </Field>
    </ConfirmDialog>
  );
}

function UnmatchedAccounts() {
  const list = usePaged((cursor?: string) => listAccounts({ matched: false, cursor }), []);
  const [linking, setLinking] = useState<Account | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <Panel title="Accounts not matched to a person">
      <p className="mb-3 text-sm text-muted">
        Slack, Teams and GitHub accounts with no person. They are left out of the audience of every private channel and repository they are in, and a repository with one is written only public records.
      </p>
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {list.loading ? (
        <Loading />
      ) : list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : list.items.length === 0 ? (
        <EmptyState title="Every account is matched" />
      ) : (
        <>
          <Table label="Accounts not matched to a person">
            <thead><tr><Th>Account</Th><Th>Platform</Th><Th>Email it gave</Th><Th className="text-right">Restricted places</Th><Th><span className="sr-only">Actions</span></Th></tr></thead>
            <tbody>
              {list.items.map((a) => (
                <tr key={`${a.platform}:${a.account}`}>
                  <Td>
                    <div className="font-medium">{a.display_name ?? a.account}</div>
                    <div className="text-xs text-muted"><Mono>{a.account}</Mono></div>
                  </Td>
                  <Td>{PLATFORM_NAMES[a.platform] ?? a.platform}</Td>
                  <Td>{a.source_email ? <Mono>{a.source_email}</Mono> : <span className="text-muted">None</span>}</Td>
                  <Td className="text-right tabular-nums">{a.unmatched_in}</Td>
                  <Td className="text-right"><Button small onClick={() => setLinking(a)}>Match to a person</Button></Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <More list={list} />
        </>
      )}
      {linking && (
        <LinkDialog
          account={linking}
          onClose={() => setLinking(null)}
          onDone={(msg) => {
            list.update((items) => items.filter((x) => !(x.platform === linking.platform && x.account === linking.account)));
            setNotice(msg);
          }}
        />
      )}
    </Panel>
  );
}

function Sessions({ userId, email }: { userId: string; email: string }) {
  const [includeEnded, setIncludeEnded] = useState(false);
  const list = usePaged((cursor?: string) => listSessions({ user_id: userId, include_ended: includeEnded, cursor }), [userId, includeEnded]);
  const [revoking, setRevoking] = useState<Session | null>(null);
  return (
    <Panel title="Sessions" actions={<Checkbox label="Show ended sessions" checked={includeEnded} onChange={setIncludeEnded} />}>
      {list.loading ? (
        <Loading />
      ) : list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : list.items.length === 0 ? (
        <EmptyState title={includeEnded ? "No sessions" : "No active sessions"} />
      ) : (
        <>
          <Table label="Sessions">
            <thead><tr><Th>Signed in</Th><Th>Last seen</Th><Th>Expires</Th><Th>Browser</Th><Th>State</Th><Th><span className="sr-only">Actions</span></Th></tr></thead>
            <tbody>
              {list.items.map((s) => (
                <tr key={s.id}>
                  <Td><Time iso={s.created_at} /></Td>
                  <Td><Time iso={s.last_seen_at} /></Td>
                  <Td><Time iso={s.expires_at} /></Td>
                  <Td className="max-w-[18rem] text-xs">
                    {s.user_agent ?? "Unknown"}
                    {s.source && <div className="text-muted">from {s.source}</div>}
                  </Td>
                  <Td>{s.active ? <Badge tone="ok">Active</Badge> : <Badge>{s.revoked_at ? "Revoked" : "Ended"}</Badge>}</Td>
                  <Td className="text-right">{s.active && <Button small variant="danger" onClick={() => setRevoking(s)}>Revoke</Button>}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <More list={list} />
        </>
      )}
      {revoking && (
        <ConfirmDialog
          title="Revoke this session?"
          confirmLabel="Revoke session"
          onClose={() => setRevoking(null)}
          onConfirm={async () => {
            await revokeSession(revoking.id);
            list.update((items) => items.map((s) => (s.id === revoking.id ? { ...s, active: false, revoked_at: new Date().toISOString() } : s)));
          }}
        >
          <p>{email} is signed out of the browser that started this session{revoking.user_agent ? <> ({revoking.user_agent})</> : null}. They can sign in again.</p>
        </ConfirmDialog>
      )}
    </Panel>
  );
}

type Pending = "deactivate" | "delete" | null;

export function PersonDetail({ id }: { id: string }) {
  const router = useRouter();
  const person = useApi(() => getPerson(id), [id]);
  const [pending, setPending] = useState<Pending>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sessionsKey, setSessionsKey] = useState(0);

  if (person.loading && !person.data) return <Loading />;
  if (person.error) {
    return (
      <>
        <PageHeader title="Person" />
        {person.status === 404 ? <EmptyState title="No such person">They may have been deleted. <Link className="underline" href="/console/people">All people</Link></EmptyState> : <ErrorState message={person.error} onRetry={person.reload} />}
      </>
    );
  }
  const p = person.data;
  if (!p) return null;

  const act = async (fn: () => Promise<string>) => {
    setBusy(true);
    setError(null);
    try {
      setNotice(await fn());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <p className="mb-2 text-sm"><Link className="underline" href="/console/people">All people</Link></p>
      <PageHeader
        title={p.email}
        description={<>{p.display_name && <>{p.display_name} · </>}<Mono>{p.id}</Mono></>}
        actions={
          <>
            <Button
              disabled={busy}
              onClick={() =>
                act(async () => {
                  const res = await setPersonAdmin(p.id, !p.is_admin);
                  person.setData((prev) => prev && { ...prev, is_admin: res.is_admin });
                  return res.is_admin ? `${p.email} is now an admin.` : `${p.email} is no longer an admin.`;
                })
              }
            >
              {p.is_admin ? "Remove admin" : "Make admin"}
            </Button>
            {p.active ? (
              <Button variant="danger" onClick={() => setPending("deactivate")}>Deactivate</Button>
            ) : (
              <Button
                disabled={busy}
                onClick={() =>
                  act(async () => {
                    const res = await reactivatePerson(p.id);
                    person.setData((prev) => prev && { ...prev, active: res.active });
                    return `${p.email} is active again. Their keys work again; they need to sign in again.`;
                  })
                }
              >
                Reactivate
              </Button>
            )}
            <Button variant="danger" onClick={() => setPending("delete")}>Delete</Button>
          </>
        }
      />
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {error && <div className="mb-4"><ErrorState message={error} /></div>}
      <div className="mb-4 flex flex-wrap gap-2 text-sm">
        {p.active ? <Badge tone="ok">Active</Badge> : <Badge>Deactivated</Badge>}
        {p.is_admin && <Badge tone="accent">Admin</Badge>}
        <span className="text-muted">{plural(p.active_sessions, "active session")} · added <Time iso={p.created_at} /></span>
      </div>
      <div className="flex flex-col gap-4">
        <Panel title="API keys bound to them">
          {p.keys.length === 0 ? (
            <EmptyState title="No keys">Create one on the <Link className="underline" href="/console/keys">API keys</Link> page with their email.</EmptyState>
          ) : (
            <KeyTable
              keys={p.keys}
              showPerson={false}
              onRevoked={(k) => person.setData((prev) => prev && { ...prev, keys: prev.keys.map((x) => (x.id === k.id ? { ...x, active: false } : x)) })}
            />
          )}
        </Panel>
        <Sessions key={sessionsKey} userId={p.id} email={p.email} />
      </div>
      {pending === "deactivate" && (
        <ConfirmDialog
          title={`Deactivate ${p.email}?`}
          confirmLabel="Deactivate"
          onClose={() => setPending(null)}
          onConfirm={async () => {
            const res = await deactivatePerson(p.id);
            person.setData((prev) => prev && { ...prev, active: res.active, active_sessions: 0 });
            setSessionsKey((k) => k + 1);
            setNotice(`${p.email} is deactivated. ${plural(res.sessions_revoked, "session")} ended.`);
          }}
        >
          <p>Their {plural(p.keys.filter((k) => k.active).length, "active key")} and {plural(p.active_sessions, "session")} stop working at once. You can reactivate them later.</p>
        </ConfirmDialog>
      )}
      {pending === "delete" && (
        <ConfirmDialog
          title={`Delete ${p.email}?`}
          confirmLabel="Delete person"
          onClose={() => setPending(null)}
          onConfirm={async () => {
            await deletePerson(p.id);
            router.push("/console/people");
          }}
        >
          <p>Deletes {p.email}, their keys and sessions, and the events they wrote. This cannot be undone.</p>
          <PurgePreview load={() => deletePerson(p.id, true)} />
        </ConfirmDialog>
      )}
    </>
  );
}
