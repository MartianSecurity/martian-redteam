# Martian SDK

The Python SDK and CLI for **Martian Protect**: security evals and adversarial
red teaming for AI agents, runnable from code and from CI.

- `martian.evals`: pinned datasets, local MLflow evaluation, Backend-scored
  regression gates and recorded CI sessions.
- `martian.redteam`: adversarial probes against a customer callback, with
  simulation and judging through the Martian Backend API.
- `assert_redteam(...)`: record saved adversarial probe results alongside
  dataset and quality checks in one eval session.
- `martian ci`: one persisted session for evals, redteam, or both, with typed Python results.

The distribution is `martian-sdk`, published on
[PyPI](https://pypi.org/project/martian-sdk/). Install its `evals` extra for both
modules:

```bash
python -m pip install 'martian-sdk[evals]'
```

What runs where:

- **Evals** run in your process against pinned datasets. Local judges
  (`response_guidelines`, `response_conditions`, `response_rubric`) grade replies
  with a model you supply; dataset scores and CI sessions are recorded by the
  Martian platform.
- **Red team** probes run in your process and call your agent through a local
  callback. Attack generation and judging are done by the Martian platform, so
  red team needs a platform connection and a token with `redteam:run`.
- Hosted features also need the SDK APIs enabled for your organisation.
  Installing the package does not enable them.

From a source checkout:

```bash
uv sync --locked --extra evals
uv run --extra evals martian --help
uv run --extra evals martian redteam validate examples/config.yaml
# In the customer checkout, after configuring the Backend origin and one Martian API token:
martian ci --evals-path tests/evals --redteam-config redteam.yaml --output results
```

[Unified SDK and CI](https://github.com/MorningHQ/martian-redteam/blob/main/docs/sdk-and-ci.md) documents imports, the shared connection,
installation, the combined gate and compatibility.
[Evals](https://github.com/MorningHQ/martian-redteam/blob/main/docs/evals.md) documents the regression gate and the local judges.
[Documentation index](https://github.com/MorningHQ/martian-redteam/blob/main/docs/README.md) lists every guide.

The wheel retains `martian_redteam`, `martian_evals_kit` and `martian-redteam`
for existing integrations. Both test engines run in the customer's process.
The Backend admits and stores customer work; Agents owns redteam simulation and judging.
The same Backend API key authorizes both modules when granted the required capabilities.

The optional MCP binding, runtime metadata reporter and cooperative call gate are
integration extras with their own credentials. They are not a Martian product line.

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
qualification. See [development](https://github.com/MorningHQ/martian-redteam/blob/main/docs/development.md).

## License

Apache-2.0. See [LICENSE](https://github.com/MorningHQ/martian-redteam/blob/main/LICENSE)
and [NOTICE](https://github.com/MorningHQ/martian-redteam/blob/main/NOTICE).
