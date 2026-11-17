# Red team targets

A target is what a red team run attacks. The engine sends it one message at a time and reads its reply. Every target implements one interface, `martian.redteam.targets.Target`:

```python
async def send(message: str, *, session: str | None = None) -> TargetReply
async def close() -> None
```

`TargetReply` holds the reply's `text`, the `tool_calls` the target made or asked for, the `session` the message was sent in, and `latency_ms`.

`target_from_run_file(block)` builds the target a [run file](redteam-run-file.md)'s `target` block names: a `FunctionTarget`, an `HttpTarget` or an `McpServerTarget`. Building one reads `${NAME}` from the environment and checks the settings; it calls nothing. The first message connects.

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
- A new session is the reset signal. It has a new id and an empty history, so a server that keeps state by the id starts fresh. `end(session)` forgets a conversation.

What the target sends:

| Target | Conversation id | History |
| --- | --- | --- |
| Function | The `session_id` parameter, when the function takes one | The `history` parameter: earlier turns as `user` and `assistant` messages |
| HTTP | `{{session_id}}` in `body`. `session.header` also sends it in a header | `{{history}}` is the earlier turns; `{{messages}}` is those and the new message. A body with only `{{input}}` sends the newest turn |
| MCP | `session_argument`, and `reset_argument` is `true` on the first turn | Not sent: the server keeps it |

A server that issues its own ids sets `session.response_path`. The first turn of a conversation sends an empty id, and each reply's id at that path is sent with the next turn.

## Reply contract

A target reports the tool calls it made or asked for, so a check can ask whether the agent called `issue_refund` with an amount above a limit, and whether the call ran:

- an HTTP target: the list at `tool_calls_path` in its JSON answer. An answer with nothing at the path made no calls;
- a function: an object `{"text": "...", "tool_calls": [...]}` with no other keys;
- a version 0.2 callback: a `Turn` with `tool_calls`, or its names-only `tools_called`.

Each tool call is an object, or a tool's name as text (which says nothing about arguments or whether it ran):

| Field | Meaning |
| --- | --- |
| `name` | The tool. OpenAI's `{"function": {"name", "arguments"}}` form is read too |
| `arguments` | An object, or JSON text of one. `args` and Anthropic's `input` are read too |
| `id` or `call_id` | The call's id |
| `executed` | `true` when the call ran, `false` when it was refused or blocked. Leave it out when the agent only asked for the call: a check that needs an executed call never counts one without it |
| `result` | What the tool returned, as text or any JSON |

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
