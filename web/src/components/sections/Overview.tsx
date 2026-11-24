"use client";

import Link from "next/link";
import { useState } from "react";
import { getHealth, getStats, getUsage } from "../../lib/api";
import { formatCost, formatNumber } from "../../lib/format";
import { useApi } from "../../lib/useApi";
import type { CheckStatus, Stats } from "../../lib/types";
import {
  Badge,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  PageHeader,
  Panel,
  Stat,
  Table,
  Td,
  Th,
  inputClass,
} from "../ui";

const PERIODS = [
  [1, "Last 24 hours"],
  [7, "Last 7 days"],
  [30, "Last 30 days"],
  [90, "Last 90 days"],
] as const;

const STATUS_TONE: Record<CheckStatus, "ok" | "warn" | "danger"> = {
  PASS: "ok",
  WARN: "warn",
  FAIL: "danger",
};
const STATUS_LABEL: Record<CheckStatus, string> = { PASS: "OK", WARN: "Warning", FAIL: "Failing" };

const eventsBySource = (stats: Stats) => {
  const totals = new Map<string, { messages: number; events: number }>();
  for (const row of stats.events) {
    const key = row.source ?? "unknown";
    const t = totals.get(key) ?? { messages: 0, events: 0 };
    totals.set(key, { messages: t.messages + row.messages, events: t.events + row.events });
  }
  return [...totals.entries()].sort((a, b) => b[1].messages + b[1].events - (a[1].messages + a[1].events));
};

function Health() {
  const health = useApi(getHealth, []);
  return (
    <Panel
      title="Health"
      actions={health.data && <Badge tone={health.data.ok ? "ok" : "danger"}>{health.data.ok ? "All checks pass" : "Needs attention"}</Badge>}
    >
      {health.loading && !health.data ? (
        <Loading label="Running checks" />
      ) : health.error ? (
        <ErrorState message={health.error} onRetry={health.reload} />
      ) : health.data && health.data.checks.length === 0 ? (
        <EmptyState title="No checks reported" />
      ) : (
        <ul className="flex flex-col divide-y divide-line">
          {health.data?.checks.map((c) => (
            <li key={c.name} className="flex flex-wrap items-start gap-x-3 gap-y-1 py-2 text-sm">
              <Badge tone={STATUS_TONE[c.status]}>{STATUS_LABEL[c.status] ?? c.status}</Badge>
              <span className="font-medium">{c.name}</span>
              <span className="min-w-0 flex-1 text-muted [overflow-wrap:anywhere]">{c.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function Activity({ days }: { days: number }) {
  const stats = useApi(() => getStats(days), [days]);
  if (stats.loading && !stats.data) return <Panel title="Activity"><Loading /></Panel>;
  if (stats.error) return <Panel title="Activity"><ErrorState message={stats.error} onRetry={stats.reload} /></Panel>;
  const s = stats.data;
  if (!s) return null;
  const sources = eventsBySource(s);
  const reads = s.agent_reads.reduce((n, r) => n + r.reads, 0);
  const deadLetters = s.dead_letters ? Object.values(s.dead_letters).reduce((n, v) => n + v, 0) : null;
  return (
    <>
      <Panel title="Activity">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat
            label="Stored"
            value={formatNumber(sources.reduce((n, [, v]) => n + v.messages + v.events, 0))}
            hint={`${formatNumber(sources.reduce((n, [, v]) => n + v.messages, 0))} chat messages, ${formatNumber(sources.reduce((n, [, v]) => n + v.events, 0))} other events`}
          />
          <Stat label="Agent reads" value={formatNumber(reads)} hint={`by ${formatNumber(s.people_reading)} ${s.people_reading === 1 ? "person" : "people"}`} />
          <Stat label="PR checks posted" value={formatNumber(s.pr_checks.posted)} hint={`${formatNumber(s.pr_checks.conflicts)} with conflicts, ${formatNumber(s.pr_checks.blocked)} blocked, ${formatNumber(s.pr_checks.errors)} errors; ${formatNumber(s.pr_checks.skipped)} of Checkcord's own skipped`} />
          <Stat label="Connections" value={formatNumber(s.connections.length)} hint={`${s.connections.filter((c) => c.state === "review").length} in review`} />
        </div>
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <div>
            <h3 className="mb-2 text-xs font-semibold text-muted">Stored in this period, by source</h3>
            {sources.length === 0 ? (
              <EmptyState title="No events in this period" />
            ) : (
              <Table label="Stored in this period, by source">
                <thead><tr><Th>Source</Th><Th className="text-right">Chat messages kept</Th><Th className="text-right">Other events</Th></tr></thead>
                <tbody>
                  {sources.map(([source, n]) => (
                    <tr key={source}>
                      <Td>{source}</Td>
                      <Td className="text-right tabular-nums">{formatNumber(n.messages)}</Td>
                      <Td className="text-right tabular-nums">{formatNumber(n.events)}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </div>
          <div>
            <h3 className="mb-2 text-xs font-semibold text-muted">Records by type (all time)</h3>
            {s.records.length === 0 ? (
              <EmptyState title="No records yet" />
            ) : (
              <Table label="Records by type">
                <thead><tr><Th>Type</Th><Th>Kind</Th><Th className="text-right">Live</Th><Th className="text-right">Held for review</Th><Th className="text-right">Retired</Th></tr></thead>
                <tbody>
                  {s.records.map((r) => (
                    <tr key={r.type}>
                      <Td>{r.type}</Td>
                      <Td>{r.kind === "decision" ? "Decision" : "Reference"}</Td>
                      <Td className="text-right tabular-nums">{formatNumber(r.live)}</Td>
                      <Td className="text-right tabular-nums">{formatNumber(r.held)}</Td>
                      <Td className="text-right tabular-nums">{formatNumber(r.retired)}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
            <p className="mt-2 text-xs text-muted">Records with removed instructions: {formatNumber(s.records_with_removed_instructions)}</p>
          </div>
        </div>
      </Panel>
      <Panel title="Pipeline" actions={<Link className="text-sm underline" href="/console/pipeline">Open pipeline</Link>}>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Stat
            label="Chat messages to group"
            value={s.discussions ? formatNumber(s.discussions.buffered) : "Not known"}
            hint={s.discussions ? `oldest ${s.discussions.buffered_oldest_minutes} min` : "Redis is not reachable"}
          />
          <Stat label="Events waiting" value={formatNumber(s.events_unfinished.pending)} />
          <Stat label="Events failed" value={formatNumber(s.events_unfinished.failed)} />
          <Stat
            label="Dead letters"
            value={deadLetters === null ? "Not known" : formatNumber(deadLetters)}
            hint={deadLetters === null ? "Redis is not reachable" : "Entries for this organization"}
          />
          <Stat
            label="Discussions waiting"
            value={s.discussions ? formatNumber(s.discussions.waiting) : "Not known"}
            hint={s.discussions ? `oldest ${s.discussions.oldest_minutes} min` : "Redis is not reachable"}
          />
          <Stat
            label="Discussions failed"
            value={s.discussions ? formatNumber(s.discussions.failed) : "Not known"}
            hint={s.discussions ? `${formatNumber(s.discussions.no_outcome)} with no outcome` : undefined}
          />
        </div>
      </Panel>
    </>
  );
}

function ModelUsage({ days }: { days: number }) {
  const usage = useApi(() => getUsage(days), [days]);
  return (
    <Panel title="Model usage">
      {usage.loading && !usage.data ? (
        <Loading />
      ) : usage.error ? (
        <ErrorState message={usage.error} onRetry={usage.reload} />
      ) : usage.data && usage.data.rows.length === 0 ? (
        <EmptyState title="No model calls in this period" />
      ) : usage.data ? (
        <>
          <div className="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Calls" value={formatNumber(usage.data.totals.calls)} hint={`${formatNumber(usage.data.totals.failures)} failed`} />
            <Stat label="Input tokens" value={formatNumber(usage.data.totals.input_tokens)} />
            <Stat label="Output tokens" value={formatNumber(usage.data.totals.output_tokens)} />
            <Stat label="Estimated cost" value={formatCost(usage.data.totals.estimated_cost)} hint="From list prices" />
          </div>
          <Table label="Model usage">
            <thead>
              <tr><Th>Use</Th><Th>Model</Th><Th className="text-right">Calls</Th><Th className="text-right">Failed</Th><Th className="text-right">Tokens in / out</Th><Th className="text-right">Avg time</Th><Th className="text-right">Est. cost</Th></tr>
            </thead>
            <tbody>
              {usage.data.rows.map((r) => (
                <tr key={`${r.kind}/${r.purpose}/${r.model}`}>
                  <Td>{r.kind} · {r.purpose}</Td>
                  <Td>{r.model}</Td>
                  <Td className="text-right tabular-nums">{formatNumber(r.calls)}</Td>
                  <Td className="text-right tabular-nums">{formatNumber(r.failures)}</Td>
                  <Td className="text-right tabular-nums">{formatNumber(r.input_tokens)} / {formatNumber(r.output_tokens)}</Td>
                  <Td className="text-right tabular-nums">{formatNumber(Math.round(r.avg_latency_ms))} ms</Td>
                  <Td className="text-right tabular-nums">{formatCost(r.estimated_cost)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </>
      ) : null}
    </Panel>
  );
}

export default function Overview() {
  const [days, setDays] = useState(7);
  return (
    <>
      <PageHeader
        title="Overview"
        description="How this Checkcord server is doing."
        actions={
          <Field label="Period" className="w-44">
            <select className={inputClass} value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {PERIODS.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </Field>
        }
      />
      <div className="flex flex-col gap-4">
        <Health />
        <Activity days={days} />
        <ModelUsage days={days} />
      </div>
    </>
  );
}
