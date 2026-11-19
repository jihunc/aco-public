# ACO — Autonomous Collections Operator

## 1. Product overview

ACO helps B2B accounts receivable teams investigate overdue invoices, prioritize follow-up and handle exceptions such as disputes and payment promises. It imports invoice, customer and payment facts from an accounting adapter, then uses those facts to calculate receivables metrics, schedule reminders and give investigators the evidence behind each proposed action.

Accounting remains the financial source of truth. ACO tracks collection activity and approvals in its own records while invoice balances and payment status come from the accounting source. The local assessment uses labeled synthetic accounting data and simulated external providers. [Product and engineering specification](ACO_SPEC.md) · [Engineering guide](AGENTS.md)

## 2. Prerequisites

- Git, Python 3.12 or 3.13, and a running Docker Engine or Docker Desktop with Docker Compose 2.24.4 or newer. The product overlay uses Compose's `!override` syntax.
- A POSIX terminal on macOS or Linux. On Windows, use WSL2 with Docker Desktop integration enabled for your distribution. Run Git and Python inside WSL2 and keep the checkout in its Linux filesystem. Native PowerShell execution is unverified.
- Internet access for the initial image and dependency downloads. The Docker Quick Start includes the application dependencies and requires no host Node, pnpm, uv, database or API keys.

## 3. Quick Start — no API keys required

Use HTTPS to clone the default `main` branch.

```sh
git clone https://github.com/jihunc/aco-public.git
cd aco-public
```

If you use a GitHub SSH key, clone over SSH.

```sh
git clone git@github.com:jihunc/aco-public.git
cd aco-public
```

Run the launcher from the repository root to create private configuration and start the stack.

```sh
python3 scripts/dev.py init --config config/development.env --mode offline
python3 scripts/dev.py up --config config/development.env
python3 scripts/dev.py credentials --config config/development.env
```

`init` creates a dedicated Compose project, an adapter selector at `config/development.env` and private infrastructure files under `.tooling/development/offline/`. `up` builds the images, starts the infrastructure, runs the database migrations, validates API and worker composition and waits for the application services. The first build can take several minutes.

Open [ACO on localhost](http://127.0.0.1:53420) and sign in as `owner@example.test` with the generated password printed by `credentials`. Keep that output private. Offline selects the simulated LLM, mock email and voice, and synthetic accounting. New workspaces start with outbound paused and policy automation off.

Use the same configuration for each command. Docker volumes retain the database, identity and object storage. The launcher's `down` command stops only the selected project and retains its containers and volumes. `up` resumes it and `restart` restarts the selected setup. Avoid volume removal or pruning when you want to keep the dataset and saved work.

```sh
python3 scripts/dev.py restart --config config/development.env
python3 scripts/dev.py down --config config/development.env
```

## 4. Guided product demo

### UAT-01 — Sign in and import accounting data

Choose **Sign in**, then enter the generated Owner credentials in Keycloak. On **Set up your workspace**, enter a **Workspace name**, review **Timezone** and choose **Create demo workspace**. In the selected workspace, choose **Import synthetic demo data**. Check for **24 imported invoices · 6 imported customers**, refresh and confirm the counts remain. Choose **Open collections workspace**. You can return through **Settings & setup**.

### UAT-02 — Inspect source-backed KPIs

In **Overview**, inspect **Outstanding AR**, **Overdue AR** and **Aging of outstanding AR**. Open **Receivables**, select an invoice and open its customer account. Compare the currency, outstanding amount, due date and source information with the Overview. The totals and aging are calculated from the stored synthetic accounting snapshots.

### UAT-03 — Create, edit, complete, reopen and delete a task

In **Review Queue**, choose **New task** and create two tasks with distinct titles. **Edit** the first, update **Notes**, set **Status** to **Done** and choose **Save task**. Open it under **Completed tasks**, edit it back to **Open** and save. For the second task, choose **Delete**, then **Confirm delete**. Refresh and check that the first task remains open with its edited notes and that the second is absent from the queue.

### UAT-04 — Reorder the queue

Create a second open task. Drag a task by its handle to reverse the order and wait for **Review order saved**. You can also focus the drag handle, press Space, use an arrow key and press Space to drop. Refresh **Review Queue** and check that the order persists. Move buttons are also available.

### UAT-05 — Run a tool-using investigation

In **AI investigations**, search for and select an imported overdue invoice such as `INV-101`. In **Investigation objective**, enter “Inspect selected invoice and source evidence; create an internal review task.” Choose **Start investigation** and open the saved run. Observe progress and inspect tool activity and evidence under **Observable activity**. Open the resulting task from the saved outcome and reload both records to verify persistence. The screen labels the default LLM **Simulated model**. The tools, background worker, authenticated event stream and state changes use the application code.

### UAT-06 — Schedule a synthetic reminder

Use an open imported invoice such as `INV-101` in the demo workspace. Its recipient must have no active hold or contact restriction. Allow enough time to complete the setup before the scheduled minute.

1. In **Email execution**, choose **Prepare synthetic inbox** to prepare the mock connection and check private storage. In **Collection policies → Recipient review**, find the invoice customer's exact imported destination. Enter its IANA timezone or explicitly confirm the displayed workspace timezone, check **Destination and email permission verified** and **Allow email channel**, enter review evidence and choose **Save recipient review**.
2. Choose **New policy** and name it. Keep one email rule and its template. Set **Days from due date** to the calendar-day difference between the invoice due date and the intended future date in the recipient timezone. Set **Local time** inside the configured weekday sending window. Choose **Save draft**, then **Preview saved policy**. Confirm the exact invoice, destination scope and projected time, and inspect any exclusions.
3. Check **I verified native accounting reminders are disabled** and **I reviewed this policy configuration**, then choose **Activate reviewed policy**. In **Email execution**, enter a synthetic sender such as `collections@aco.example.test`, the exact imported destination under **Permitted recipient emails**, a small **Maximum requests** cap and **Review evidence**. Check both review boxes and choose **Save synthetic activation**. Check for **Current channel: Synthetic demo**.
4. Choose **Request workspace resume**. At the saved due time the worker evaluates the current policy, accounting, recipient and contact guards. If the eligible action is still listed under **Due reminders**, use **Queue due reminder**. Any proposal that requires a separate human decision must be reviewed by an authorized second user in **Review Queue → Human approvals**. Check the invoice's **Recent reminder actions** and **Conversations** for the resulting synthetic communication and retained source, or inspect the visible reason if the action was blocked. Choose **Pause outbound** when finished.

The scheduled exercise uses a mock transport. An accepted synthetic action is evidence of application execution. Real delivery requires separate provider evidence.

### UAT-07 — Verify restart persistence

With the same `config/development.env`, run the `restart` command from Quick Start. Return to the same workspace and reopen the edited task, investigation and outcome, and communication and reminder history. Verify the records and imported data are still present and the reordered tasks remain in the same relative order.

### UAT-08 — Optional independent approval

The generated demo realm has `owner@example.test`, `finance@example.test`, `operator@example.test` and `viewer@example.test`. They share the generated password and gain workspace roles only through membership. Use a demo workspace with an eligible recipient within the contact limits. If UAT-06 reached that recipient's cap, create a fresh demo workspace. As its Owner, go to **Settings & setup → Invite a teammate**, enter `finance@example.test`, select `finance_manager` and choose **Create invitation**. In a separate browser profile or private window, sign in as Finance, enter the code under **Join a workspace** and choose **Accept invitation**. The application displays the code locally.

To prepare the approval request, repeat the recipient, sender and policy review from UAT-06, choose **Draft for human review** under **Autonomy**, and set the scheduled reminder far enough ahead that it won't consume the test contact allowance. Resume this synthetic workspace and choose an eligible invoice within the recipient's business window. In **AI investigations**, select the invoice, choose **Use synthetic email review objective**, and start the run.

Open the saved request in **Review Queue → Human approvals** as its Owner proposer. Verify **Approve proposal** is unavailable for your own request. In the Finance session, choose **Review proposal**, inspect the accounting and proposal evidence, enter a **Decision reason**, set **Approval valid for (hours)**, and choose **Approve proposal**. Reload to verify the retained independent decision. The worker rechecks eligibility before any synthetic execution. Pause outbound when finished.

## 5. Five assessment capabilities

| Capability | What to inspect | Walkthrough |
| --- | --- | --- |
| Persistent CRUD | Create, update, complete, reopen and delete collection tasks with durable state and audit history. | UAT-03, UAT-07 |
| At least two KPIs or charts | Outstanding AR, Overdue AR and aging from persisted accounting-derived amounts, due dates and currency. | UAT-02 |
| Persistent drag and drop | Saved workspace priority ordering through the Review Queue drag handles. | UAT-04, UAT-07 |
| Streaming and scheduled work | Authenticated server-sent investigation events, durable reminder jobs and stored execution outcomes. | UAT-05, UAT-06 |
| Multi-step tool-using AI | LangGraph and PydanticAI coordinate scoped reads, validated proposals and a saved business outcome. | UAT-05, UAT-08 |

## 6. Architecture

The same Docker application runs for every provider selection. The Next.js frontend serves the workspace and proxies authenticated API and SSE traffic. FastAPI owns server-side membership, permissions, versions, action guards and provider-neutral contracts. The API and background worker share application code. The worker uses Procrastinate and PostgreSQL for durable jobs and runs LangGraph and PydanticAI investigations. PostgreSQL stores business state and audit evidence. Alembic owns schema migrations. Keycloak supplies OIDC identity. SeaweedFS implements local private S3-compatible storage for original artifacts.

Provider ports handle capabilities, normalized payloads and errors, idempotency, source identity, provenance and usage. Vendor SDKs stay inside adapters. The same policy, approval, audit and persistence code handles simulated and real provider selections. The Compose product topology keeps PostgreSQL and storage internal and exposes the web, API and identity on loopback.

## 7. Offline and optional Live providers

| Boundary | Development setting | Supported selections |
| --- | --- | --- |
| Accounting | `ACCOUNTING_ADAPTER` | Synthetic demo (`demo`) |
| Model | `LLM_PROVIDER` | Fake (`fake`) or OpenAI (`openai`) |
| Email | `EMAIL_ADAPTER` | Mock (`mock`) or Resend (`resend`) |
| Voice | `VOICE_ADAPTER` | Mock (`mock`) or Vapi (`vapi`) |

The [Offline template](config/development.env.example) documents the Quick Start defaults. `python3 scripts/dev.py init --config config/development-live.env --mode live` prepares a separate isolated configuration following the [Live template](config/development-live.env.example). It starts with simulated selections, outbound disabled and the automation kill switch enabled. `python3 scripts/dev.py check --config config/development-live.env` checks configuration shape without provider calls. Provider selection, API credentials and a successful check confer no execution authority.

**Real-provider execution in Docker is blocked by the canonical campaign ledger safety gate.** Under mixed versions, an older host allocator can bypass the current cooperative directory lock. The runtime rejects real selections with `CONTAINER_CAMPAIGN_LOCKING_UNVERIFIED` when it reaches that gate. Keep the canonical allocation history and kill switches in place until the required mixed-client exclusion proof succeeds. A fresh clone, alternate ledger or renewed key does not reset authorized spend.

When the gate is resolved, real-provider evaluation still requires separate explicit authorization, a clean source-bound configuration, current persisted grants, conservative request/token/message/seconds/USD budgets, applicable private and application kill switches, and allowlisted synthetic recipients or consented test participants. Setup uses private provider files with owner-only access. Only the API and worker receive the provider configuration. See the maintained templates and provider activation contracts in the [specification](ACO_SPEC.md).

| Adapter | Additional readiness |
| --- | --- |
| OpenAI | A private API key, verified account access to the selected tool-capable model, current pricing, and a source-bound grant and allocation. [Environment template](config/openai.env.example) · [Grant template](config/openai-grant.json.example) |
| Resend | A private API key, verified sender and exact allowed recipient, reviewed retention and private storage. Inbound needs a separately authorized receiving mailbox, reachable HTTPS webhook, signing secret and approved original download hosts. [Selection template](config/resend-selection.json.example) · [Credentials template](config/resend-credentials.json.example) |
| Vapi | Private credentials, reviewed assistant and signed tool callback, reachable HTTPS callback routes, exact verified participant, current rates and separately reviewed capture/retention conditions. [Selection template](config/vapi-selection.json.example) · [Credentials template](config/vapi-credentials.json.example) |

## 8. Automated testing

### Fast CI

[Fast CI on GitHub Actions](https://github.com/jihunc/aco-public/actions/workflows/ci.yml) runs on pushes and pull requests with Python and web lanes. Install the locked dependencies first. CI uses Python 3.12, uv 0.13.8, Node 22.18.0 and pnpm 10.30.3.

```sh
uv sync --frozen
pnpm install --frozen-lockfile
scripts/fast-checks web
scripts/fast-checks python
```

The web lane checks generated API types, TypeScript, lint, the Next.js build and Node component tests. The Python lane runs Ruff, mypy, the tracked-file secret scan, unit, component, process and selected contract and database integration tests. The runners require kernel egress containment. On Linux, install `sudo`, `unshare`, `setpriv`, `ip`, `iptables` and `ip6tables`. The Python lane also needs PostgreSQL server binaries in `/usr/lib/postgresql/<version>/bin` and creates its own disposable PostgreSQL instance. On macOS, the scripts use `sandbox-exec`, and the Python lane requires a dedicated test database on port 55432 via `ACO_TEST_DATABASE_URL` or the private native test configuration. Neither lane should be run through its internal `--contained` entry point.

### Comprehensive local regression

Run from a fresh disposable clone on a **Linux** host or VM with Git, Python 3.12 or 3.13, Docker Compose and `sudo`. Use a clean shell without inherited `COMPOSE_*` selectors and a Docker environment without any existing native `aco` containers or retained volumes.

```sh
scripts/test-local-regression
```

The runner prepares private synthetic infrastructure and builds a test image labeled with the checkout's current commit. It then verifies the embedded input manifest against the checkout, starts isolated local services and runs the maintained backend and Playwright suites. Test code gets an internal network with kernel egress restrictions, without the Docker socket or paid provider credentials. The runner refuses conflicting resources and stale source correspondence, and removes its containers and networks on exit while retaining volumes. Use a new disposable environment for another clean rehearsal if retained native resources conflict.

### Browser testing

- **Manual browser UAT** is the [guided product demo](#4-guided-product-demo) against the Quick Start stack.
- **Automated Playwright suites** run through the Linux comprehensive runner. For focused macOS runs, `scripts/test-e2e test tests/e2e/approvals.spec.ts` uses a separately prepared native test stack and the private `ACO_TEST_DEMO_PASSWORD`. Use the existing native test ports for API/web (58000/53000), PostgreSQL (55432), identity (58080) and storage (59000). The related backend entry point is `scripts/test-offline tests/integration/test_ingress_runtime.py -q`. Both wrappers require macOS `sandbox-exec` and the relevant locked dependencies.
- **Product smoke on macOS** targets the actual Docker Quick Start stack. Install locked web dependencies and Google Chrome. Use the generated private infrastructure file and adapt only the two loopback ports in the supplied product sandbox policy. For the default ports, run the following from the repository root.

```sh
sed -e 's/53100/53420/g' -e 's/58180/58480/g' \
  infra/testing/product-smoke-macos.sb > .tooling/development/offline/browser.sb
sandbox-exec -f .tooling/development/offline/browser.sb \
  node scripts/smoke-product.mjs .tooling/development/offline/product.env
```

The smoke runner checks browser login, synthetic import, a source-backed KPI and aging chart, persisted tasks and drag ordering, proxied SSE, and a saved simulated investigation outcome. After a successful baseline you can append `--inbound` to check synthetic receipt and private evidence, or `--scheduled` to check a genuine future business-time reminder. The scheduled option needs an available weekday window. After the documented restart, append `--verify-restart` to check persistence against the saved baseline. Each run writes its result under the private infrastructure directory. This macOS command does not run natively on Linux or Windows.

## 9. Troubleshooting

| Symptom | What to check |
| --- | --- |
| Initialization refuses existing files | Reuse the existing selector if it is your intended environment, or initialize with `--config config/<name>.env` and `--output .tooling/development/<name>`. Generated files use owner-only permissions. Preserve existing state. |
| Port already in use | Initialize a separate setup with unused `--web-port`, `--api-port` and `--identity-port` values. The default ports are 53420, 58420 and 58480. Update the product smoke sandbox to match when applicable. |
| Build or startup stops | Check `docker info` and `docker compose version`, image download access, free disk and the failed service or migration in the launcher output. Use `docker ps -a` to identify the selected project before inspecting logs. Avoid sharing resolved Compose output or private environment files. |
| Login redirects or fails | Start from the exact web URL printed by `up`. Verify the selected identity port and that `identity.aco.localhost` resolves to loopback. Preserve the exact issuer and callback settings. |
| No eligible reminder appears | Check the invoice balance, pause state, active reviewed policy, local sending window, recipient consent/allowlist, contact caps, holds and current sender activation. Read the preview or saved action outcome for its exclusion reason. |
| Tests refuse to run or report stale source | Use the supported OS and containment tools for the lane. Install dependencies before containment. On Linux, use a fresh disposable test host when native resources conflict and rebuild the portable test image from the current checkout after source or dependency changes. |

## 10. Known limitations

- The dataset and default model, mail and voice are synthetic. This release provides no evidence of real email delivery, real inbound replies, live audio, PSTN calls or production accounting connectivity.
- Real-provider execution in Docker remains blocked by the ledger safety gate. A passing fixture, smoke test or Fast CI run does not verify a paid integration. The Fast CI lanes cover a subset of the comprehensive regression.
- Railway templates are included for web, API and worker. A hosted HTTPS deployment requires its own PostgreSQL, private storage, OIDC, secrets, migration and smoke verification. The Quick Start is a local evaluation.
- Synthetic comparisons and usage records demonstrate product behavior. Commercial readiness, actual billing, collection uplift, hosted recovery and regulatory compliance each require separate evidence.
