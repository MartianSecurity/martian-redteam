# Security and data

## Trust boundary

```text
Customer process             Approved Backend origin          Agents
local target callback <--- SDK -- Bearer / typed DTOs --------> execution owner
         |                    |                                  |
         +-- target provider  +-- eval outputs / CI records       +-- models
```

Modern SDK clients call only Backend with one scoped platform token. The target
callback remains local and owns its provider authentication. Simulation sends
catalog IDs, purpose, method/mode and an optional prompt. Judging sends attack,
reply, tool identifiers and optional custom criteria. These texts can contain
sensitive target data; they cross the customer boundary and enter the service's
controlled operation/recovery lifecycle. SDK digests do not remove data from
actual model requests. Backend needs raw dataset outputs for canonical scoring.

Requests cannot choose model/service/target endpoints. YAML cannot override the
platform origin. HTTPS is required except loopback; redirects and environment
proxies are off unless proxies are explicitly opted in. Constructor connections
must supply a complete URL/token pair. No `.env` is loaded and unused legacy
variables are ignored; legacy auth is never selected after a failed public call.

Reports default to content digests. Content opt-in does not disable credential
redaction, including constructor secrets after client close and report copies.
Explicit input/output redaction flags take precedence. Raw in-memory assessment
fields/model dumps and private local MLflow traces are not safe public reports.

## Local suite runs

`martian evals run evals.yaml` does not use the platform connection above and sends nothing to
Martian. Its outbound calls are the ones the suite file names:

- **A URL target** (`target.url`) receives one JSON `POST` per case, carrying that case's input.
  The suite file chooses this address, so review a suite file from someone else before running
  it with your credentials loaded. `http` is accepted as well as `https`; use `https` for any
  endpoint that is not on your own machine or network.
- **Header values** can reference `${NAME}`, read from the environment when the suite loads.
  Only the names appear in the suite file. The values are sent to the target and are not
  written to `results.jsonl`, `summary.json`, `junit.xml`, the MLflow trace or the local Evals
  UI's data folder. They are not
  expanded in the request body, and text inside a case is never expanded. Loading a suite file
  into the [shared eval model](eval-model.md#the-sdk-suite-file), as a store keeps it, keeps the
  URL and header values as written, with `${NAME}` unexpanded, so the environment's values are
  never stored. A literal value is stored as it is: keep credentials in the environment.
- **An LLM judge** receives the case's input, the reply and your criteria at the provider you
  configure.
- **MLflow** receives inputs, replies and judge reasons: a local store by default, or the
  server named in `mlflow.tracking_uri`.

Every run is also recorded in the local Evals UI's data folder on this machine (`--data-dir`,
`MARTIAN_EVALS_HOME` or `~/.martian/evals`), which `martian ui` reads: each case's inputs, reply,
verdicts and reasons as `results.jsonl` holds them, its trace with the target's spans, and the
target as the suite file writes it. Nothing there leaves the machine; treat the folder as you
treat `results.jsonl`.

Redirects are not followed and proxy settings from the environment are not used for the target
call. Target errors name the failure, never the address, headers or response body.

## Runtime reporting data boundary

Optional `RuntimeReporter` uses a different destination and credential from the probe APIs above: outbound HTTPS to the backend's `/v1/discovery/telemetry-bindings/{id}/resource-events` (v3), authenticated with a file-mounted, binding-scoped `mt_` token. It sends generated event/trace/optional parent UUIDs, model/tool kind, configured provider or connection alias, configured model/tool name, completion time, duration and outcome. It does not export the assessment TraceRecorder, prompts, arguments, results, customer/session identifiers, raw URLs or provider credentials.

Strict allowlisted fields and identifier syntax limit the payload but cannot detect sensitive data deliberately placed in an identifier. The application owns safe naming and separates production and assessment bindings. The backend derives tenant/deployment/environment/version from the credential; these are declared labels, not hardware or binary attestation. Reports remain application assertions. Local buffers are volatile, bounded and single-attempt; failures and drops are counted locally, not reported as a complete backend capture. Backend time-window queries are not a retention policy; automatic expiry is not implemented. See [lifecycle and delivery limits](customer-integration.md#optional-runtime-reporting).

## API keys

An owner/admin issues one organisation key with explicitly selected evals/CI
and/or `redteam:run` capabilities and applicable resource restrictions. The
plaintext is shown once; save it in a secret manager. Lists and later reads
contain metadata only. The key cannot manage the console or mint other keys.
Existing evals tokens retain their original capabilities; use a newly authorized
key to add redteam. Server-side revocation/lifecycle checks apply on each call.
Model entitlement and per-key operational limits remain server-owned.

Legacy direct `martian_redteam.Client` / `martian-redteam` is an explicit operator
compatibility path only. It requires a complete explicit `Settings` pair or
`MARTIAN_REDTEAM_API_URL` / `MARTIAN_REDTEAM_API_KEY`, never generic platform
variables or `.env`. Its key is bound to that selected origin and YAML cannot
change it. Never reuse a platform token as an Agents key. The legacy development
HTTP escape hatch does not apply to modern `MartianConfig`.

The single-key contract applies to evals/redteam APIs. Target-provider, MCP,
runtime reporting and cooperative call-gate credentials have separate scopes.
No hosted/customer readiness is established merely by installing the wheel;
qualify the coordinated services and auth/resource isolation before rollout.

## Retention and AUP

Operation payloads/results and native execution data follow the **service** retention and recovery policy. Backend/Agents must qualify database/checkpoint/Redis cleanup together; SDK local timeouts do not prove server payload deletion. Toxicity and illegal-activity categories will produce disallowed content by design. Requirements on the service side:

- Acceptable-use statement for stored adversarial payloads
- Bedrock-refusal path treated as an **error**, not a silent pass
- Retention/deletion story before public tenants

The SDK accepts only typed operation results. It does not filter attack text or use a provider refusal as a passing judgment.

## Supply chain and CI hygiene

- Pin GitHub Actions by SHA in this repo and in `examples/github-actions.yml`
- `uv.lock` is required in CI (`uv sync --locked`)
- `persist-credentials: false` on checkout
- Contents-read permission on the example workflow
- No `continue-on-error` on the assessment step

## Licence, attribution and vulnerability reports

Every file in this repository is Apache-2.0 (`LICENSE`); no file carries another licence.
Paid Martian features are not in this repository. Report a vulnerability as
[SECURITY.md](../SECURITY.md) describes, never in a public issue.

`NOTICE` records DeepTeam as the source of taxonomy identifiers and labels. Simulation and evaluation prompts in martian-agents are authored by Martian Security and are not copies of DeepTeam template corpora. Do not paste DeepTeam metric class files into this tree.

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
