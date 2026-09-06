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
