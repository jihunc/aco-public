# ACO engineering guide

ACO is a standalone, multi-tenant collections application. [ACO_SPEC.md](ACO_SPEC.md) governs product behavior, interfaces, acceptance and release gates. Review its decision register, detailed contracts and preservation checklist before changing behavior. [README.md](README.md) covers setup and test entry points.

## Product and architecture

- Accounting owns all invoice and payment facts. ACO reads and reconciles verified source evidence. Keep balances, status, currency and provenance intact, and label demo financial data as synthetic.
- Keep reminders invoice-centric and enforce contact limits across the customer. Store each in-scope original communication once and link it to every relevant invoice and case. Retain authorized evidence, approvals, business actions and source events with their provenance.
- Keep vendor SDKs and native payloads behind replaceable adapters. Fake and real adapters share normalized contracts, business logic and downstream validation. Use maintained libraries for infrastructure and preserve one Docker-based runtime with environment-specific configuration.
- Customer-defined, versioned policy drives scheduling. Enforce tenant isolation, current membership and role permissions at every boundary. Verify source freshness, exact recipient and consent, independent approval, material changes, pause and idempotency server-side. Model output cannot grant authority. Preserve audit history and fail closed on uncertainty.
- Compute KPIs from authoritative persisted data and distinguish observed results from synthetic cohorts and estimates. Attribute shared provider usage once and expose cost provenance, including unknown charges.

## Verification and maintenance

- Default development and tests use fake external providers. Use the maintained contained test entry points, real local infrastructure where required, and assertions on persisted state and side effects.
- Live operations require separate opt-in, private credentials, current scoped grants, approved recipients or participants, consent where applicable, enforceable budgets and kill switches. Record actual per-provider outcomes, operation evidence, usage and cost. Offline success provides offline evidence.
- Update affected specification clauses, decisions, code, migrations, tests and the [traceability fixture](tests/fixtures/verification/traceability.md) together. Preserve stable requirements, tenant and financial invariants, retained evidence and backward compatibility. Review material behavior changes and resolve conflicting requirements explicitly.
- Keep the README current for operators and evaluators. Record exact test commands, results and source identity. Report blocked and unexecuted scope, and keep private configuration, credentials, operating data and generated evidence out of version control.
