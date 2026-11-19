"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { MemberView, Role } from "@/lib/generated-api";
import {
  membersApi,
  sameMemberReview,
  type ReviewedMember,
} from "./members-api";
const roles: Role[] = ["viewer", "operator", "finance_manager", "owner"];
export function MemberReview({
  review,
  blocked,
  pending,
  onSave,
  onCancel,
}: {
  review: ReviewedMember;
  blocked: boolean;
  pending: boolean;
  onSave: () => void;
  onCancel: () => void;
}) {
  return (
    <section
      aria-label="Reviewed membership change"
      className="space-y-2 rounded border p-3"
    >
      <h4 className="font-semibold break-all">Review {review.member.email}</h4>
      <p>Saved version {review.member.version}</p>
      <p>
        Role: {review.member.role} → {review.change.role}
      </p>
      <p>
        Status: {review.member.active ? "Active" : "Inactive"} →{" "}
        {review.change.active ? "Active" : "Inactive"}
      </p>
      <p>
        Roles apply across this company workspace. Inactive members lose access;
        existing records and attribution remain.
      </p>
      <button disabled={blocked || pending} onClick={onSave}>
        Save reviewed membership
      </button>{" "}
      <button disabled={pending} onClick={onCancel}>
        Cancel review
      </button>
    </section>
  );
}
function MemberEditor({
  member,
  disabled,
  onReview,
}: {
  member: MemberView;
  disabled: boolean;
  onReview: (review: ReviewedMember) => void;
}) {
  const [role, setRole] = useState<Role>(member.role),
    [active, setActive] = useState(member.active);
  return (
    <form
      className="space-y-2 rounded border p-3 min-w-0"
      aria-label={`Member ${member.email}`}
      onSubmit={(event) => {
        event.preventDefault();
        onReview({ member: { ...member }, change: { role, active } });
      }}
    >
      <h4 className="font-semibold break-all">{member.email}</h4>
      <p>
        Saved: {member.role} · {member.active ? "Active" : "Inactive"} · version{" "}
        {member.version}
      </p>
      <label className="block">
        Member role{" "}
        <select
          className="max-w-full border p-2"
          value={role}
          disabled={disabled}
          onChange={(event) => {
            const next = roles.find((value) => value === event.target.value);
            if (next) setRole(next);
          }}
        >
          {roles.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <label className="block">
        <input
          type="checkbox"
          checked={active}
          disabled={disabled}
          onChange={(event) => setActive(event.target.checked)}
        />{" "}
        Active membership
      </label>
      <button
        disabled={
          disabled || (role === member.role && active === member.active)
        }
      >
        Review membership change
      </button>
    </form>
  );
}
export function MembersPanel({
  workspace,
  csrfToken,
  disabled = false,
}: {
  workspace: string;
  csrfToken: string;
  disabled?: boolean;
}) {
  const cache = useQueryClient();
  const members = useQuery({
    queryKey: ["members", workspace],
    queryFn: ({ signal }) => membersApi.list(workspace, signal),
    retry: false,
  });
  const [review, setReview] = useState<ReviewedMember | null>(null),
    [pending, setPending] = useState(false),
    [error, setError] = useState(""),
    [saved, setSaved] = useState("");
  const current = members.data?.find(
    (member) => member.user_id === review?.member.user_id,
  );
  const stale =
    !!review && (!current || !sameMemberReview(review.member, current));
  const blocked =
    disabled || pending || members.isFetching || members.isError || stale;
  async function save() {
    if (!review || blocked) return;
    setPending(true);
    setError("");
    setSaved("");
    try {
      const result = await membersApi.change(
        workspace,
        review.member,
        review.change,
        csrfToken,
      );
      setReview(null);
      setSaved(
        `Saved ${result.member.email} as ${result.member.role}, ${result.member.active ? "active" : "inactive"}.`,
      );
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["members", workspace] }),
        cache.invalidateQueries({ queryKey: ["me"] }),
      ]);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Unable to save membership.",
      );
    } finally {
      setPending(false);
    }
  }
  return (
    <section aria-label="Workspace members" className="space-y-3">
      <h3 className="text-xl font-semibold">Workspace members</h3>
      <p>
        Owner manages settings and members. Finance Manager reviews collection
        decisions. Operator handles collection work. Viewer has read-only
        access. Separate action approvals and safety guards still apply.
      </p>
      <button
        disabled={pending || members.isFetching}
        onClick={() => members.refetch()}
      >
        Refresh members
      </button>
      {members.isPending && <p role="status">Loading members…</p>}
      {members.isError && <p role="alert">{members.error.message}</p>}
      {error && <p role="alert">{error}</p>}
      {saved && <p role="status">{saved}</p>}
      {stale && (
        <p role="alert">
          The reviewed membership changed. Cancel this review and review the
          current record.
        </p>
      )}
      {review && (
        <MemberReview
          review={review}
          blocked={blocked}
          pending={pending}
          onSave={save}
          onCancel={() => {
            setReview(null);
            setError("");
          }}
        />
      )}
      {!members.isError &&
        members.data?.map((member) => (
          <MemberEditor
            key={`${member.user_id}:${member.version}`}
            member={member}
            disabled={disabled || pending || !!review || members.isFetching}
            onReview={(next) => {
              setReview(next);
              setError("");
              setSaved("");
            }}
          />
        ))}
    </section>
  );
}
