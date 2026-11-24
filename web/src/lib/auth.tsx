"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { getMe, getSignInConfig, logout, setAuthToken, setCsrf } from "./api";
import { errorMessage, errorStatus } from "./errors";
import type { Me, SignInConfig } from "./types";

const KEY_STORAGE = "checkcord.console.key";

export type AuthState =
  | { status: "loading" }
  | { status: "signed-out"; config: SignInConfig | null; configError: string | null; ended: boolean }
  | { status: "signed-in"; me: Me; config: SignInConfig | null }
  | { status: "error"; message: string };

interface AuthContextValue {
  state: AuthState;
  signInWithKey: (key: string) => Promise<string | null>;
  signOut: () => Promise<void>;
  reload: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const storedKey = (): string | null => {
  try {
    return window.sessionStorage.getItem(KEY_STORAGE);
  } catch {
    return null;
  }
};

const storeKey = (key: string | null) => {
  try {
    if (key) window.sessionStorage.setItem(KEY_STORAGE, key);
    else window.sessionStorage.removeItem(KEY_STORAGE);
  } catch {}
};

const applyCsrf = (me: Me | null) => {
  setCsrf(me?.csrf_header, me?.csrf_token);
};

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  const loadConfig = async (): Promise<[SignInConfig | null, string | null]> => {
    try {
      return [await getSignInConfig(), null];
    } catch (err) {
      return [null, errorMessage(err)];
    }
  };

  const reload = useCallback(async () => {
    const key = storedKey();
    setAuthToken(key);
    const [config, configError] = await loadConfig();
    try {
      const me = await getMe();
      applyCsrf(me);
      setState({ status: "signed-in", me, config });
    } catch (err) {
      applyCsrf(null);
      if (errorStatus(err) === 401) {
        if (key) {
          storeKey(null);
          setAuthToken(null);
        }
        setState({ status: "signed-out", config, configError, ended: Boolean(key) });
      } else {
        setState({ status: "error", message: errorMessage(err) });
      }
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loads the signed-in person once
    void reload();
  }, [reload]);

  useEffect(() => {
    const onRevoked = (event: Event) => {
      if ((event as CustomEvent).detail !== 401) return;
      storeKey(null);
      setAuthToken(null);
      applyCsrf(null);
      setState((prev) => ({
        status: "signed-out",
        config: prev.status === "signed-in" ? prev.config : null,
        configError: null,
        ended: true,
      }));
    };
    window.addEventListener("auth:revoked", onRevoked);
    return () => window.removeEventListener("auth:revoked", onRevoked);
  }, []);

  const signInWithKey = useCallback(async (key: string): Promise<string | null> => {
    setAuthToken(key);
    try {
      const me = await getMe();
      storeKey(key);
      applyCsrf(me);
      setState((prev) => ({
        status: "signed-in",
        me,
        config: prev.status === "signed-out" || prev.status === "signed-in" ? prev.config : null,
      }));
      return null;
    } catch (err) {
      setAuthToken(storedKey());
      return errorStatus(err) === 401 ? "That key was not accepted." : errorMessage(err);
    }
  }, []);

  const signOut = useCallback(async () => {
    const current = state;
    if (current.status === "signed-in" && current.me.auth === "session") {
      try {
        await logout();
      } catch {}
    }
    storeKey(null);
    setAuthToken(null);
    applyCsrf(null);
    setState({
      status: "signed-out",
      config: current.status === "signed-in" ? current.config : null,
      configError: null,
      ended: false,
    });
  }, [state]);

  const value = useMemo(
    () => ({ state, signInWithKey, signOut, reload }),
    [state, signInWithKey, signOut, reload]
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export const useAuth = (): AuthContextValue => {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth needs an AuthProvider");
  return value;
};

export const useMe = (): Me | null => {
  const { state } = useAuth();
  return state.status === "signed-in" ? state.me : null;
};

export const safeNext = (next: string | null | undefined): string =>
  next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/sign-in")
    ? next
    : "/console";
