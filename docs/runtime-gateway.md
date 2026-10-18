# Customer-managed MCP gateway

`martian-runtime-gateway` runs the SDK's deterministic controls in a separate
customer-owned process. A standard Streamable HTTP MCP client can use it without
importing the Martian SDK. It holds upstream credentials and exposes only configured
tool names under pre-issued tasks. It is separate from the sandbox assessment gateway.

## Authority and data

Each `/tasks/<route UUID>/mcp` endpoint has a distinct `mgw_` bearer credential and
an immutable operator configuration: upstream connections, selected tools, pinned
metadata, exact argument permissions and an existing shared-task assignment. The
gateway does not accept tasks, permissions, credentials, upstream URLs or additional
headers from an MCP caller. It has no task-issuance or configuration HTTP API.

The trusted issuer uses the existing [shared task API](customer-integration.md#shared-runtime-task-policies)
after an administrator approves the operation policy. Distribute the resulting
`TaskAuthorization` and matching `TaskAssignment` through the gateway's private
configuration. Every replica attaches to that same task; startup never creates a new
budget. Principal UUIDs remain trusted-issuer assertions, not independent IdP proof.
Use a separate route credential for each task and never give an agent the issuer,
backend, upstream or Kubernetes credentials.

Authentication runs before MCP body parsing. Each request rereads its route credential
file. Unknown tools are rejected. The gateway reopens the fixed upstream connections,
checks their metadata pins, validates and copies arguments, applies local exact task
rules, checks fresh Call Gate and shared task decisions, and executes through the
existing SDK transport. All selected raw tools must be enrolled in the Call Gate;
explicit dispatcher maps can additionally resolve individual operations. There are
no cached allows, quota refunds, automatic transport retries or local-only recovery.
Network proxy environment variables are ignored by the MCP client.

`tools/list` exposes the configured public names and checked startup schemas only
while the shared task remains active. Calls recheck current upstream metadata. An
unavailable backend, invalid/expired/revoked task, active suspension or invalid
decision stops entry. Argument and operation denials return their fixed SDK reason;
other schema/transport failures return `gateway_request_stopped`. No exception body
or upstream credential is returned to callers or written to gateway logs.

Calls may contain private arguments and results inside the customer's gateway.
Only the existing opt-in metadata and fixed decision records leave through its
separately enrolled reporter. Reporting is asynchronous and can drop events; the
durable shared-task audit and exact gate decisions remain separate. Admission is
not proof of a business effect. The gateway does not run a learned model or apply
the final-response output guard to tool results.

## Configuration and installation

Install the optional runtime dependencies, or build the separate image:

```sh
uv sync --frozen --extra gateway
uv run --extra gateway martian-runtime-gateway --print-schema
docker build -f Dockerfile.gateway -t martian-runtime-gateway:local .
```

Create one private JSON file matching `GatewayConfig` in
[gateway.py](../src/martian_redteam/gateway.py). The schema output has no credentials.
Do not put the populated file in Git, a ConfigMap, console screenshots or shared logs.

| Field | Required operator input |
| --- | --- |
| `backend_url` | Trusted backend origin, with HTTPS by default |
| `gate`, `gate_token_file` | Console-issued `GateAssignment` and separate `mg_` credential file |
| `channel`, `channel_token_file` | Same-binding `PolicyChannelAssignment` and separate `mp_` credential file |
| `reporter_token_file` | Same-binding `mt_` credential file |
| `action_review_targets` | Optional exact resolved tools whose complete action context may be exported for encrypted administrator review; requires policy schema 3 |
| `kubernetes_token_file` | Optional projected Pod credential with the exact gate audience |
| `tls_cert_file`, `tls_key_file` | Absolute certificate-chain/private-key paths for the HTTPS listener |
| `allowed_hosts` | Exact DNS/IPv4 host authorities, including ports; no wildcards |
| `max_inflight` | Concurrent HTTP admission limit per process, default 8, maximum 32; excess gets HTTP 503 |
| `tasks` | 1–32 distinct routes, each with the fields below |

Each task contains `route_id`, an absolute `credential_file`, `authorization`,
`assignment`, `connections`, `artifacts` and optionally `operations`. Connections
use the existing `MCPConnection` schema. Credentials in connection headers belong
only to the upstream service. A trusted upstream virtual-host header can be configured
there; caller headers are never copied upstream.

Every selected tool needs exactly one artifact entry: `system`, `tool`, `public_name`
and `sha256`. Obtain the digest from `BoundMCPTools.inspections[alias].artifact_sha256`
or the [static scanner](artifact-inspection.md), then review and pin it. Public names
must be unique in a route; an operator can preserve original names for a transparent
single-service route. The hash covers the full tool name, description and input
schema. It does not attest the upstream implementation or prevent the server from
changing behavior after metadata inspection. Changing a pin is an operator review
and rollout, never automatic acceptance of a changed tool.

Route credentials contain `mgw_` followed by 40–128 random URL-safe characters.
Generate them with a cryptographic random source. Credential files are limited to
256 bytes and configuration to 512 KiB. Absolute paths and matching task/deployment
assignments are required. Existing policy limits still bound operations, grants and
task lifetime; do not extend an expiry by editing the local assignment.

For [exact-action approval](customer-integration.md#exact-action-approval), explicitly configure
`action_review_targets` and issue a one-call task under policy schema 3. The gateway
binds review to arguments, destination, Host, transport tool and metadata digest. A
changed request cannot use the approval; the 15-minute deadline and durable one-use
consumption survive gateway restarts. Content export is disabled by default.

An approved backend policy may require [human review of a task-operation allowance](customer-integration.md#human-approval-of-task-operations).
The gateway returns the fixed `central_approval_required` error before tool entry.
An administrator reviews the task's allocated operation budget in the console;
customer-local argument rules still apply. Approval never resumes the original call
automatically. The caller must explicitly submit a new attempt, and every control
is checked again. Rejected, expired or revoked reviews remain denied.

```sh
export MARTIAN_GATEWAY_CONFIG=/secure/gateway/config.json
uv run --extra gateway martian-runtime-gateway --check-config
uv run --extra gateway martian-runtime-gateway
```

`--check-config` checks schema and local structural constraints without contacting
providers. Startup separately authenticates the gate, checks every shared task,
opens upstream MCP connections and verifies all metadata pins before serving.
The listener defaults to port 8400; `MARTIAN_GATEWAY_PORT` selects another port.
One worker runs per container; scale replicas with the same immutable task assignments.

TLS certificate/key paths must be supplied together. Plain HTTP requires explicit
`allow_http: true`, intended for an owned local environment. That option also permits
HTTP backend/upstream origins. There is no automatic TLS-to-HTTP transition. Browser
origins are not allowed; the transport is for authenticated server-side MCP clients.
MCP request bodies are capped at 256 KiB, total admitted HTTP requests at 105 seconds,
operation execution at 100 seconds, and
ordinary SDK argument/result limits remain active. Responses are JSON, with no
resumable event store, resources, prompts, sampling or delegated task-creation API.
The transport sends one tool request and accepts only an ordinary `CallToolResult`;
interactive/extended results are not driven through automatic additional calls.
An operation timeout can leave an uncertain upstream effect; it never restores a
spent allowance or retries the write.

## Kubernetes lifecycle

The workspace also supplies an independent [Helm chart](../../martian-helm-charts/charts/martian-runtime-gateway/README.md)
for image digest pinning, TLS probes, configuration-rollout revisions and the optional
exact Pod-identity audience. Its values contain references only; enrollment and the
private Secret remain operator-owned. Use either Helm or the standalone manifest
as the deployment owner, with an explicit reviewed handover when changing owners.

The [Deployment/Service manifest](../infra/kubernetes-runtime-gateway.yaml) uses a
non-root container, read-only root filesystem, bounded resources, a memory-backed
temporary directory and an existing `martian-runtime-gateway-config` Secret. Set
the namespace and publish an immutable image digest before applying it. It creates
only a ClusterIP Service and a service account with no API permissions. It does not
create any public ingress, TLS certificate or upstream service.

Mount `config.json` and the configured credential files in that Secret. For TLS,
mount the certificate/key and set the three probe `httpGet.scheme` values to HTTPS.
For required Pod identity, project a separate service-account token with audience
`martian-call-gate:<gate UUID>`, configure its path, and enable the backend's
[workload verifier](customer-integration.md#kubernetes-pod-identity). Ordinary
service-account token automount stays disabled.

`/healthz` reports process liveness. `/readyz` is served only after startup succeeds
and returns 503 when every configured task has expired. It does not continuously
probe upstreams or promise backend availability; each operation still requires fresh
decisions. Expired tasks need new trusted issuance and a reviewed configuration rollout.
Configuration, upstream credentials, pins and TLS files are loaded at startup;
roll the deployment after changing them. Protect any private configuration backup
with the same access policy as its upstream credentials.

To rotate or revoke **gateway access**, atomically replace/remove the corresponding
route credential file in the secret store. Subsequent requests reread it; existing
HTTP calls already admitted may complete. Kubernetes Secret projection adds its own
propagation delay, so verify rejection of the old token on each replica. Give the new
credential only to the intended client. This changes gateway access, not its upstream
provider credential or task budget. For task/principal restrictions, use the existing
reviewed console suspension, expiry and resume workflow. A restart preserves durable
aggregate operation budgets but resets the extra process-local per-grant counters.

Removing this deployment stops the gateway. Revoke its reporting, gate and policy
channel credentials through the backend; upstream credential revocation belongs to
the service owner. Deleting a reporting binding alone is not network containment.

## Coverage boundary

The enforcement boundary is this routed MCP connection. Credential separation can
prevent a caller with only a gateway token from authenticating directly upstream;
verify that property against the actual upstream service. A client that also holds
upstream credentials, another permitted route, or control of the gateway host can
bypass this boundary. NetworkPolicy/CNI enforcement, provider-wide identity revocation,
HTTP-tool interception, in-flight cancellation and semantic disclosure detection are
not supplied by this gateway. Use provider-side transactional checks for business
state and independent reads to verify effects.
