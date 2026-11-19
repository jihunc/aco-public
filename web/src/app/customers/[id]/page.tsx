import { ProductWorkspace } from "@/features/receivables/workspace";
export default async function Page({
  searchParams,
  params,
}: {
  searchParams: Promise<{ workspace?: string }>;
  params: Promise<{ id: string }>;
}) {
  const [{ workspace }, { id }] = await Promise.all([searchParams, params]);
  return <ProductWorkspace mode="customer" workspace={workspace} id={id} />;
}
