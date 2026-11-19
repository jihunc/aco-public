import { ProductWorkspace } from "@/features/receivables/workspace";
export default async function Page({ searchParams }: { searchParams: Promise<{ workspace?: string }> }) {
  const { workspace } = await searchParams;
  return <ProductWorkspace mode="conversations" workspace={workspace} />;
}
