# Red team targets

A target is what a red team run attacks. The engine sends it one message at a time and reads its reply. Every target implements one interface, `martian.redteam.targets.Target`:

```python
async def send(message: str, *, session: str | None = None) -> TargetReply
async def close() -> None
```

`TargetReply` holds the reply's `text`, the `tool_calls` the target made or asked for, the `session` the message was sent in, and `latency_ms`.

`target_from_run_file(block)` builds the target a [run file](redteam-run-file.md)'s `target` block names. Building one reads `${NAME}` from the environment and checks the settings; it calls nothing. The first message connects.

## Function and HTTP targets

The `file` and `url` blocks are a suite file's target block, read by the suite file's own rules, so the same block works in an [eval suite](evals.md) and a red team run.

- **Function** (`file`, `function`). The function runs in a process of its own, as a suite file's function target does, and cannot read the run's private files on Linux. It is called with the keyword argument `message`. Its return value must be JSON. Text is the reply; any other value is kept as its JSON text. A function that raises is a target error that names the exception's type only.
- **HTTP** (`url`, `headers`, `body`, `response_path`, `timeout_seconds`). One JSON `POST` per message: no retry, no redirect, no proxy from the environment. `${NAME}` in `url` and header values is read from the environment. The reply is the response's text, or the value at `response_path`; a value that is not text is kept as its JSON. A failed call names the setting to check and never repeats the address, a header or the response.

`HttpTarget(reference, address_policy=...)` pins every connection to an address the policy allows, as the judges' connections are pinned. An edition that calls endpoints other people name passes one. A local run calls the endpoint its user named and passes none.

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

A server that issues its own ids sets `session.response_path`. The first turn of a conversation sends an empty id, and each reply's id at that path is sent with the next turn.
