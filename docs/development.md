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
uv run mypy src
uv run pytest -q
uv run python -m compileall -q src
```

Ruff: `E,F,I,UP,B,SIM`, Python 3.12, line length 120. Mypy: `strict` on `martian_redteam`.

CI runs the same sequence. Format check is `--check` (no rewrite).

## Tests

- **No live network.** `respx` intercepts `https://agents.test.example/runs/wait`.
- Fixtures in `tests/conftest.py` plus JSON under `tests/fixtures/`.
- `pytest-asyncio` mode `auto`.
- CLI tests use Typer’s `CliRunner` (catalog and `--version` only; `run` would need a key and mocks).

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

- [ ] Tests, ruff, mypy green on `main`
- [ ] Docs match request/response shapes
- [ ] `NOTICE` still accurate if catalog IDs change
- [ ] Do **not** publish to PyPI until per-key identity and quotas exist on martian-agents
- [ ] Do **not** point this package at the generic `martian_agents` assistant as a red-team target

## Related repos

| Repo | Role |
| --- | --- |
| [MorningHQ/martian-redteam](https://github.com/MorningHQ/martian-redteam) | This SDK |
| [MorningHQ/martian-agents](https://github.com/MorningHQ/martian-agents) | Hosted graphs, Aegra, Bedrock, infra |
| confident-ai/deepteam (local checkout only) | Taxonomy reference, not a publishable fork |
