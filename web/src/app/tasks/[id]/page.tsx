import { ProductWorkspace } from "@/features/receivables/workspace";
export default async function Page({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ workspace?: string }>;
}) {
  const [{ id }, { workspace }] = await Promise.all([params, searchParams]);
  return <ProductWorkspace mode="agent-task" workspace={workspace} id={id} />;
}
