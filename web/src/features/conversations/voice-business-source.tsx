import Link from "next/link";
import type { VoiceEvidenceView } from "@/lib/generated-api";

export function VoiceBusinessSource({
  source,
  workspace,
}: {
  source: VoiceEvidenceView;
  workspace: string;
}) {
  return (
    <section
      className="source-provenance"
      aria-label="Signed call business facts"
      style={{ minWidth: 0, overflowWrap: "anywhere" }}
    >
      <p>
        <strong>Unverified operational business facts</strong> ·{" "}
        {source.synthetic ? "Synthetic" : "Observed"} · {source.evidence_mode}
      </p>
      <p>
        These normalized {source.fact_kind.replaceAll("_", " ")} facts came from
        a signed call invocation. They are not a transcript, recording or
        payment evidence.
      </p>
      <Link
        href={`/voice/${encodeURIComponent(source.session_id)}?workspace=${encodeURIComponent(workspace)}`}
      >
        Open source voice session
      </Link>
      <details>
        <summary>Exact signed business facts and references</summary>
        <pre
          style={{
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            minWidth: 0,
          }}
        >
          {source.facts_json}
        </pre>
        <p>
          Amounts use exact integer minor units with their currency and
          exponent.
        </p>
        <dl className="detail-grid">
          <div>
            <dt>Observed</dt>
            <dd>{source.observed_at}</dd>
          </div>
          <div>
            <dt>Provider call</dt>
            <dd>{source.provider_call_id}</dd>
          </div>
          <div>
            <dt>Native invocation</dt>
            <dd>{source.native_invocation_id}</dd>
          </div>
          <div>
            <dt>Evidence reference</dt>
            <dd>{source.id}</dd>
          </div>
          <div>
            <dt>Reviewed facts hash</dt>
            <dd>{source.facts_hash}</dd>
          </div>
        </dl>
      </details>
    </section>
  );
}
