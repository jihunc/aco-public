import { ProductWorkspace } from "@/features/receivables/workspace";
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ workspace?: string; invoice?: string }>;
}) {
  const { workspace, invoice } = await searchParams;
  return <ProductWorkspace mode="agents" workspace={workspace} id={invoice} />;
}
