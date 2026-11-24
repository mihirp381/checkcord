import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from "axios";
import { vi } from "vitest";
import { api } from "../src/lib/api";

export type Reply = ((config: InternalAxiosRequestConfig) => unknown) | object | string | number | boolean | null;

export class Fail {
  constructor(
    public status: number,
    public detail: unknown = "failed"
  ) {}
}

export const routeOf = (config: InternalAxiosRequestConfig) =>
  `${(config.method ?? "get").toUpperCase()} ${config.url}`;

export const bodyOf = (config: InternalAxiosRequestConfig) =>
  typeof config.data === "string" ? JSON.parse(config.data) : config.data;

export function mockApi(routes: Record<string, Reply>) {
  const calls: InternalAxiosRequestConfig[] = [];
  const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
    calls.push(config);
    const route = routeOf(config);
    if (!(route in routes)) {
      throw new AxiosError(`no mock for ${route}`, "ERR_NETWORK", config);
    }
    const entry = routes[route];
    const value = typeof entry === "function" ? (entry as (c: InternalAxiosRequestConfig) => unknown)(config) : entry;
    if (value instanceof Fail) {
      const response = { status: value.status, statusText: "", data: { detail: value.detail }, headers: {}, config };
      throw new AxiosError("failed", "ERR_BAD_RESPONSE", config, null, response);
    }
    return { status: 200, statusText: "OK", data: value, headers: {}, config };
  });
  api.defaults.adapter = adapter as unknown as AxiosAdapter;
  const called = (route: string) => calls.filter((c) => routeOf(c) === route);
  return { calls, called, adapter };
}

export const page = <T>(items: T[], next_cursor: string | null = null) => ({ items, next_cursor });

export const ADMIN_ME = {
  tenant_id: "00000000-0000-0000-0000-000000000001",
  user_id: "00000000-0000-0000-0000-0000000000aa",
  email: "admin@example.com",
  is_admin: true,
  auth: "session",
  key_id: null,
  key_name: null,
  session_id: "s1",
  session_expires_at: "2030-01-01T00:00:00Z",
  session_idle_expires_at: null,
  csrf_token: "csrf-value",
  csrf_header: "X-CSRF-Token",
};

export const MEMBER_ME = { ...ADMIN_ME, email: "member@example.com", is_admin: false };

export const fakeKey = () => ["chk", "test", Math.random().toString(36).slice(2)].join("_");
