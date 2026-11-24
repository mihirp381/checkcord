"use client";

import { formatNumber } from "../lib/format";
import type { PurgeCounts } from "../lib/types";
import { useApi } from "../lib/useApi";
import { ErrorState, Loading } from "./ui";

const LINES: [keyof PurgeCounts, string][] = [
  ["events", "events"],
  ["records_deleted", "records deleted"],
  ["records_updated", "records that lose a source but stay"],
  ["meeting_segments", "meeting segments"],
  ["code_symbols", "code symbols"],
  ["audience_containers", "stored audiences"],
  ["chat_buffers_and_trees", "chat buffers"],
];

export function PurgeCountsList({ counts }: { counts: Partial<PurgeCounts> }) {
  const shown = LINES.filter(([k]) => typeof counts[k] === "number" && (counts[k] as number) > 0);
  if (shown.length === 0) return <p className="text-muted">Nothing stored would be deleted.</p>;
  return (
    <ul className="list-disc pl-5">
      {shown.map(([k, label]) => (
        <li key={k}>
          {formatNumber(counts[k] as number)} {label}
        </li>
      ))}
    </ul>
  );
}

export function PurgePreview({ load }: { load: () => Promise<Partial<PurgeCounts>> }) {
  const preview = useApi(load, []);
  if (preview.loading) return <Loading label="Counting what would be deleted" />;
  if (preview.error) return <ErrorState message={`Could not count what would be deleted. ${preview.error}`} onRetry={preview.reload} />;
  return (
    <div>
      <p className="font-medium">This deletes:</p>
      <PurgeCountsList counts={preview.data ?? {}} />
    </div>
  );
}
