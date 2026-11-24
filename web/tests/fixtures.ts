export const T = "2026-03-02T10:00:00Z";

export const connection = {
  connection: "github:acme/billing",
  provider: "github",
  identifier: "acme/billing",
  status: "beta",
  state: "review",
  created_at: T,
  last_event_at: T,
  events: 12,
  held_records: 2,
  scope: { include: [], exclude: [] },
  scope_skipped: 0,
};

export const held = (id: string, summary: string) => ({
  id,
  connection: "jira:acme.example.net",
  context_type: "constraint",
  severity: "advisory",
  summary,
  created_at: T,
  sources: [{ platform: "jira", container: "PAY", events: 1 }],
  event_count: 1,
});

export const record = {
  id: "11111111-1111-1111-1111-111111111111",
  context_type: "adr",
  severity: "advisory",
  summary: "Retries stay idempotent",
  state: "live",
  held_connection: null,
  retired_at: null,
  replaced_by: null,
  confidence_score: 0.9,
  created_at: T,
};

export const recordDetail = {
  id: record.id,
  context_type: "adr",
  severity: "advisory",
  summary: "Retries stay idempotent",
  scope: ["repo:acme/billing"],
  scope_rows: [{ scope_type: "repo", scope_value: "acme/billing" }],
  is_locked: false,
  audience: "3 people",
  sources: [{ platform: "slack", container: "C123", events: 2 }],
  confidence_score: 0.9,
  created_at: T,
  state: "live",
  retired_at: null,
  retired_by: null,
  retired_reason: null,
  replaced_by: null,
  supporting_events: { count: 2, first: T, last: T },
  suggested_replacements: [] as { id: string; replacement_id: string; basis: string }[],
  updates: [] as { happened_at: string | null; source: { platform: string | null; type: string | null }; text: string }[],
};

export const suggestion = {
  id: "55555555-5555-5555-5555-555555555555",
  basis: "stated",
  similarity: 0.82,
  created_at: T,
  record_id: record.id,
  record_summary: "Quotes run in a nightly batch",
  record_created_at: T,
  replacement_id: "66666666-6666-6666-6666-666666666666",
  replacement_summary: "Quotes are fetched live. This supersedes the nightly batch.",
  replacement_created_at: T,
};

export const person = {
  id: "22222222-2222-2222-2222-222222222222",
  email: "dev@example.com",
  display_name: "Dev",
  active: true,
  is_admin: false,
  created_at: T,
};

export const apiKey = {
  id: "33333333-3333-3333-3333-333333333333",
  name: "laptop agent",
  is_admin: false,
  active: true,
  created_at: T,
  user_id: person.id,
  user_email: person.email,
};

export const session = {
  id: "44444444-4444-4444-4444-444444444444",
  user_id: person.id,
  user_email: person.email,
  created_at: T,
  last_seen_at: T,
  expires_at: "2030-01-01T00:00:00Z",
  revoked_at: null,
  active: true,
  user_agent: "Firefox on Linux",
  source: "10.0.0.5",
};

export const auditEntry = {
  id: "55555555-5555-5555-5555-555555555555",
  created_at: T,
  tenant_id: "t",
  channel: "mcp",
  action: "graph.context",
  user_id: person.id,
  user_email: person.email,
  key_id: apiKey.id,
  key_name: apiKey.name,
  params: { symbol: "charge" },
  record_ids: [record.id],
  redacted_count: 1,
  result_count: 2,
};

export const audience = {
  platform: "slack",
  container: "C0PRIVATE",
  is_public: false,
  member_count: 4,
  unmatched_count: 1,
  refreshed_at: T,
  attempted_at: T,
  last_error: null,
};

export const failedEvent = {
  event_id: "66666666-6666-6666-6666-666666666666",
  source: "slack",
  ingested_at: T,
  attempts: 5,
  last_error: "embedding call timed out",
};

export const stream = { name: "checkcord:events.dlq", entries: 1, oldest_age_seconds: 7200 };

export const deadLetter = { entry_id: "1700000000000-0", age_seconds: 7200, event_id: failedEvent.event_id, error: "boom" };

export const health = { ok: true, checks: [{ name: "Database", status: "PASS", detail: "reachable" }] };

export const stats = {
  days: 7,
  since: "2026-02-23",
  events: [
    { source: "slack", day: "2026-03-01", messages: 14, events: 5 },
    { source: "slack", day: "2026-03-02", messages: 3, events: 0 },
  ],
  records: [
    { type: "adr", kind: "decision", live: 3, held: 1, retired: 0 },
    { type: "general", kind: "reference", live: 5, held: 0, retired: 0 },
  ],
  records_with_removed_instructions: 1,
  events_unfinished: { pending: 0, failed: 1 },
  dead_letters: { "checkcord:events.dlq": 1 },
  discussions: { waiting: 2, oldest_minutes: 4.5, failed: 1, buffered: 6, buffered_oldest_minutes: 1.5, no_outcome: 3 },
  agent_reads: [{ day: "2026-03-01", reads: 9, people: 2 }],
  people_reading: 2,
  pr_checks: { posted: 4, skipped: 2, flagged: 1, conflicts: 1, blocked: 0, errors: 1 },
  connections: [{ connection: connection.connection, state: "live", last_event_at: T }],
};

export const emptyStats = {
  ...stats,
  events: [],
  records: [],
  agent_reads: [],
  people_reading: 0,
  pr_checks: { posted: 0, skipped: 0, flagged: 0, conflicts: 0, blocked: 0, errors: 0 },
  connections: [],
};

export const usage = {
  days: 7,
  since: T,
  rows: [{ kind: "chat", purpose: "extract", model: "model-a", calls: 10, failures: 1, input_tokens: 1000, output_tokens: 200, avg_latency_ms: 850, estimated_cost: 0.42 }],
  totals: { calls: 10, failures: 1, input_tokens: 1000, output_tokens: 200, estimated_cost: 0.42 },
};

export const emptyUsage = { ...usage, rows: [], totals: { calls: 0, failures: 0, input_tokens: 0, output_tokens: 0, estimated_cost: null } };

export const purgeCounts = {
  dry_run: true,
  events: 3,
  records_deleted: 2,
  records_updated: 1,
  meeting_segments: 0,
  code_symbols: 0,
  audience_containers: 0,
  chat_buffers_and_trees: 0,
};
