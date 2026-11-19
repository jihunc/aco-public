import type { CustomerContactFrequency, CustomerInteractions, NextSavedAction as SavedAction } from "@/lib/generated-api";
import { stamp } from "./shared";

export function CustomerContactActivity({ interactions, frequency }: {
  interactions: CustomerInteractions; frequency: CustomerContactFrequency;
}) {
  return <section aria-label="Customer contact activity">
    <h3>Contact history across invoices</h3>
    <p>{interactions.original_count} recorded originals · {interactions.incoming_count} incoming · {interactions.outgoing_count} outgoing</p>
    <p className="muted">Last {interactions.window_days} days from {stamp(interactions.window_start)} through {stamp(interactions.as_of_at)}. Each original is counted once across linked invoices.</p>
    <ul>{interactions.groups.map(group => <li key={[group.channel, group.direction, group.state, group.synthetic].join(":")}>
      {group.count} · {group.channel} · {group.direction} · {group.state.replaceAll("_", " ")} · {group.synthetic ? "Synthetic" : "Observed"}
    </li>)}</ul>
    <h4>Recorded contact exposures</h4>
    <p>{frequency.last_24_hours} in 24 hours · {frequency.last_7_days} in 7 days, as of {stamp(frequency.as_of_at)}.</p>
    <ul>{Object.entries(frequency.states).map(([state, count]) => <li key={state}>{state.replaceAll("_", " ")}: {count}</li>)}</ul>
    <p className="muted">Exposure includes reservations and uncertain outcomes; it is not proof of delivery. Released reservations are excluded. These are recorded ACO facts, not all external customer contact or permission to send.</p>
  </section>;
}

export function NextSavedAction({ action }: { action?: SavedAction }) {
  if (!action) return <p>Saved action data unavailable.</p>;
  if (action.availability === "none_scheduled") return <p>No saved pending action.</p>;
  return <div aria-label="Next saved action">
    <p>Next saved action: {action.kind?.replaceAll("_", " ")} · {action.status?.replaceAll("_", " ")} · {action.channel} · due {stamp(action.due_at)}</p>
    <p className="muted">Saved reference {action.id}. Execution eligibility is rechecked; this due time does not guarantee outreach or delivery.</p>
  </div>;
}
