import { AdminOnly } from "@/components/ConsoleShell";
import RecordDetail from "@/components/sections/RecordDetail";

export default async function RecordPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <AdminOnly>
      <RecordDetail id={decodeURIComponent(id)} />
    </AdminOnly>
  );
}
