import Graph from "@/components/sections/Graph";

export default async function GraphPage({
  searchParams,
}: {
  searchParams: Promise<{ repo?: string | string[] }>;
}) {
  const { repo } = await searchParams;
  return <Graph repo={typeof repo === "string" && repo ? repo : undefined} />;
}
