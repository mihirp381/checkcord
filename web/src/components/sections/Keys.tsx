"use client";

import Link from "next/link";
import { useState } from "react";
import { createKey, listKeys, revokeKey } from "../../lib/api";
import { errorMessage } from "../../lib/errors";
import type { ApiKey, KeyCreated } from "../../lib/types";
import { useApi } from "../../lib/useApi";
import {
  Badge,
  Button,
  Checkbox,
  ConfirmDialog,
  CopyField,
  Dialog,
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

export function KeyTable({
  keys,
  onRevoked,
  showPerson = true,
}: {
  keys: ApiKey[];
  onRevoked: (key: ApiKey) => void;
  showPerson?: boolean;
}) {
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  return (
    <>
      <Table label="API keys">
        <thead>
          <tr>
            <Th>Name</Th>
            {showPerson && <Th>Person</Th>}
            <Th>Rights</Th><Th>State</Th><Th>Created</Th><Th><span className="sr-only">Actions</span></Th>
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id}>
              <Td>
                <div className="font-medium">{k.name}</div>
                <div className="text-xs text-muted"><Mono>{k.id}</Mono></div>
              </Td>
              {showPerson && (
                <Td>{k.user_id ? <Link className="underline" href={`/console/people/${k.user_id}`}>{k.user_email ?? k.user_id}</Link> : <span className="text-muted">Not bound</span>}</Td>
              )}
              <Td>{k.is_admin ? <Badge tone="accent">Admin</Badge> : <Badge>Standard</Badge>}</Td>
              <Td>{k.active ? <Badge tone="ok">Active</Badge> : <Badge>Revoked</Badge>}</Td>
              <Td><Time iso={k.created_at} /></Td>
              <Td className="text-right">
                {k.active && <Button small variant="danger" onClick={() => setRevoking(k)}>Revoke</Button>}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
      {revoking && (
        <ConfirmDialog
          title={`Revoke the key "${revoking.name}"?`}
          confirmLabel="Revoke key"
          onClose={() => setRevoking(null)}
          onConfirm={async () => {
            await revokeKey(revoking.id);
            onRevoked(revoking);
          }}
        >
          <p>
            Anything using <span className="font-medium">{revoking.name}</span>
            {revoking.user_email ? <> (bound to {revoking.user_email})</> : null} stops working at once. A revoked key cannot be turned back on.
          </p>
        </ConfirmDialog>
      )}
    </>
  );
}

function CreateKeyDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (k: KeyCreated) => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [admin, setAdmin] = useState(false);
  const [createUser, setCreateUser] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setError("Give the key a name.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onCreated(await createKey({ name: name.trim(), admin, user_email: email.trim() || null, create_user: Boolean(email.trim()) && createUser }));
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <Dialog title="Create an API key" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
        <Field label="Name" hint="What the key is for, such as the laptop or service that uses it.">
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
        </Field>
        <Field label="Person (optional)" hint="Bind the key to a person so agents see only what they may see. Leave empty for a key that is not bound to anyone.">
          <input type="email" className={inputClass} value={email} onChange={(e) => setEmail(e.target.value)} placeholder="they@example.com" />
        </Field>
        {email.trim() && <Checkbox label="Add the person if they are not in Checkcord yet" checked={createUser} onChange={setCreateUser} />}
        <Checkbox label="Admin key (can use this console and every admin API)" checked={admin} onChange={setAdmin} />
        {error && <ErrorState message={error} />}
        <div className="mt-2 flex justify-end gap-2">
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>{busy ? "Creating…" : "Create key"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function ShowOnce({ created, onClose }: { created: KeyCreated; onClose: () => void }) {
  return (
    <Dialog title={`Key "${created.name}" created`} onClose={onClose} footer={<Button variant="primary" onClick={onClose}>I have stored the key</Button>}>
      <div className="flex flex-col gap-3">
        <p className="rounded-md bg-warn-soft px-3 py-2 text-warn">
          Copy the key now. It is shown only this once and Checkcord cannot show it again.
        </p>
        <CopyField label="API key" value={created.key} />
        <p className="text-muted">
          {created.user_email ? <>Bound to {created.user_email}. </> : <>Not bound to a person. </>}
          {created.is_admin ? "It has admin rights." : "It has no admin rights."}
        </p>
      </div>
    </Dialog>
  );
}

export default function Keys() {
  const keys = useApi(() => listKeys(), []);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<KeyCreated | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showRevoked, setShowRevoked] = useState(false);

  const all = keys.data?.items ?? [];
  const shown = showRevoked ? all : all.filter((k) => k.active);

  return (
    <>
      <PageHeader
        title="API keys"
        description="Keys let coding agents, scripts and people without sign-in reach Checkcord. Checkcord stores only a hash of each key."
        actions={<Button variant="primary" onClick={() => setCreating(true)}>Create key</Button>}
      />
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      <Panel actions={<Checkbox label="Show revoked keys" checked={showRevoked} onChange={setShowRevoked} />} title="Keys">
        {keys.loading && !keys.data ? (
          <Loading />
        ) : keys.error ? (
          <ErrorState message={keys.error} onRetry={keys.reload} />
        ) : shown.length === 0 ? (
          <EmptyState title={all.length ? "No active keys" : "No keys yet"}>Create a key for each agent or script that reads from Checkcord.</EmptyState>
        ) : (
          <KeyTable
            keys={shown}
            onRevoked={(k) => {
              keys.setData((prev) => prev && { ...prev, items: prev.items.map((x) => (x.id === k.id ? { ...x, active: false } : x)) });
              setNotice(`Revoked the key "${k.name}".`);
            }}
          />
        )}
      </Panel>
      {creating && (
        <CreateKeyDialog
          onClose={() => setCreating(false)}
          onCreated={(k) => {
            setCreating(false);
            setCreated(k);
            keys.reload();
          }}
        />
      )}
      {created && <ShowOnce created={created} onClose={() => setCreated(null)} />}
    </>
  );
}
