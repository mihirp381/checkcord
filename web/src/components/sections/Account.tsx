"use client";

import Link from "next/link";
import { useAuth } from "../../lib/auth";
import { Badge, Mono, PageHeader, Panel, Time } from "../ui";

export default function Account() {
  const { state } = useAuth();
  if (state.status !== "signed-in") return null;
  const { me } = state;
  return (
    <>
      <PageHeader title="Your account" description="Who Checkcord sees you as in this browser." />
      <Panel className="max-w-2xl">
        <dl className="grid grid-cols-1 gap-x-4 gap-y-3 text-sm sm:grid-cols-[12rem_1fr]">
          <dt className="text-muted">Email</dt>
          <dd className="[overflow-wrap:anywhere]">{me.email ?? <span className="text-muted">Not bound to a person</span>}</dd>
          <dt className="text-muted">Rights</dt>
          <dd>{me.is_admin ? <Badge tone="accent">Admin</Badge> : <Badge>Member</Badge>}</dd>
          <dt className="text-muted">Signed in with</dt>
          <dd>{me.auth === "session" ? "Your identity provider" : <>The API key <span className="font-medium">{me.key_name}</span>, kept in this tab</>}</dd>
          {me.auth === "session" && (
            <>
              <dt className="text-muted">Sign-in ends</dt>
              <dd className="flex flex-col gap-1">
                <Time iso={me.session_expires_at} empty="Unknown" />
                {me.session_idle_expires_at && <span className="text-xs text-muted">or earlier if idle until <Time iso={me.session_idle_expires_at} /></span>}
              </dd>
            </>
          )}
          <dt className="text-muted">Organization ID</dt>
          <dd><Mono>{me.tenant_id}</Mono></dd>
          {me.user_id && (
            <>
              <dt className="text-muted">Person ID</dt>
              <dd><Mono>{me.user_id}</Mono></dd>
            </>
          )}
        </dl>
      </Panel>
      {!me.is_admin && (
        <p className="mt-4 max-w-2xl text-sm text-muted">
          Your account can open the <Link className="underline" href="/console/graph">graph</Link>. Admin sections need admin rights.
        </p>
      )}
    </>
  );
}
