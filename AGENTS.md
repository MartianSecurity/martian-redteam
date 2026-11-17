# Repository instructions

Read README.md, then docs/README.md and docs/development.md as needed before changing this repository. docs/architecture.md, docs/agent-protocol.md and docs/gate-and-ci.md hold the contracts.

This SDK is the customer entry point for **Martian Protect** (Evals and Red Team) outside the console. The optional MCP binding, runtime reporter and call gate are integration extras, not a product line; do not extend them unless a current product needs it.

Every change starts with an issue on the Martian Security Board (https://github.com/orgs/MartianSecurity/projects/2), filed in `MartianSecurity/martian-notes`. Each branch is tied to one issue and its pull request links it.

The distribution is `martian-sdk`, with `martian.evals` and `martian.redteam`
modules and a combined `martian ci` gate. See docs/sdk-and-ci.md. The relocated
eval engine is canonical here; compatibility namespaces retain old imports.

This includes the in-process adversarial-testing SDK and CLI (`martian-redteam`, Python 3.12+, hatchling `src/martian_redteam`). Probes are orchestrated in the customer's process; modern customer calls go through typed Backend `/v1/redteam` operations, where Agents generates and judges attacks; a version 2 run file that names `models` generates and judges them locally instead (`martian.redteam.engine`), and the customer's model stays behind a local `model_callback`. The package is published on PyPI; publishing a new version requires a separate explicit user request. Do not expose customer API tokens to unreviewed CI code. Legacy direct-Agents access is explicitly operator-only, with a dedicated origin/key pair.

- The catalog holds vulnerability and attack ids, and the one copy of the generator and judge prompts (`martian.redteam.catalog.prompts`), which martian-agents imports. Bump `CATALOG_VERSION` when ids, types or gate flags change; a change to the prompt text changes `PROMPTS_REVISION` and needs `tests/redteam/fixtures/hosted-prompt-digests.json` regenerated on purpose.
- The gate fails closed on transport/schema errors and on gated pass rate. Do not add fallback or retry-to-pass behavior unless explicitly requested.
- `martian_redteam.__all__` is the supported surface. Keep `protocol`, `config` and `catalog.get_*` internal.
- Keep GitHub Actions SHA-pinned; never revert a pin to a floating tag. Customer examples pin the SDK to a tag or SHA, not `@main`.
- The generic `martian_agents` assistant is not a valid red-team target.
- `pr-reviews/` is untracked local review output; leave it alone.

## Commands

Run from the repository root, matching `.github/workflows/ci.yml`:

```bash
uv sync --locked --extra evals
uv run --extra evals ruff format --check src tests examples
uv run --extra evals ruff check src tests examples
uv run --extra mcp --extra evals mypy src
uv run --extra evals pytest -q -n auto
uv run --extra evals python -m compileall -q src
```

Redteam tests cover typed Backend operations and the retained legacy `/runs/wait` adapter; eval tests use local MLflow and loopback fixtures. Neither suite uses external services or requires customer/provider credentials.

## Working preferences

- Keep changes within the requested scope and preserve unrelated dirty work.
- Follow docs/development.md for tests: catalog changes need the catalog test it describes; do not add other test infrastructure unless explicitly requested.
- Inspect this repository's actual branch and git status; do not rely on an old branch name in a document.
- Do not commit, push or deploy unless requested. Do not expose secrets or raw customer data in code, docs or logs.

## Documentation is part of the change

Update the affected guide in the same change when request/response shapes, CLI flags (`validate`, `-c`, `-a`, `-o`), catalog ids, gate behavior or customer Action inputs change. Keep README.md short; keep contracts in docs/. Update both ends of an integration contract when martian-agents is also in the workspace; otherwise name the follow-up repository and change. Run `git diff --check` and verify changed links and commands against the repository. In the final handoff, state which docs changed and what was verified.

CLAUDE.md is a symlink to this file so every agent shares these rules. Edit AGENTS.md only.
