import { AdminOnly } from "@/components/ConsoleShell";
import { PersonDetail } from "@/components/sections/People";

export default async function PersonPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <AdminOnly>
      <PersonDetail id={decodeURIComponent(id)} />
    </AdminOnly>
  );
}
