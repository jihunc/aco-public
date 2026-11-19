import { ProductWorkspace } from "@/features/receivables/workspace";
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ workspace?: string; since?: string; until?: string }>;
}) {
  const { workspace, since, until } = await searchParams;
  const end = new Date();
  const impactWindow =
    since !== undefined || until !== undefined
      ? { since: since ?? "", until: until ?? "" }
      : {
          since: new Date(end.getTime() - 30 * 86400000).toISOString(),
          until: end.toISOString(),
        };
  return (
    <ProductWorkspace
      mode="impact"
      workspace={workspace}
      impactWindow={impactWindow}
    />
  );
}
