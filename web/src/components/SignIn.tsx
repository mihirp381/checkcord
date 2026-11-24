"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { loginUrl } from "../lib/api";
import { safeNext, useAuth } from "../lib/auth";
import { Button, ErrorState, Field, Loading, Notice, inputClass } from "./ui";

const FAILURES: Record<string, string> = {
  bad_state: "That sign-in expired or was already used. Start again.",
  provider_error: "Your identity provider did not complete the sign-in. Try again.",
  invalid_token: "Checkcord could not check the answer from your identity provider. Try again, or ask an admin.",
  unverified_email: "Your identity provider did not confirm your email address. Ask an admin.",
  ambiguous_person: "More than one person in Checkcord has your email. Ask an admin to fix it.",
  deactivated_person: "Your account is deactivated in Checkcord.",
  unknown_person: "You are not set up in Checkcord yet. Ask an admin to add you, then sign in again.",
};

export default function SignIn() {
  const { state, signInWithKey, reload } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const ended = params.get("ended") === "1";
  const failure = params.get("error");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null);

  useEffect(() => {
    if (state.status === "signed-in") router.replace(next);
  }, [state, next, router]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const value = key.trim();
    if (!value) {
      setKeyError("Enter an API key.");
      return;
    }
    setBusy(true);
    setKeyError(null);
    const problem = await signInWithKey(value);
    setBusy(false);
    if (problem) setKeyError(problem);
    else setKey("");
  };

  let body: React.ReactNode;
  if (state.status === "loading") body = <Loading label="Checking your sign-in" />;
  else if (state.status === "signed-in") body = <Loading label="Signed in. Opening the console" />;
  else if (state.status === "error") body = <ErrorState message={state.message} onRetry={() => void reload()} />;
  else {
    const { config, configError } = state;
    body = (
      <>
        {ended && <Notice tone="warn">Your sign-in ended. Sign in again to carry on.</Notice>}
        {failure && <Notice tone="warn">{FAILURES[failure] ?? "Sign-in failed. Try again."}</Notice>}
        {config?.sign_in_enabled ? (
          <div className="flex flex-col gap-2">
            <Button
              variant="primary"
              className="w-full py-2"
              onClick={() => window.location.assign(loginUrl(config, `${window.location.origin}${next}`))}
            >
              Sign in
            </Button>
            <p className="text-xs text-muted">Uses your organization&apos;s identity provider.</p>
          </div>
        ) : configError ? (
          <ErrorState message={`Could not read the sign-in settings. ${configError}`} onRetry={() => void reload()} />
        ) : (
          <p className="text-sm text-muted">
            Sign-in through an identity provider is not set up on this server. Use an API key below.
            An admin can turn sign-in on by setting the <code className="font-mono">OIDC_*</code>{" "}
            settings on the API.
          </p>
        )}
        <form onSubmit={submit} className="mt-6 flex flex-col gap-3 border-t border-line pt-5" noValidate>
          <p className="text-sm font-medium">
            {config?.sign_in_enabled ? "Or continue with an API key" : "Continue with an API key"}
          </p>
          <Field
            label="API key"
            hint="Kept in this browser tab until you sign out or close the tab. Never sent in a link."
          >
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              className={`${inputClass} font-mono`}
              value={key}
              onChange={(e) => setKey(e.target.value)}
              aria-invalid={keyError ? true : undefined}
            />
          </Field>
          {keyError && <ErrorState message={keyError} />}
          <Button type="submit" disabled={busy}>
            {busy ? "Checking…" : "Continue"}
          </Button>
        </form>
      </>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm rounded-lg border border-line bg-panel p-6">
        <h1 className="text-lg font-semibold">Sign in to Checkcord</h1>
        <p className="mb-5 mt-1 text-sm text-muted">The admin console for your Checkcord server.</p>
        {body}
      </div>
    </main>
  );
}
