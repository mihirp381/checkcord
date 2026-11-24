import React, { Suspense } from "react";
import GraphEmbed from "@/components/GraphEmbed";

interface PageProps {
  params: Promise<{
    repoId: string;
  }>;
}

export default async function EmbedPage({ params }: PageProps) {
  const { repoId } = await params;
  const decodedRepoId = decodeURIComponent(repoId);

  return (
    <Suspense
      fallback={
        <div className="flex h-screen w-screen flex-col items-center justify-center bg-slate-950 text-slate-200">
          <div className="h-16 w-16 animate-spin rounded-full border-4 border-slate-800 border-t-indigo-500"></div>
          <p className="mt-4 text-sm tracking-wide text-slate-400">Loading graph…</p>
        </div>
      }
    >
      <GraphEmbed repoId={decodedRepoId} />
    </Suspense>
  );
}
