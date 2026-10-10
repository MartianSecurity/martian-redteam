# Repository instructions

Read README.md, then docs/README.md and docs/development.md as needed before changing this repository. docs/architecture.md, docs/agent-protocol.md and docs/gate-and-ci.md hold the contracts.

This is the in-process adversarial-testing SDK and CLI (`martian-redteam`, Python 3.12+, hatchling `src/martian_redteam`). Probes are orchestrated in the customer's process; attack generation and judging run on martian-agents through `POST /runs/wait`, and the customer's model stays behind a local `model_callback`. It is an internal scaffold: do not publish to PyPI and do not point untrusted CI at the shared staging API key.

- The catalog holds vulnerability and attack ids only. Rubric prose belongs in the martian-agents evaluator graph, not here. Bump `CATALOG_VERSION` when ids, types or gate flags change.
- The gate fails closed on transport/schema errors and on gated pass rate. Do not add fallback or retry-to-pass behavior unless explicitly requested.
- `martian_redteam.__all__` is the supported surface. Keep `protocol`, `config` and `catalog.get_*` internal.
- Keep GitHub Actions SHA-pinned; never revert a pin to a floating tag. Customer examples pin the SDK to a tag or SHA, not `@main`.
- The generic `martian_agents` assistant is not a valid red-team target.
- `pr-reviews/` is untracked local review output; leave it alone.

## Commands

Run from the repository root, matching `.github/workflows/ci.yml`:

```bash
uv sync --locked
uv run ruff format --check src tests examples
uv run ruff check src tests examples
uv run mypy src
uv run pytest -q
uv run python -m compileall -q src
```

Tests never touch the network (`respx` fakes `/runs/wait`) and need no `MARTIAN_API_KEY` or `OPENAI_API_KEY`.

## Working preferences

- Keep changes within the requested scope and preserve unrelated dirty work.
- Follow docs/development.md for tests: catalog changes need the catalog test it describes; do not add other test infrastructure unless explicitly requested.
- Inspect this repository's actual branch and git status; do not rely on an old branch name in a document.
- Do not commit, push or deploy unless requested. Do not expose secrets or raw customer data in code, docs or logs.

## Documentation is part of the change

Update the affected guide in the same change when request/response shapes, CLI flags (`validate`, `-c`, `-a`, `-o`), catalog ids, gate behavior or customer Action inputs change. Keep README.md short; keep contracts in docs/. Update both ends of an integration contract when martian-agents is also in the workspace; otherwise name the follow-up repository and change. Run `git diff --check` and verify changed links and commands against the repository. In the final handoff, state which docs changed and what was verified.

CLAUDE.md is a symlink to this file so every agent shares these rules. Edit AGENTS.md only.
