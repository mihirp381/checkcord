import axios from 'axios';
import type {
  Account,
  AccountLinked,
  ApiKey,
  Audience,
  AudienceRefreshOutcome,
  AuditEntry,
  AuditFilters,
  Connection,
  ConnectionAudience,
  ConnectionAudienceChange,
  ConnectionScope,
  DeadLetter,
  DeadLetterDone,
  DeadLetterStream,
  Disconnected,
  FailedEvents,
  GraphRepository,
  Health,
  HeldRecord,
  InstallLink,
  KeyCreate,
  KeyCreated,
  Me,
  OAuthProvider,
  Page,
  Person,
  PersonChanged,
  PersonDeleted,
  PersonDetail,
  PurgeCounts,
  RecordDetail,
  RecordFilters,
  RecordSummary,
  Rejected,
  Released,
  Retried,
  ReviewTarget,
  Revoked,
  ScopeRow,
  ScopeUpdated,
  Session,
  RecordType,
  Severity,
  SignInConfig,
  Stats,
  Suggestion,
  Usage,
} from './types';

declare module 'axios' {
  interface AxiosRequestConfig {
    // A 401/403 on this request is handled by the caller and does not end the session.
    skipAuthRevoked?: boolean;
  }
}

const FALLBACK_API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
const SAFE_METHODS = new Set(['get', 'head', 'options']);

let authToken: string | null = null;
let csrf: { header: string; token: string } | null = null;

export const setAuthToken = (token: string | null) => {
  authToken = token;
};

export const setCsrf = (header: string | null | undefined, token: string | null | undefined) => {
  csrf = header && token ? { header, token } : null;
};

// The server renders the address into the page, so it can change without a rebuild.
export const apiBaseUrl = (): string => {
  const fromPage =
    typeof document !== 'undefined'
      ? document.querySelector('meta[name="checkcord-api-url"]')?.getAttribute('content')
      : null;
  return (fromPage || FALLBACK_API_URL).replace(/\/+$/, '');
};

export const api = axios.create({ timeout: 10000, withCredentials: true });

api.interceptors.request.use((config) => {
  config.baseURL ??= apiBaseUrl();
  if (authToken) {
    config.headers.Authorization = `Bearer ${authToken}`;
  } else if (csrf && !SAFE_METHODS.has((config.method || 'get').toLowerCase())) {
    config.headers[csrf.header] = csrf.token;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status;
    if ((status === 401 || status === 403) && !error.config?.skipAuthRevoked) {
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('auth:revoked', { detail: status }));
      }
    }
    return Promise.reject(error);
  }
);

const A = '/api/v1/admin';
const quiet = { skipAuthRevoked: true };
const enc = encodeURIComponent;

const clean = (params: object): Record<string, string | number | boolean> =>
  Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
  );

const get = async <T>(path: string, params: object = {}): Promise<T> =>
  (await api.get<T>(path, { params: clean(params) })).data;

const send = async <T>(method: 'post' | 'put' | 'patch' | 'delete', path: string, body?: unknown, params: object = {}): Promise<T> =>
  (await api.request<T>({ method, url: path, data: body, params: clean(params) })).data;

export const getSignInConfig = () => get<SignInConfig>('/api/v1/auth/config');

export const getMe = async (): Promise<Me> =>
  (await api.get<Me>('/api/v1/auth/me', quiet)).data;

export const logout = async (): Promise<void> => {
  await api.post('/api/v1/auth/logout', undefined, quiet);
};

export const loginUrl = (config: SignInConfig, returnTo: string): string => {
  const base = config.login_url || '/api/v1/auth/login';
  const url = new URL(base, apiBaseUrl() + '/');
  url.searchParams.set('return_to', returnTo);
  return url.toString();
};

export const getHealth = () => get<Health>(`${A}/health`);
export const getStats = (days: number) => get<Stats>(`${A}/stats`, { days });
export const getUsage = (days: number) => get<Usage>(`${A}/usage`, { days });

export const listConnections = () => get<Page<Connection>>(`${A}/connections`);
export const setConnectionReview = (connection: string, inReview: boolean) =>
  send<{ connection: string; state: string }>('post', `${A}/connections/review`, {
    connection,
    in_review: inReview,
  });
export const disconnect = (connection: string, purge: boolean, dryRun = false) =>
  send<Disconnected>('post', `${A}/connections/disconnect`, {
    connection,
    purge,
    dry_run: dryRun,
  });
export const setConnectionScope = (connection: string, include: string[], exclude: string[]) =>
  send<ConnectionScope>('put', `${A}/connections/scope`, { connection, include, exclude });
export const setConnectionAudience = (body: ConnectionAudienceChange) =>
  send<ConnectionAudience>('put', `${A}/connections/audience`, body);
export const createInstallLink = (provider: OAuthProvider) =>
  send<InstallLink>('post', `${A}/connections/install-link`, { provider });

export const listHeld = (connection?: string) =>
  get<Page<HeldRecord>>(`${A}/review`, { connection });
export const releaseHeld = (target: ReviewTarget) =>
  send<Released>('post', `${A}/review/release`, target);
export const rejectHeld = (target: ReviewTarget) =>
  send<Rejected>('post', `${A}/review/reject`, target);

export const listRecords = (filters: RecordFilters, cursor?: string, limit = 50) =>
  get<Page<RecordSummary>>(`${A}/records`, { ...filters, cursor, limit });
export const getRecord = (id: string) => get<RecordDetail>(`${A}/records/${enc(id)}`);
export const deleteRecord = (id: string, dryRun = false) =>
  send<PurgeCounts>('delete', `${A}/records/${enc(id)}`, undefined, { dry_run: dryRun });
export const setRecordSeverity = (id: string, severity: Severity) =>
  send<{ id: string; severity: Severity }>('patch', `${A}/records/${enc(id)}/severity`, {
    severity,
  });
export const setRecordType = (id: string, type: RecordType) =>
  send<{ id: string; type: RecordType }>('patch', `${A}/records/${enc(id)}/type`, { type });
export const retireRecord = (id: string, body: { replaced_by?: string; reason?: string }) =>
  send<{ id: string; state: 'retired' }>('post', `${A}/records/${enc(id)}/retire`, body);
export const restoreRecord = (id: string) =>
  send<{ id: string; state: 'live' }>('post', `${A}/records/${enc(id)}/restore`);
export const listSuggestions = (cursor?: string, limit = 50) =>
  get<Page<Suggestion>>(`${A}/records/suggestions`, { cursor, limit });
export const decideSuggestion = (id: string, decision: 'accept' | 'dismiss') =>
  send<{ suggestion: string; decision: string }>('post', `${A}/records/suggestions/${enc(id)}/${decision}`);
export const setRecordScope = (id: string, scopeRows: ScopeRow[], isLocked: boolean) =>
  send<ScopeUpdated>('patch', `${A}/records/${enc(id)}/scope`, {
    scope_rows: scopeRows,
    is_locked: isLocked,
  });

export const listPeople = (params: { q?: string; active?: boolean; cursor?: string; limit?: number }) =>
  get<Page<Person>>(`${A}/users`, params);
export const listAccounts = (params: { platform?: string; matched?: boolean; cursor?: string; limit?: number }) =>
  get<Page<Account>>(`${A}/accounts`, params);
export const linkAccount = (platform: string, account: string, email: string) =>
  send<AccountLinked>('post', `${A}/accounts/link`, { platform, account, email });
export const unlinkAccount = (platform: string, account: string) =>
  send<{ platform: string; account: string; unlinked: boolean }>('post', `${A}/accounts/unlink`, { platform, account });
export const getPerson = (id: string) => get<PersonDetail>(`${A}/users/${enc(id)}`);
export const setPersonAdmin = (id: string, isAdmin: boolean) =>
  send<Person>('put', `${A}/users/${enc(id)}/admin`, { is_admin: isAdmin });
export const deactivatePerson = (id: string) =>
  send<PersonChanged>('post', `${A}/users/${enc(id)}/deactivate`);
export const reactivatePerson = (id: string) =>
  send<PersonChanged>('post', `${A}/users/${enc(id)}/reactivate`);
export const deletePerson = (id: string, dryRun = false) =>
  send<PersonDeleted>('delete', `${A}/users/${enc(id)}`, undefined, { dry_run: dryRun });

export const listSessions = (params: { user_id?: string; include_ended?: boolean; cursor?: string }) =>
  get<Page<Session>>(`${A}/sessions`, params);
export const revokeSession = (id: string) =>
  send<Revoked>('post', `${A}/sessions/${enc(id)}/revoke`);

export const listKeys = (userId?: string) => get<Page<ApiKey>>(`${A}/keys`, { user_id: userId });
export const createKey = (body: KeyCreate) => send<KeyCreated>('post', `${A}/keys`, body);
export const revokeKey = (id: string) => send<Revoked>('post', `${A}/keys/${enc(id)}/revoke`);

export const listAudit = (filters: AuditFilters, cursor?: string, limit = 100) =>
  get<Page<AuditEntry>>(`${A}/audit`, { ...filters, cursor, limit });
export const exportAudit = async (format: 'csv' | 'jsonl', filters: AuditFilters): Promise<Blob> =>
  (
    await api.get<Blob>(`${A}/audit/export`, {
      params: clean({ ...filters, format }),
      responseType: 'blob',
      timeout: 120000,
    })
  ).data;

export const listAudiences = () => get<Page<Audience>>(`${A}/audiences`);
export const refreshAudience = (target: { platform: string; container: string } | { all: true }) =>
  send<Page<AudienceRefreshOutcome>>('post', `${A}/audiences/refresh`, target, {});
export const purgeSource = (platform: string, container: string, dryRun = false) =>
  send<PurgeCounts>('post', `${A}/purge/source`, { platform, container, dry_run: dryRun });

export const listFailedEvents = (cursor?: string) =>
  get<FailedEvents>(`${A}/events/failed`, { cursor });
export const retryEvents = (target: { event_ids: string[] } | { all: true }) =>
  send<Retried>('post', `${A}/events/retry`, target);

export const listDeadLetterStreams = () => get<Page<DeadLetterStream>>(`${A}/dlq`);
export const listDeadLetters = (stream: string, cursor?: string) =>
  get<Page<DeadLetter>>(`${A}/dlq/${enc(stream)}/entries`, { cursor });
export const replayDeadLetters = (stream: string, target: { entry_ids: string[] } | { all: true }) =>
  send<DeadLetterDone>('post', `${A}/dlq/${enc(stream)}/replay`, target);
export const dropDeadLetters = (stream: string, target: { entry_ids: string[] } | { all: true }) =>
  send<DeadLetterDone>('post', `${A}/dlq/${enc(stream)}/drop`, target);

export const listGraphRepositories = () =>
  get<{ items: GraphRepository[] }>('/api/v1/graph/repositories');
