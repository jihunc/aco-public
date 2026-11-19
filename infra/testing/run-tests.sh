#!/bin/sh
set -eu
cd /aco
.venv/bin/ruff check src tests scripts/prepare-ci.py scripts/portable_manifest.py scripts/check_spec.py scripts/verify_live.py scripts/serve-health.py scripts/local_runtime.py scripts/agent_live_runtime.py scripts/run-worker.py scripts/test-portable scripts/migrate.py scripts/setup-local-identity.py scripts/seed-demo.py scripts/prepare-product.py scripts/product-compose.py alembic
.venv/bin/mypy src tests scripts/prepare-ci.py scripts/portable_manifest.py scripts/check_spec.py scripts/verify_live.py scripts/serve-health.py scripts/local_runtime.py scripts/agent_live_runtime.py scripts/run-worker.py scripts/test-portable scripts/migrate.py scripts/setup-local-identity.py scripts/seed-demo.py scripts/prepare-product.py scripts/product-compose.py alembic
.venv/bin/python scripts/check_spec.py --planning
node web/scripts/generate-api.mjs --check
node_modules/.bin/tsc --noEmit
node_modules/.bin/tsc --project web/tsconfig.json --noEmit
(cd web && node_modules/.bin/eslint .)
(cd web && node node_modules/next/dist/bin/next build --webpack)
.venv/bin/python scripts/migrate.py
# Prerequisites above (and outer source/network bootstrap) remain fail-fast.
# Only these fixed, independent verification gates collect failures.
set +e
node_modules/.bin/playwright test tests/e2e/network-policy.spec.ts tests/e2e/onboarding.spec.ts tests/e2e/demo-onboarding.spec.ts tests/e2e/business-export.spec.ts tests/e2e/receivables.spec.ts tests/e2e/usage.spec.ts tests/e2e/policy.spec.ts tests/e2e/review.spec.ts tests/e2e/reminder.spec.ts tests/e2e/approvals.spec.ts tests/e2e/payment-source.spec.ts tests/e2e/impact.spec.ts tests/e2e/cohorts.spec.ts tests/e2e/economics.spec.ts tests/e2e/observed-cohorts.spec.ts
browser_mock_status=$?
ACO_TEST_PROFILE=synthetic-reminders node_modules/.bin/playwright test tests/e2e/reminder-execution.spec.ts
browser_synthetic_reminders_status=$?
ACO_TEST_PROFILE=synthetic-agents node_modules/.bin/playwright test tests/e2e/agent-stream.spec.ts tests/e2e/agent-approval.spec.ts tests/e2e/demo-reset.spec.ts tests/e2e/provider-connections.spec.ts
browser_synthetic_agents_status=$?
ACO_TEST_PROFILE=synthetic-agent-email node_modules/.bin/playwright test tests/e2e/agent-email.spec.ts
browser_synthetic_agent_email_status=$?
ACO_TEST_PROFILE=synthetic-voice node_modules/.bin/playwright test tests/e2e/voice.spec.ts
browser_synthetic_voice_status=$?
ACO_TEST_PROFILE=voice-live-code node_modules/.bin/playwright test tests/e2e/voice-live-code.spec.ts
browser_voice_live_code_status=$?
ACO_TEST_PROFILE=synthetic-inbound node_modules/.bin/playwright test tests/e2e/inbound.spec.ts tests/e2e/promise-performance.spec.ts tests/e2e/promise-multi.spec.ts tests/e2e/promise-installments.spec.ts tests/e2e/cases.spec.ts tests/e2e/case-approval.spec.ts tests/e2e/contact-stop.spec.ts tests/e2e/three-intent.spec.ts
browser_synthetic_inbound_status=$?
backend_status=0
.venv/bin/python -m pytest tests/component tests/process tests/unit tests/contracts tests/integration tests/business_impact tests/evaluations -q --boundary-report=/tmp/aco-boundary-execution.json || backend_status=$?
report_status=0
.venv/bin/python scripts/check_spec.py --mode planned --boundary-report /tmp/aco-boundary-execution.json || report_status=$?
node --test tests/component/usage-render.test.mjs
node_component_status=$?
node --test web/tests/*.test.mjs tests/voice_live_web_fixture.test.mjs
node_web_status=$?
set -e

overall_status=0
for gate in \
    "browser_mock=$browser_mock_status" \
    "browser_synthetic-reminders=$browser_synthetic_reminders_status" \
    "browser_synthetic-agents=$browser_synthetic_agents_status" \
    "browser_synthetic-agent-email=$browser_synthetic_agent_email_status" \
    "browser_synthetic-voice=$browser_synthetic_voice_status" \
    "browser_voice-live-code=$browser_voice_live_code_status" \
    "browser_synthetic-inbound=$browser_synthetic_inbound_status" \
    "backend=$backend_status" \
    "boundary=$report_status" \
    "node_component=$node_component_status" \
    "node_web=$node_web_status"
do
    printf "ACO_GATE %s\n" "$gate"
    gate_status=${gate#*=}
    if [ "$overall_status" -eq 0 ] && [ "$gate_status" -ne 0 ]; then
        overall_status=$gate_status
    fi
done
exit "$overall_status"
