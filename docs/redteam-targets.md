# Red team targets

A target is what a red team run attacks. The engine sends it one message at a time and reads its reply. Every target implements one interface, `martian.redteam.targets.Target`:

```python
async def send(message: str, *, session: str | None = None) -> TargetReply
async def close() -> None
```

`TargetReply` holds the reply's `text`, the `tool_calls` the target made or asked for, `tool_calls_observed` (whether the target reports its tool calls at all, so that an empty list means none were made), the `session` the message was sent in, `latency_ms`, and the `evidence` a boundary the harness trusts captured about the calls: the [recording proxy](#recording-proxy)'s ledger or a [trace](#tool-calls-from-traces)'s tool spans, each with its source.

`target_from_run_file(block)` builds the target a [run file](redteam-run-file.md)'s `target` block names: a `FunctionTarget`, an `HttpTarget` or an `McpServerTarget`. `martian redteam run` and the local edition's run routes build the `url` and `mcp` targets with it; they run a `file` target in process with the version 0.2 callback adapter (`targets/callback.py`), which keeps each session's conversation and takes the [reply contract](#reply-contract)'s object as well as text or a `Turn`, and put the target behind the [recording proxy](#recording-proxy) when the run file lists `tools.proxy`. Building one reads `${NAME}` from the environment and checks the settings; it calls nothing. The first message connects.

## Function and HTTP targets

The `file` and `url` blocks are a suite file's target block, read by the suite file's own rules, so the same block works in an [eval suite](evals.md) and a red team run.

- **Function** (`file`, `function`). The function runs in a process of its own, as a suite file's function target does, and cannot read the run's private files on Linux. It is called with the keyword argument `message`. Its return value must be JSON: text, or the [reply contract](#reply-contract)'s object. Any other value is kept as its JSON text. A function that raises is a target error that names the exception's type only.
- **HTTP** (`url`, `headers`, `body`, `response_path`, `timeout_seconds`). One JSON `POST` per message: no retry, no redirect, no proxy from the environment. `${NAME}` in `url` and header values is read from the environment. The reply is the response's text, or the value at `response_path`; a value that is not text is kept as its JSON. `tool_calls_path` names where the answer lists its tool calls. A failed call names the setting to check and never repeats the address, a header or the response.

`HttpTarget(reference, address_policy=...)` pins every connection to an address the policy allows, as the judges' connections are pinned. An edition that calls endpoints other people name passes one. A local run calls the endpoint its user named and passes none.

## MCP server target

An agent exposed as an MCP server is attacked through one entry tool, used as the chat. The run file's `mcp` block names the server (a Streamable HTTP `url`, or a stdio `command`), the `tool` and the `message_argument` the attack goes in. It needs the `mcp` extra (`pip install "martian-sdk[mcp]"`).

- Each message is one call of the tool, with `arguments` beside it. `session_argument` carries the conversation's id and `reset_argument` is `true` on its first turn.
- The reply is the text of the result. `response_path` and `tool_calls_path` read the result's structured content instead, or its text as JSON.
- A result marked as an error, or a server that cannot be reached, is a target error that never repeats the address or what the server sent.
- One connection serves every message of the run. A stdio server starts with the first message and ends when the target closes; it gets only the environment variables MCP's stdio client passes on.

The tool's input schema is pinned for the run, as the hosted engine pins it: its SHA-256 over canonical JSON is `schema_sha256` when the block gives one, else the digest the first message sees. Every message checks it again. A tool that is gone or whose schema changed raises `TargetChanged`, and the engine stops the run rather than attack a different tool.

## Conversations

Each attack is one conversation, and one conversation is one `session`:

- `send(message)` with no session starts a new conversation. The reply's `session` names it.
- `send(message, session=reply.session)` continues it.
- `send(message, session="attempt-0001")` with a session the target has not seen starts a new conversation with that name. The engine can name each attempt's conversation this way.
- The conversation's id is the session's name when `bind_tools` can send it as an action id (letters, digits, `_ . : -`, up to 128). Otherwise, as for the engine's attempt names such as `prompt_leakage/instructions/prompt_injection#1`, it is `s-` and a stable digest of the name.
- A new session is the reset signal. It has a new id and an empty history, so a server that keeps state by the id starts fresh. `end(session)` forgets a conversation.

What the target sends:

| Target | Conversation id | History |
| --- | --- | --- |
| Function | The `session_id` parameter, when the function takes one | The `history` parameter, when the function takes one: the earlier turns as `user` and `assistant` messages, under `martian redteam run` and in a suite file |
| HTTP | `{{session_id}}` in `body`. `session.header` also sends it in a header | `{{history}}` is the earlier turns; `{{messages}}` is those and the new message. A body with only `{{input}}` sends the newest turn |
| MCP | `session_argument`, and `reset_argument` is `true` on the first turn | Not sent: the server keeps it |

A server that issues its own ids sets `session.response_path`. The first turn of a conversation sends an empty id, and each reply's id at that path is sent with the next turn.

## Reply contract

A target reports the tool calls it made or asked for, so a check can ask whether the agent called `issue_refund` with an amount above a limit, and whether the call ran:

- an HTTP target: the list at `tool_calls_path` in its JSON answer. An answer with nothing at the path made no calls;
- a function, under `martian redteam run` or in a suite file: an object `{"text": "...", "tool_calls": [...]}` with no other keys;
- a version 0.2 callback: a `Turn` with `tool_calls`, or its names-only `tools_called`.

Each tool call is an object, or a tool's name as text (which says nothing about arguments or whether it ran):

| Field | Meaning |
| --- | --- |
| `name` | The tool. OpenAI's `{"function": {"name", "arguments"}}` form is read too |
| `arguments` | An object, or JSON text of one. `args` and Anthropic's `input` are read too |
| `id` or `call_id` | The call's id |
| `executed` | `true` when the call ran, `false` when it was refused or blocked. Leave it out when the agent only asked for the call: a check that needs an executed call never counts one without it |
| `result` | What the tool returned, as text or any JSON |

A reply reports its calls, so `tool_calls_observed` is true, when the HTTP or MCP target has a `tool_calls_path`, when a function returns an object with `tool_calls` (an empty list included), and for a `Turn`; the proxy and traces set it too. A tool-call check on a reply that does not report its calls is undetermined, never held.

A call the reply contract cannot read is the target's error, never an empty list: an item without a name, arguments that are not an object, or `executed` that is not `true` or `false`.

```json
{
  "reply": "Your refund is on its way.",
  "tool_calls": [
    {"name": "lookup_order", "arguments": {"order": 7}, "executed": true},
    {"name": "issue_refund", "arguments": {"order": 7, "amount": 80}, "executed": true, "result": "refunded"}
  ]
}
```

A version 0.2 report keeps the calls on each turn. Their arguments and results are target content: the report hides them unless it includes target content, and the run's redaction applies either way.

## Recording proxy

With no environment the harness controls nothing the agent reads and sees nothing the agent does at its tools, and the agent's real tools would carry out an attacker's goal. The recording proxy is the open edition's tool boundary, the local equivalent of the cloud's run-scoped gateway. The agent reaches each of its own MCP tool servers (the run file's `tools.proxy`) through it.

```python
from martian.redteam.proxy import ToolProxy

async with ToolProxy(run_file.tools.proxy) as proxy:
    session = await proxy.issue_session(target_id="refund-agent")   # what the agent binds
    ...
    proxy.calls()           # the ledger: every call, in order
```

Each proxied server has a mode:

| Mode | What the proxy does | `executed` in the ledger |
| --- | --- | --- |
| `record` | Forwards the call | `true` |
| `inject` | Forwards the call and adds the `inject` text to the result the agent reads. An attack method's `tool_result` route instead adds its own attack, for one conversation only ([delivery routes](redteam-methods.md#delivery-routes)). The ledger records the delivery | `true` |
| `block` | Answers with `block_reply` and does not forward: the tool server never sees the call. A blocked tool is listed without its output schema, so the text is a valid answer | `false` |

`tools` limits `inject` and `block` to the tools it names. A call that fails on its way to the tool server is recorded with `executed` unknown, and the agent gets an error result that never repeats what the server sent.

Each ledger entry (`ProxyCall`) holds the server, the tool, its arguments, the action id, the mode, whether it ran, the injected text, the start of the result, and when. `call.tool_call()` is the call as an attempt keeps it; `call.evidence()` is its evidence: the call at the tool boundary, and a payload's delivery (level 0) in inject mode.

In a run, the session is handed to the agent as the cloud hands one over: posted to `tools.session_api` when the run file names one, and written to `tool-session.json` in the output folder for the length of the run ([run file](redteam-run-file.md#tools)).

The proxy records each call with the `X-Martian-Action-Id` header that `bind_tools(..., action_id=...)` sends. An agent that passes its conversation's id (the HTTP body's `{{session_id}}`, a function's `session_id`) as the action id lets `ProxiedTarget(target, proxy)` add that conversation's calls and their evidence to each reply. A call the target reported itself and the proxy saw, by its id or by its name and arguments, is kept once, as the proxy saw it.

The proxy is not an environment. It simulates no application and holds no data: the ledger, in memory, is all it keeps. It listens on 127.0.0.1 only, one port per server. It records at most 10,000 calls a run, and refuses a call past that rather than leave it out. It needs the `mcp` extra. The upstream must be a Streamable HTTP server.

## Local sessions

The proxy issues sessions in the shape the cloud issues them, `martian_redteam.AssessmentSession`, so an agent binds its tools with `bind_tools` (or `bind_sandbox_mcp` for a JSON payload) the same way against a local run and a cloud assessment. Its code does not change between editions.

```python
from martian.redteam.proxy import session_payload
from martian_redteam import bind_sandbox_mcp

session = await proxy.issue_session(target_id="refund-agent", campaign_id=run_id)
payload = session_payload(session)          # JSON with the header values, as the cloud sends it

# In the agent, unchanged from the cloud:
async with bind_sandbox_mcp(payload, action_id=conversation_id) as tools:
    ...
```

| Field | Local session | Cloud session |
| --- | --- | --- |
| `session_id` | `local-` and a random id | The campaign's id |
| `campaign_id`, `target_id` | What the run passes | The campaign and the registered agent |
| `expires_at` | One hour by default (`ttl`) | The run's time limit and five minutes |
| `mcp_connections` | One per proxied server: its loopback address, `Authorization: Bearer` with the session's token, and the tools its upstream lists now | One per sandbox system: the run-scoped gateway, its bearer token, the approved tools |

Every request to the proxy must carry the token of an open session. A session ends when `end_session(session_id)` is called, when it expires, or when the proxy stops at the end of the run; after that its requests are refused with `401` before they reach a tool, and nothing is recorded. The ledger records each call's session, and its action id from the binding's `X-Martian-Action-Id` header.

## Tool calls from traces

An agent that already traces itself records its tool calls as tool spans. A tool span is a call that ran, so it is evidence of an executed call with no change to the agent. The spans are read through the evals core's [trace mapping](evals-core.md#trace-conventions), which knows MLflow's span types, the OpenTelemetry GenAI conventions and OpenInference. Spans inside a tool span are the tool's own work and are not read.

| Route | How the spans are found |
| --- | --- |
| A function target | Its process records each call's MLflow spans and hands them back with the reply, as a suite file's function target does. A function that uses `@mlflow.trace(span_type="TOOL")`, or a library MLflow's autologging traces, needs nothing else |
| `TracedTarget(target, StoreTraces(store, principal))` | After each message, the traces in the local trace store of the reply's session that started since. The local edition's OpenTelemetry endpoint (`POST /v1/traces`) writes them; the agent puts the conversation's id in `gen_ai.conversation.id` (or OpenInference's `session.id`). `settle_seconds` waits for an exporter that sends spans in batches |
| `calls_in_trace(trace)`, `calls_in_mlflow(trace)` | A trace the caller already has |

The OpenTelemetry GenAI conventions make a tool call's arguments opt-in (`gen_ai.tool.call.arguments`). A span that names its tool and nothing else is name-only evidence: its evidence says `arguments_captured: false`. When the target also reported that call, the call is kept once, marked as executed, with the arguments the target reported. Every call from a trace has evidence of kind `tool_call` whose source is `trace:otel_genai`, `trace:openinference` or `trace:mlflow`, with its trace and span ids.

## Worked examples

`examples/redteam/` holds one agent per framework, each a red team target with its tools bound through the recording proxy. Each uses a stub model that falls for the attack, so it runs with no key and reaches no other machine:

| Example | Framework | How its tools are bound |
| --- | --- | --- |
| `openai_agents_agent.py` | OpenAI Agents SDK | `bind_tools`, each tool a `FunctionTool`; a stub `Model` |
| `langgraph_agent.py` | LangGraph | `bind_tools`, each tool a LangChain `StructuredTool` in a `ToolNode` graph; a stub model node |
| `pydantic_ai_agent.py` | Pydantic AI | `bind_tools`, each tool `Tool.from_schema`; Pydantic AI's `FunctionModel` |
| `claude_agent_sdk_agent.py` | Claude Agent SDK | The session's connections as the SDK's `mcp_servers`, with the action id header; the bundled CLI calls a stub of the Messages API on loopback |

Each one shows the three pieces: a target (`AgentTarget` in `shared.py`, wrapped in `ProxiedTarget`), structured tool calls in the [reply contract](#reply-contract), and the proxy, which blocks `issue_refund` so the attacker's refund never reaches the tool server. The reply says `lookup_order` ran and `issue_refund` did not.

```bash
uv sync --extra evals --extra mcp --group examples
uv run python -m examples.redteam.langgraph_agent
uv run --extra evals --extra mcp --group examples pytest -q examples/redteam
```

The `examples` dependency group pins each framework; none is a dependency of `martian-sdk`. CI runs the four in the `redteam-examples` job.
