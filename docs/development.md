# Development

## Layout

Editable install from the repo root (`uv sync`). Package lives under `src/martian_redteam` (hatchling src layout).

| Path | Role |
| --- | --- |
| `src/martian_redteam/protocol.py` | Wait-body JSON, envelope unwrap, Retry-After |
| `src/martian_redteam/client.py` | httpx client, retries, schema errors |
| `src/martian_redteam/orchestrator.py` | Probe loop and summaries |
| `src/martian_redteam/catalog/` | IDs only |
| `tests/fixtures/` | Recorded `/runs/wait` graph state |
| `docs/` | Architecture and contracts |

## Commands

```bash
uv sync --locked
uv run ruff format src tests examples
uv run ruff check src tests examples
uv run --extra mcp mypy src   # the mcp extra provides the types mcp_binding.py imports
uv run pytest -q
uv run python -m compileall -q src
```

Optional local coverage (CI always produces XML + JUnit):

```bash
uv run pytest -q --cov=martian_redteam --cov-report=term-missing --cov-report=xml --junitxml=junit.xml
```

Ruff: `E,F,I,UP,B,SIM`, Python 3.12, line length 120. Mypy: `strict` on `martian_redteam`.

## This repository’s CI

`.github/workflows/ci.yml` is split so branch protection can require a single aggregator:

| Job | Command | Python |
| --- | --- | --- |
| `lint` | `uv sync --locked`, `ruff format --check`, `ruff check` | 3.12 |
| `types` | `uv sync --locked`, `mypy src` with the `mcp` extra, `compileall` | 3.12 |
| `test` | `uv sync --locked`, pytest with coverage + JUnit | **3.12 and 3.13** (matrix) |
| `packaging` | `uv build`, throwaway venv install, `validate examples/config.yaml` | 3.12 |
| `action-smoke` | `uses: ./.github/actions/validate` | 3.12 |
| `ci` | Succeeds only if every job above succeeded | — |

Require the **`ci`** check on `main`, not the individual matrix cells. Format is `--check` (no rewrite).

`test` uploads `coverage.xml` and `junit.xml` as artifacts (including on failure). It does **not** call Martian Agents, does **not** set `MARTIAN_API_KEY` or `OPENAI_API_KEY`, and does not run `martian-redteam run`.

## Testing this library

The product is a CI/CD SDK. GitHub Actions is the harness; this package is the subject. Do not red-team `martian_agents` as if it were the customer app.

| Layer | Where | Secrets | Required on PRs |
| --- | --- | --- | --- |
| 1. Library | `pytest` + recorded `/runs/wait` fixtures | none | yes |
| 2. Packaging / Action | wheel install + `validate`; composite `.github/actions/validate` | none | yes |
| 3. Live dogfood | `martian-redteam run` / root `action.yml` / `red-team.yml` against staging graphs | `MARTIAN_API_KEY` | **no** (dispatch + environment, after Phase 0) |

Layer 2 uses a nested validate composite so the customer gate Action (`action.yml`) stays **run-only**. That avoids a `command: validate` switch that could later be forgotten on `pull_request` once a staging key exists.

Third-party actions are SHA-pinned. Concurrency cancels in-progress runs on the same ref.

Customer red-team workflows (reusable workflow, composite action, `validate` / `run -c -a`) are documented in [gate and CI](gate-and-ci.md). They are not this repo’s unit-test pipeline.

## Dependabot

`.github/dependabot.yml` opens **weekly** PRs for:

- `package-ecosystem: uv` — `pyproject.toml` / `uv.lock`
- `package-ecosystem: github-actions` — action SHAs used by CI, the reusable workflow, and the composite action

Review action bumps by comparing SHAs. Do not merge a bump that reverts a pin to a floating tag (`@v4`, `@main`).

## Tests

- **No live network.** `respx` intercepts `https://agents.test.example/runs/wait`.
- Fixtures in `tests/conftest.py` plus JSON under `tests/fixtures/`.
- `pytest-asyncio` mode `auto`.
- CLI tests use Typer’s `CliRunner` for `--version`, `catalog`, and `validate` (`tests/test_cli.py`). `tests/test_cli_run.py` drives `run` end to end with `respx` in place of the hosted API and asserts the reports are written and the exit code is `0` on a pass and `1` on a gated failure or an infrastructure error. They never need a real key or OpenAI.

When martian-agents Phase 0 contract tests exist, copy real wait bodies into `tests/fixtures/` and keep envelope/heartbeat cases.

## Adding a vulnerability

1. Add a factory in `catalog/vulnerabilities.py` with `id`, `label`, `allowed_types`, and `gated`.
2. Register it in `VULNERABILITIES` (PascalCase key). Lookup aliases are generated from the name and `id`.
3. Add YAML/Python examples and a catalog test.
4. **Do not** put rubric prose in the SDK. Add the rubric to the evaluator graph in martian-agents and extend that graph’s `context_schema` if needed.
5. Bump `CATALOG_VERSION` when IDs/types/gate flags change in a way reports must distinguish.

## Adding an attack

- **LLM kind:** new `attack_method` the simulator graph understands. Coordinate with martian-agents first.
- **Encoding kind:** add a branch in `encoding.apply_encoding` and an `Attack` factory. Keep it side-effect free and reversible when possible (ROT13 is; Base64 encode is not expected to round-trip through the target).

## Public API

`martian_redteam.__all__` is the supported surface. Prefer factories (`PromptLeakage()`) over constructing `Vulnerability` by hand. `ClientError`, `RetryableClientError`, `GateFailure`, and `evaluate_gate` are exported for CI wrappers.

Internal modules (`protocol`, `config`, `catalog.get_*`) may change without a major version while the package is `0.x`.

## Release checklist (internal)

- [ ] `ci` aggregator green on `main` (lint, types, test 3.12 + 3.13, packaging, action-smoke)
- [ ] Docs match request/response shapes, CLI flags (`validate`, `-c`/`-a`/`-o`), and customer Actions
- [ ] Coverage / JUnit artifacts still produced; Dependabot ecosystems still `uv` + `github-actions`
- [ ] `NOTICE` still accurate if catalog IDs change
- [ ] Do **not** publish to PyPI until per-key identity and quotas exist on martian-agents
- [ ] Do **not** point this package at the generic `martian_agents` assistant as a red-team target
- [ ] Customer examples pin the SDK to a tag or SHA, not floating `@main`

## Related repos

| Repo | Role |
| --- | --- |
| [MorningHQ/martian-redteam](https://github.com/MorningHQ/martian-redteam) | This SDK |
| [MorningHQ/martian-agents](https://github.com/MorningHQ/martian-agents) | Hosted graphs, Aegra, Bedrock, infra |
| confident-ai/deepteam (local checkout only) | Taxonomy reference, not a publishable fork |
