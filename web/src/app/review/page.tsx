import { ProductWorkspace } from "@/features/receivables/workspace";
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ workspace?: string; approval?: string }>;
}) {
  const { workspace, approval } = await searchParams;
  return <ProductWorkspace mode="review" workspace={workspace} id={approval} />;
}
