"use client";
import { useQuery } from "@tanstack/react-query";
import { readApi } from "@/lib/api";
import type { Communication } from "@/features/communications/api";
import { CommunicationLinkPanel } from "@/features/communications/link-review";
import { CommunicationRecord } from "@/features/communications/timeline";
import { ErrorState } from "@/features/receivables/shared";
export function SourceCommunication({
  workspace,
  id,
}: {
  workspace: string;
  id: string;
}) {
  const query = useQuery({
    queryKey: ["communication", workspace, id],
    queryFn: ({ signal }) =>
      readApi<Communication>(
        `/api/workspaces/${encodeURIComponent(workspace)}/communications/${encodeURIComponent(id)}`,
        signal,
      ),
    retry: false,
  });
  return query.isPending ? (
    <p role="status">Loading original communication…</p>
  ) : query.isError ? (
    <ErrorState
      label="Source communication unavailable"
      error={query.error}
      retry={() => query.refetch()}
    />
  ) : (
    <>
      <CommunicationRecord row={query.data} workspace={workspace} />
      <CommunicationLinkPanel source={query.data} workspace={workspace} />
    </>
  );
}
