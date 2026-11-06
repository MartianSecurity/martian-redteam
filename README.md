# Martian SDK

One Python SDK for agent evaluations and adversarial testing.

- `martian.evals`: pinned datasets, local MLflow evaluation, Backend-scored
  regression gates and recorded CI sessions.
- `martian.redteam`: adversarial probes against a customer callback, with
  simulation and judging through the Martian Backend API.
- `assert_redteam(...)`: record saved adversarial probe results alongside
  dataset and quality checks in one eval session.
- `martian ci`: one persisted session for evals, redteam, or both, with typed Python results.

The distribution is `martian-sdk`; install its `evals` extra for both modules.
This source implementation is internal and unpublished. Hosted use requires
released Backend CI APIs and authorized tenant credentials.

```bash
uv sync --locked --extra evals
uv run --extra evals martian --help
uv run --extra evals martian redteam validate examples/config.yaml
# In the customer checkout, after configuring the Backend origin and one Martian API token:
martian ci --evals-path tests/evals --redteam-config redteam.yaml --output results
```

[Unified SDK and CI](docs/sdk-and-ci.md) documents imports, the shared connection,
installation, the combined gate, compatibility and rollout limits.
[Documentation index](docs/README.md) covers the existing redteam contracts.

The wheel retains `martian_redteam`, `martian_evals_kit` and `martian-redteam`
for existing integrations. Both test engines run in the customer's process.
The Backend admits and stores customer work; Agents owns redteam simulation and judging.
The same Backend API key authorizes both modules when granted the required capabilities.

## Development

```bash
uv sync --locked --extra evals
uv run --extra evals ruff format --check src tests examples
uv run --extra evals ruff check src tests examples
uv run --extra mcp --extra evals mypy src
uv run --extra evals pytest -q
uv build
```

The existing eval regression suite has moved into this SDK's CI. Tests use
local MLflow and loopback service fixtures; hosted/paid execution is a separate
qualification. See [development](docs/development.md).

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
