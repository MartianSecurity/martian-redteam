# Security and data

## Trust boundary

```text
Customer network                         Martian VPC
─────────────────                        ────────────
model_callback  ◄── attack string
      │                                  POST /runs/wait (simulator)
      │                                  POST /runs/wait (evaluator)
      └── reply ──────────────────────►  attack + reply + tools_called
                                         persisted in Aegra/Postgres
```

The customer model is **never** invoked from Martian’s VPC. Martian never receives the customer’s API keys for OpenAI/Anthropic/etc. unless the callback (wrongly) forwards them.

Evaluator calls **do** send the target’s reply to Martian. If the target leaks a system prompt, credential, or PII, that text is in the wait body and may be stored with the run. Document this to anyone enabling CI on production apps.

## What the SDK sends

| Direction | Content |
| --- | --- |
| To simulator | Catalog ids, purpose string, attack method, mode, a generation instruction |
| To target | Simulator `attack`, possibly encoded |
| To evaluator | Attack string, target reply, `tools_called`, catalog ids, optional custom `criteria` |
| Headers | `X-API-Key`, `Content-Type`, `Accept` |
| Metadata | `sdk=martian-redteam`, `catalog_version` (no payloads) |

The SDK does not send: customer cloud credentials, `.env` files, source trees, or conversation history beyond the single-turn probe (Phase 1).

## Runtime reporting data boundary

Optional `RuntimeReporter` uses a different destination and credential from the probe APIs above: outbound HTTPS to the backend's `/v1/discovery/telemetry-bindings/{id}/resource-events` (v3), authenticated with a file-mounted, binding-scoped `mt_` token. It sends generated event/trace/optional parent UUIDs, model/tool kind, configured provider or connection alias, configured model/tool name, completion time, duration and outcome. It does not export the assessment TraceRecorder, prompts, arguments, results, customer/session identifiers, raw URLs or provider credentials.

Strict allowlisted fields and identifier syntax limit the payload but cannot detect sensitive data deliberately placed in an identifier. The application owns safe naming and separates production and assessment bindings. The backend derives tenant/deployment/environment/version from the credential; these are declared labels, not hardware or binary attestation. Reports remain application assertions. Local buffers are volatile, bounded and single-attempt; failures and drops are counted locally, not reported as a complete backend capture. Backend time-window queries are separate from the opt-in [30-day report-retention schedule](../../backend/docs/discovery.md#application-report-retention); deploying the scheduler is an operator responsibility. See [lifecycle and delivery limits](customer-integration.md#optional-runtime-reporting).

## API keys

Today martian-agents `auth.py` has **one shared `MARTIAN_API_KEY`** and **one Aegra identity**. Every holder of that key shares threads and run history.

Hard gates before a **public** SDK:

1. **Per-key identity** — distinct keys map to distinct identities; org-scoped thread/run access.
2. **Quotas** — application-level concurrency and daily caps **per key**. WAF is IP-based and will collide across GitHub-hosted runners.
3. **Pinned graph I/O** — `simulator` / `evaluator` `context_schema` and structured output. Extra `configurable` keys currently fail coercion on the generic assistant.

Until (1) and (2) exist, this repo is **internal/staging-only**. Dogfood with the shared key is allowed. `pip install` to third parties is not.

Do not log `Settings.api_key`. It is a `SecretStr`. httpx headers are set at client construction; tests assert the header on the mock transport without printing it.

## Retention and AUP

Attack strings and replies persist in Aegra/Postgres according to the **service** retention policy (not defined in this repo). Toxicity and illegal-activity categories will produce disallowed content by design. Requirements on the service side:

- Acceptable-use statement for stored adversarial payloads
- Bedrock-refusal path treated as an **error**, not a silent pass
- Retention/deletion story before public tenants

This SDK records whatever the graphs return. It does not filter attack text.

## Supply chain and CI hygiene

- Pin GitHub Actions by SHA in this repo and in `examples/github-actions.yml`
- `uv.lock` is required in CI (`uv sync --locked`)
- `persist-credentials: false` on checkout
- Contents-read permission on the example workflow
- No `continue-on-error` on the assessment step

## Attribution

Apache-2.0. `NOTICE` records DeepTeam as the source of taxonomy identifiers and labels. Simulation and evaluation prompts in martian-agents are authored by Martian Security and are not copies of DeepTeam template corpora. Do not paste DeepTeam metric class files into this tree.

## Resource runtime metadata

The optional runtime reporter can export explicitly configured response-ID mappings under the v3 contract. A successful MCP structured response contributes at most ten opaque resource references with configured service/type/action, completion time and reported outcome. It does not export native IDs, arguments or response bodies. References are keyed, binding-scoped HMAC values; they are pseudonymous correlation data, not anonymous data or authorization proof. Missing/error/unmapped calls have unknown resource coverage. Credentials and references change with a new deployment binding. See [configuration and boundaries](customer-integration.md#explicit-resource-response-mappings).

The separate explicit `native_kubernetes=True` mode exports v4 to `/native-events`.
It includes native cluster alias, service-account namespace/name and named request
coordinates. Those identifiers are **not opaque** and may reveal infrastructure
metadata; customer approval of names and binding scope is required. It exports no
credential/header/body/response and does not inspect an existing Kubernetes client.
The application declares the actual identity and request, including on errors.
See [opt-in usage](customer-integration.md#explicit-kubernetes-request-reporting).

## Opt-in call control boundary

The separate `mg_` gate credential exposes only configured target identifiers and
generated process/request UUIDs. It cannot authorize assessments, telemetry, console
reads or control administration. A customer can attach an exact target assignment
to a reporter; selected calls then fail closed without a live decision. This changes
application availability intentionally and remains cooperative instrumentation,
without complete-replica attestation or resistance to direct SDK bypass. Telemetry
revocation and gate revocation have different effects. See the [full lifecycle and
limits](customer-integration.md#sdk-call-gate).

## Explicit exact-action review export

The separate [exact-action approval](customer-integration.md#exact-action-approval)
path is disabled by default and requires a local allowlist of resolved tool targets.
It exports complete bounded arguments, configured destination/Host, transport tool
and metadata digest to the backend for an authenticated administrator's review.
Arguments may contain personal or business data; selecting an operation is explicit
consent to this processing and the task retention period. Do not select operations
whose arguments contain credentials or disallowed content. Connection authorization
headers are not exported. The reporter and ordinary task-allowance path remain
metadata-only.

Backend encrypts this context using its existing configured encryption provider,
with action-specific tenant/record authentication. Only the owner/admin detail endpoint
returns plaintext, with `no-store` and a view audit. The inventory, decision API and
Monitoring do not return it. Encrypted records cascade with tasks 30 days after their
expiry; view/review audit follows the 90-day administration retention. See the
[server contract](../../backend/docs/discovery.md#exact-action-approval), including
KMS context permissions and the separation between request approval and business-state
constraints. A local gateway's upstream HTTP mode is intended only for an explicitly
accepted local network boundary; use HTTPS for confidential transports.
