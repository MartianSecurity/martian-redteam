# Repository instructions

`AGENTS.md` and `.agents/skills/` are canonical. `CLAUDE.md` and `.claude/skills` are relative symlinks; edit the originals.

Read README.md, then docs/README.md and docs/development.md as needed before changing this repository. docs/architecture.md, docs/agent-protocol.md and docs/gate-and-ci.md hold the contracts.

This SDK is the customer entry point for **Martian Protect** (Evals and Red Team) outside the console. The optional MCP binding, runtime reporter and call gate are integration extras, not a product line; do not extend them unless a current product needs it.

Link the branch and pull request to the relevant issue on the Martian Security Board (https://github.com/orgs/MartianSecurity/projects/2), normally in `MartianSecurity/martian-notes`. Reuse an existing issue. Create or update tracker records only within the user's authorized scope; local research and reviewable edits can proceed while a missing issue link is reported before publication.

The distribution is `martian-sdk`, with `martian.evals` and `martian.redteam`
modules and a combined `martian ci` gate. See docs/sdk-and-ci.md. The relocated
eval engine is canonical here; compatibility namespaces retain old imports.

This includes the in-process adversarial-testing SDK and CLI (`martian-redteam`, Python 3.12+, hatchling `src/martian_redteam`). Probes are orchestrated in the customer's process; modern customer calls go through typed Backend `/v1/redteam` operations, where Agents generates and judges attacks; a version 2 run file that names `models` generates and judges them locally instead (`martian.redteam.engine`), and the customer's model stays behind a local `model_callback`. The package is published on PyPI; publishing a new version requires a separate explicit user request. Do not expose customer API tokens to unreviewed CI code. Legacy direct-Agents access is explicitly operator-only, with a dedicated origin/key pair.

- The catalog holds vulnerability and attack ids, and the one copy of the generator and judge prompts (`martian.redteam.catalog.prompts`), which martian-agents imports. Bump `CATALOG_VERSION` when ids, types or gate flags change; a change to the prompt text changes `PROMPTS_REVISION` and needs `tests/redteam/fixtures/hosted-prompt-digests.json` regenerated on purpose.
- Red team content (prompts, catalogue, attack methods, datasets) ships only with an entry in `src/martian/redteam/catalog/licences.json` under a permitted licence; `scripts/content_licences.py` checks it. Never copy from `martian-agents`, `martian-skills` or `martian-wiki`: they have no licence.
- The gate fails closed on transport/schema errors and on gated pass rate. Retry only documented transient failures within the existing total deadline; reconcile uncertain run admission before resubmission. Never retry-to-pass or turn an error into a passing result.
- Preserve the documented modern `martian`, `martian.evals` and `martian.redteam` exports alongside the legacy `martian_redteam.__all__` compatibility surface; see docs/sdk-and-ci.md. Keep legacy `protocol`, `config` and `catalog.get_*` internal.
- Execution POSTs are sent once; reconcile uncertain admission/status with the documented reads instead of resubmitting work. Do not reinterpret a timeout or cancellation as permission to replay a target action.
- Keep GitHub Actions SHA-pinned; never revert a pin to a floating tag. Customer examples pin the SDK to a tag or SHA, not `@main`.
- The generic `martian_agents` assistant is not a valid red-team target.
- `pr-reviews/` is untracked local review output; leave it alone.

## Commands

Run from the repository root, matching `.github/workflows/ci.yml`:

```bash
uv sync --locked --extra evals
uv run --extra evals ruff format --check src tests examples scripts
uv run --extra evals ruff check src tests examples scripts
uv run --extra mcp --extra evals mypy src
uv run --extra evals --extra mcp pytest -q -n auto
uv run --extra evals python -m compileall -q src
```

Redteam tests cover typed Backend operations and the retained legacy `/runs/wait` adapter; eval tests use local MLflow and loopback fixtures. Neither suite uses external services or requires customer/provider credentials.

## Working preferences

- Keep changes within the requested scope and preserve unrelated dirty work.
- Follow docs/development.md for test isolation and commands. Extend focused coverage for meaningful bugs, public contracts, gate failures and data integrity; reuse fixtures rather than adding a new test framework. Catalog changes need the documented catalog test.
- Inspect this repository's actual branch and git status; do not rely on an old branch name in a document.
- Do not commit, push or deploy unless requested. Do not expose secrets or raw customer data in code, docs or logs.

## Verification

- Start with affected tests and preserve the CI aggregator's packaging, Action, Evals core and compatibility gates. Local test fixtures and mocked transports establish their own contracts, not hosted service readiness.
- Exercise changed CLI behavior through the real CLI. Credential-free local commands such as `validate`, help, conversion or file export need valid/invalid local inputs, exit codes and file readback where applicable; do not invent a service prerequisite. Changes to network/API behavior require the actual affected disposable service path: success, relevant failure/auth denial and persisted readback. Use the real Backend/Agents contract for hosted Red Team and the real local server for local Evals API/UI changes. Click through changed UI journeys without intercepting the behavior being qualified.
- Retain isolation from the developer's data folder and use the fixture rules in docs/development.md. State the exact revision, environment and any unavailable prerequisites. A published wheel, container image and hosted rollout have separate outcomes; do not claim one from another. Instruction-only edits need changed-path/command checks and `git diff --check`.

## Documentation is part of the change

Update the affected guide in the same change when request/response shapes, CLI flags (`validate`, `-c`, `-a`, `-o`), catalog ids, gate behavior or customer Action inputs change. Keep README.md short; keep contracts in docs/. Update all directly affected producers and consumers when available; otherwise name the repository and exact follow-up. Typed Red Team operations are versioned with MartianBackend; Evals API changes involve the SDK core, Backend contract and Frontend fixture; graph/model contracts involve martian-agents. Preserve the unchanged generated contract copies and version-bump procedure in Backend's usage guide. The vendored UI build/release relationship is separate from the HTTP contract. Run `git diff --check` and verify changed links and commands against the repository. In the final handoff, state which docs changed and what was verified.

CLAUDE.md is a symlink to this file so every agent shares these rules. Edit AGENTS.md only.
