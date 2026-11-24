import { AdminOnly } from "@/components/ConsoleShell";
import Review from "@/components/sections/Review";

export default async function ReviewPage({
  searchParams,
}: {
  searchParams: Promise<{ connection?: string | string[] }>;
}) {
  const { connection } = await searchParams;
  return (
    <AdminOnly>
      <Review connection={typeof connection === "string" && connection ? connection : undefined} />
    </AdminOnly>
  );
}
