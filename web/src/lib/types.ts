export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
}

export interface SignInConfig {
  sign_in_enabled: boolean;
  login_url: string | null;
}

export interface Me {
  tenant_id: string;
  user_id: string | null;
  email: string | null;
  is_admin: boolean;
  auth: "session" | "key";
  key_id?: string | null;
  key_name?: string | null;
  session_id?: string | null;
  session_expires_at?: string | null;
  session_idle_expires_at?: string | null;
  csrf_token?: string | null;
  csrf_header?: string | null;
}

export type CheckStatus = "PASS" | "WARN" | "FAIL";

export interface Health {
  ok: boolean;
  checks: { name: string; status: CheckStatus; detail: string }[];
}

export interface Stats {
  days: number;
  since: string;
  events: { source: string | null; day: string; messages: number; events: number }[];
  records: { type: string; kind: RecordKind; live: number; held: number; retired: number }[];
  records_with_removed_instructions: number;
  events_unfinished: { pending: number; failed: number };
  dead_letters: Record<string, number> | null;
  discussions: {
    waiting: number;
    oldest_minutes: number;
    failed: number;
    buffered: number;
    buffered_oldest_minutes: number;
    no_outcome: number;
  } | null;
  agent_reads: { day: string; reads: number; people: number }[];
  people_reading: number;
  pr_checks: { posted: number; skipped: number; flagged: number; conflicts: number; blocked: number; errors: number };
  connections: { connection: string; state: ConnectionState; last_event_at: string | null }[];
}

export interface UsageRow {
  kind: string;
  purpose: string;
  model: string;
  calls: number;
  failures: number;
  input_tokens: number;
  output_tokens: number;
  avg_latency_ms: number;
  estimated_cost: number | null;
}

export interface Usage {
  days: number;
  since: string;
  rows: UsageRow[];
  totals: {
    calls: number;
    failures: number;
    input_tokens: number;
    output_tokens: number;
    estimated_cost: number | null;
  };
}

export type ConnectionState = "live" | "review";

export type ConnectorStatus = "beta" | "experimental";

export interface Connection {
  connection: string;
  provider: string;
  identifier: string;
  status: ConnectorStatus | null;
  state: ConnectionState;
  created_at: string | null;
  last_event_at: string | null;
  events: number;
  held_records: number;
  scope: ScopeLists | null;
  scope_skipped: number;
  audience_from?: "source" | "event" | "admin" | null;
  audience?: ConnectionAudience[];
}

export type AudienceMode = "nobody" | "everyone" | "groups" | "people";

export interface ConnectionAudience {
  connection: string;
  container: string | null;
  mode: AudienceMode;
  groups: string[];
  people: string[];
  members: number;
  updated_by: string | null;
  updated_at: string | null;
  recompute: { state: string | null; done: number; total: number; finished_at: string | null };
}

export interface ConnectionAudienceChange {
  connection: string;
  container?: string | null;
  mode: AudienceMode;
  groups?: string[];
  people?: string[];
}

export interface ScopeLists {
  include: string[];
  exclude: string[];
}

export interface ConnectionScope extends ScopeLists {
  connection: string;
  only_listed: boolean;
  skipped: number;
  days: number;
}

export type OAuthProvider = "slack" | "teams" | "notion" | "linear";

export interface InstallLink {
  provider: string;
  url: string;
  redirect_uri: string;
  expires_in_seconds: number;
}

export interface PurgeCounts {
  dry_run?: boolean;
  events: number;
  records_deleted: number;
  records_updated: number;
  meeting_segments: number;
  code_symbols: number;
  audience_containers?: number;
  chat_buffers_and_trees?: number;
}

export interface Disconnected {
  disconnected: boolean;
  purged: PurgeCounts | null;
}

export interface RecordSource {
  platform: string | null;
  container: string | null;
  events: number;
}

export interface HeldRecord {
  id: string;
  connection: string;
  context_type: string | null;
  severity: Severity;
  summary: string;
  created_at: string;
  sources: RecordSource[];
  event_count: number;
}

export type ReviewTarget =
  | { connection: string }
  | { record_id: string }
  | { record_ids: string[] };

export interface Released {
  events: number;
  records: number;
  record_ids: string[];
  not_held?: string[];
}

export interface Rejected extends Partial<PurgeCounts> {
  not_held?: string[];
}

export type Severity = "advisory" | "blocking";
export type RecordType = "adr" | "constraint" | "business_requirement" | "risk" | "general";
export type RecordKind = "decision" | "reference";
export type RecordState = "live" | "held" | "retired";

export interface RecordSummary {
  id: string;
  context_type: string | null;
  severity: Severity;
  summary: string;
  state: RecordState;
  held_connection: string | null;
  retired_at: string | null;
  replaced_by: string | null;
  confidence_score: number | null;
  created_at: string;
}

export interface ScopeRow {
  scope_type: string;
  scope_value: string;
}

export interface RecordDetail {
  id: string;
  context_type: string | null;
  severity: Severity;
  summary: string;
  scope: string[];
  scope_rows: ScopeRow[];
  is_locked: boolean;
  audience: string;
  sources: RecordSource[];
  confidence_score: number | null;
  created_at: string;
  state: "live" | "retired";
  retired_at: string | null;
  retired_by: string | null;
  retired_reason: string | null;
  replaced_by: string | null;
  supporting_events: { count: number; first: string | null; last: string | null };
  suggested_replacements: { id: string; replacement_id: string; basis: SuggestionBasis }[];
  updates: RecordUpdate[];
  may_be_replaced_by?: ReplacementNote | null;
}

export interface RecordUpdate {
  happened_at: string | null;
  source: { platform: string | null; type: string | null };
  text: string;
  audience?: string | null;
}

export interface ReplacementNote {
  record_id: string;
  created_at: string | null;
  basis: SuggestionBasis;
  summary: string;
}

export type SuggestionBasis = "stated" | "model";

export interface Suggestion {
  id: string;
  basis: SuggestionBasis;
  similarity: number | null;
  created_at: string;
  record_id: string;
  record_summary: string;
  record_created_at: string;
  replacement_id: string;
  replacement_summary: string;
  replacement_created_at: string;
}

export interface RecordFilters {
  repository?: string;
  type?: string;
  kind?: RecordKind;
  severity?: Severity;
  state?: RecordState;
  q?: string;
}

export interface ScopeUpdated {
  id: string;
  scope_repo_identifiers: string[];
  scope_rows: ScopeRow[];
  is_locked: boolean;
}

export interface Person {
  id: string;
  email: string;
  display_name: string | null;
  active: boolean;
  is_admin: boolean;
  created_at: string;
}

export interface Account {
  platform: string;
  account: string;
  display_name: string | null;
  source_email: string | null;
  user_id: string | null;
  person_email: string | null;
  link_source: "email" | "profile" | "manual" | null;
  linked_at: string | null;
  linked_by: string | null;
  unmatched_in: number;
}

export interface AccountLinked {
  platform: string;
  account: string;
  user_id: string;
  person_email: string;
  created: boolean;
}

export interface PersonDetail extends Person {
  keys: ApiKey[];
  active_sessions: number;
}

export interface PersonChanged extends Person {
  sessions_revoked: number;
}

export interface PersonDeleted extends PurgeCounts {
  user_id?: string;
  keys?: number;
}

export interface Session {
  id: string;
  user_id: string;
  user_email: string;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  revoked_at: string | null;
  active: boolean;
  user_agent: string | null;
  source: string | null;
}

export interface ApiKey {
  id: string;
  name: string;
  is_admin: boolean;
  active: boolean;
  created_at: string;
  user_id: string | null;
  user_email: string | null;
}

export interface KeyCreate {
  name: string;
  admin: boolean;
  user_email?: string | null;
  create_user?: boolean;
}

export interface KeyCreated {
  id: string;
  name: string;
  is_admin: boolean;
  user_email: string | null;
  key: string;
}

export interface Revoked {
  id: string;
  revoked: boolean;
}

export type AuditChannel = "mcp" | "api" | "cli" | "scim";

export interface AuditFilters {
  since?: string;
  until?: string;
  user?: string;
  key?: string;
  action?: string;
  channel?: AuditChannel;
  record_id?: string;
}

export interface AuditEntry {
  id: string;
  created_at: string;
  tenant_id: string;
  channel: string;
  action: string;
  user_id: string | null;
  user_email: string | null;
  key_id: string | null;
  key_name: string | null;
  params: Record<string, unknown> | null;
  record_ids: string[] | null;
  redacted_count: number | null;
  result_count: number | null;
}

export interface Audience {
  platform: string;
  container: string;
  is_public: boolean;
  member_count: number;
  unmatched_count?: number;
  refreshed_at: string | null;
  attempted_at: string | null;
  last_error: string | null;
}

export interface AudienceRefreshOutcome {
  platform: string;
  container: string;
  is_public: boolean;
  member_count: number;
  kept_last_known: boolean;
  events_updated: number;
  records_updated: number;
  error: string | null;
}

export interface FailedEvent {
  event_id: string;
  source: string;
  ingested_at: string;
  attempts: number;
  last_error: string | null;
}

export interface FailedEvents extends Page<FailedEvent> {
  retrying: number;
}

export interface Retried {
  retried: string[];
  not_failed: string[];
}

export interface DeadLetterStream {
  name: string;
  entries: number;
  oldest_age_seconds: number | null;
}

export interface DeadLetter {
  entry_id: string;
  age_seconds: number;
  event_id: string | null;
  error: string | null;
}

export interface DeadLetterDone {
  stream: string;
  done: string[];
}

export interface GraphRepository {
  repo_identifier: string;
  files: number;
}
