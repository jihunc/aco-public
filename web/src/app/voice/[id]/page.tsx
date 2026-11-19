import { ProductWorkspace } from "@/features/receivables/workspace";
export default async function Page({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ workspace?: string }>;
}) {
  const [{ id }, { workspace }] = await Promise.all([params, searchParams]);
  return (
    <ProductWorkspace mode="voice" workspace={workspace} id={id} />
  );
}
