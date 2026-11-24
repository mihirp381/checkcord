"use client";

import { useRouter } from "next/navigation";
import { listGraphRepositories } from "../../lib/api";
import { formatNumber } from "../../lib/format";
import { useApi } from "../../lib/useApi";
import GraphEmbed from "../GraphEmbed";
import { EmptyState, ErrorState, Field, Loading, Mono, PageHeader, inputClass } from "../ui";

export default function Graph({ repo }: { repo?: string }) {
  const router = useRouter();
  const repos = useApi(listGraphRepositories, []);
  const items = repos.data?.items ?? [];
  const chosen = repo ?? (items.length === 1 ? items[0].repo_identifier : undefined);

  return (
    <>
      <PageHeader
        title="Graph"
        description="The code of a connected repository with the records that cover it. Select a node to see its records."
        actions={
          items.length > 0 && (
            <Field label="Repository" className="w-72 max-w-full">
              <select
                className={inputClass}
                value={chosen ?? ""}
                onChange={(e) => router.push(e.target.value ? `/console/graph?repo=${encodeURIComponent(e.target.value)}` : "/console/graph")}
              >
                {!chosen && <option value="">Choose a repository</option>}
                {repo && !items.some((r) => r.repo_identifier === repo) && <option value={repo}>{repo}</option>}
                {items.map((r) => (
                  <option key={r.repo_identifier} value={r.repo_identifier}>
                    {r.repo_identifier} ({formatNumber(r.files)} files)
                  </option>
                ))}
              </select>
            </Field>
          )
        }
      />
      {repos.loading && !repos.data ? (
        <Loading />
      ) : repos.error ? (
        <ErrorState message={repos.error} onRetry={repos.reload} />
      ) : !chosen ? (
        items.length === 0 ? (
          <EmptyState title="No repository has a code graph yet">
            Connect one on the server with <Mono>checkcord connect github --repo OWNER/NAME</Mono>. The graph appears once its first backfill is done.
          </EmptyState>
        ) : (
          <EmptyState title="Choose a repository to open its graph" />
        )
      ) : (
        <GraphEmbed key={chosen} repoId={chosen} session />
      )}
    </>
  );
}
