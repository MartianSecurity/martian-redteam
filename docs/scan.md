# Scanning agent files: `martian scan`

`martian scan` sends a folder's agent files to the Martian Scanner (the Skills / MCP / Plugin Scanner
of Martian Protect), waits for the scan, prints the findings and exits on the server's gate. It needs
a **Scanner-enabled Martian Cloud or Enterprise organisation**: the scan runs on Martian's servers, and
the open-source Community edition has no Scanner.

The command is a thin client. It chooses files by path, zips them, uploads them, polls and prints. It
holds no rule and no detection logic and never judges a file itself; the findings, their evidence
levels and the gate all come from the server. A test (`tests/scan/test_scan_cli.py`) fails if the
scan package imports anything beyond the standard library, httpx, pydantic and the SDK's connection
helpers, or if a rule file or a scanner engine reaches the package.

```bash
martian scan                                   # the current folder's agent files
martian scan path/to/repo --sarif results/scan.sarif --json results/scan.json
martian scan --include 'skills/**' --include src/agent/ --kind agent_code --name support-agent
martian scan --dry-run                         # list what would be sent; needs no token
```

## Access

Set the SDK's usual pair ([SDK and CI](sdk-and-ci.md#one-customer-connection)):

| Setting | Value |
| --- | --- |
| `MARTIAN_API_URL` | The Martian server's origin |
| `MARTIAN_API_TOKEN` | An organisation API key with `scan:run` (upload) and `scan:read` (read the scan, its findings, gate and exports) |

An admin issues the key under the console's **API Keys**. A key bound to an agent uploads subjects
linked to that agent only. An organisation without the Scanner, or a key without the capabilities, is
refused with 403, and the command says what it needs.

## What is sent

1. **The candidates.** Inside a Git work tree, the files `git ls-files --cached --others
   --exclude-standard` lists: tracked and untracked files, without those `.gitignore` and Git's other
   exclude files ignore. Git runs with `core.fsmonitor` off, so a folder's own Git configuration
   cannot start a program. Outside a Git work tree, every file under the folder except `.git/`; no
   ignore file is read then, and the command says so.
2. **The include patterns.** By default, the files coding agents and their harnesses load:
   `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md` and `GEMINI.md` at any depth; `.mcp.json`, `mcp.json`
   and `mcp_config.json` at any depth; `.cursorrules`, `.windsurfrules` and `.clinerules`; everything
   in `.claude/`, `.agents/`, `.cursor/`, `.windsurf/`, `.gemini/`, `.codex/`, `.continue/`, `.roo/`,
   `.kiro/`, `.amazonq/` and `.clinerules/` folders at any depth; and `.github/copilot-instructions.md`
   with `.github/instructions/`, `prompts/`, `chatmodes/` and `agents/`. A folder holding a `SKILL.md`
   or a `.claude-plugin/` folder is sent whole, because a skill or a plugin is its folder
   (`DEFAULT_INCLUDE` and `FOLDER_MARKERS` in `src/martian/scan/files.py`).

   `--include` replaces the defaults; repeat it for more patterns. Patterns are relative to the
   folder: one without a slash matches a name at any depth (`*.md`), `*` matches within one folder
   name, `**` any number of folders (`skills/**`), and a trailing `/` a whole folder (`src/agent/`).
   A pattern with `..` is refused.
3. **Symbolic links are never followed.** A link the patterns could reach that points out of the
   folder refuses the scan (exit 2, naming the links): an agent would read the file it points at,
   which the scan cannot send. A link that stays inside the folder is not sent and is listed as
   `Not sent`; its target is sent in its own right when it is selected. A submodule or another file
   that is not a regular file is listed the same way.

The archive is a ZIP of regular file entries only, sorted, with a fixed time and mode and deflate
compression, so the same files give the same bytes. The command prints the file count, sizes and the
**content digest**: SHA-256 over each file's path, a NUL, the file's SHA-256 in hex and a newline, in
path order. The server computes the same digest from the archive it receives; a scan that reports
another digest is an error (exit 2). The server's limits apply (by default 50 MiB of archive, 5,000
entries, 10 MiB per file); a refusal prints its code and limit.

| Option | Default | Meaning |
| --- | --- | --- |
| `FOLDER` | `.` | The folder to scan. Paths in findings and in SARIF are relative to it |
| `--include`, `-i` | the agent paths above | A pattern of files to send; repeatable |
| `--kind` | `repository` | The subject's kind: `repository`, `skill`, `mcp_server`, `plugin`, `subagent`, `harness_config`, `agent_code` or `registry_entry` |
| `--name` | `GITHUB_REPOSITORY`, else the folder's name | The subject's name; scans of the same kind and name share a subject |
| `--agent-id` | none | Link the subject to a registered agent |
| `--no-model-analysis` | analysis allowed | Keep file contents from any model, whatever the organisation's policy |
| `--timeout` | 600 | Seconds to wait for the scan to end |
| `--json PATH`, `--sarif PATH` | none | Write the server's JSON or SARIF 2.1.0 export, byte for byte |
| `--dry-run` | off | List the files and the digest, upload nothing; needs no token |

The scan's `source` is `ci` when `CI` is `true` or `1` or `GITHUB_ACTIONS` is set, else `cli`.

## The exchange with the server

1. `POST /v1/scanner/scans`, `multipart/form-data` with a `manifest` part
   (`{"subject": {"kind", "name", "agent_id"?}, "source", "options": {"model_analysis"}}`) and then an
   `archive` part. It is sent **once**. The server has no idempotency key for uploads yet, so a
   repeated request would start a second scan: a transport error or a 5xx on the upload leaves its
   outcome unknown, and the command exits 2 with the content digest to look for in the console
   instead of sending it again.
2. `GET /v1/scanner/scans/{id}` until the scan is `completed` or `failed`, with a growing interval
   (1 to 10 seconds) and `--timeout` as the deadline. Reads are retried on transport errors, 429
   (after its `Retry-After`, at most 30 seconds) and 500, 502, 503 and 504, within the deadline. A
   scan still queued or running at the deadline is not a result (exit 2).
3. For a completed scan, `GET /v1/scanner/scans/{id}/findings` (every page), `GET
   /v1/scanner/scans/{id}/check` (the gate), and `GET /v1/scanner/scans/{id}/export?format=json` or
   `format=sarif` when `--json` or `--sarif` asks.

The routes of steps 1 and 2 are the backend's contract: `tests/scan/contract/scanner-scans.json` is a
copy of the customer `/v1/scanner` routes of MartianSecurity/MartianBackend, and every request the
client sends and every answer the tests give it are held to that file, so a route that drifts fails
`tests/scan/test_scan.py`. The findings, check and export routes of step 3 are read when the server
has them: a server without one answers a bare `404 Not Found`, and the command says the server does
not list findings, evaluate a gate or export; any other 404 is an error. Until the backend's contract
holds those routes, the tests list them as pending and hold them to the shapes the backend documents.

Output names files, lines, rule ids, severities, evidence levels, statuses and titles. It never
prints a finding's excerpt, which can quote the file, and it prints no server message except a
failed scan's reason; text from the server is put on one line without control characters, and
known credentials are redacted.

## Exit codes and the gate

| Exit | Meaning |
| --- | --- |
| 0 | The scan completed and the server's gate passed |
| 1 | The scan completed and the server's gate failed: the findings and the gate's reasons are printed |
| 2 | No result: no file to send, a symbolic link out of the folder, missing or refused credentials, a refused upload, a transport error, an invalid answer, a failed scan, the deadline, or an export that could not be written |

The gate is decided on the server. The command reads `GET /scans/{id}/check`, which evaluates the
completed scan, and exits 1 when it answers `passed: false`. The command passes no threshold, so the
check uses the server's defaults: as the backend documents the route, any open finding blocks
(minimum severity `S5`, evidence level 0), accepted and waived findings do not, and a bundle whose
unpacking stopped early always falls short. The gate policy file
(MartianSecurity/martian-notes#470) sets the threshold on the server; the command does not change.
A server that has no check route answers by the scan's state alone: a completed scan exits 0 and the
command says that no gate was evaluated. The exit codes match `martian redteam run` and `martian ci`.

## In GitHub Actions

The scan action (`.github/actions/scan`) installs the SDK and runs `martian scan`, with the token
taken from the job's environment, never from an input. Reference it as
`MartianSecurity/martian-redteam/.github/actions/scan@<tag or full SHA>` after `actions/checkout`.
[examples/scan/workflow.yml](../examples/scan/workflow.yml) is a complete workflow, pinned by commit:
it scans on pushes to `main`, fails the job on the gate and uploads the SARIF to code scanning.

| Input | Default | Meaning |
| --- | --- | --- |
| `path` | `.` | The folder to scan; keep the repository root when the SARIF goes to code scanning, because SARIF paths are relative to it |
| `include` | empty: the agent paths | Patterns, one per line |
| `name` | the repository | The subject's name |
| `output` | `results/scan` | Folder for `scan.sarif` and `scan.json` |
| `timeout` | `600` | Seconds to wait |
| `model-analysis` | `true` | `false` keeps file contents from any model |

Its outputs are `exit-code` and `sarif` (the SARIF's path, when written). The step fails with the
command's exit code after writing both files, so a following step with `if: always()` still uploads
the findings of a failed gate.

- **Pushes to protected branches, not pull requests.** A customer token must not reach code nobody
  has reviewed, and pull requests from forks get no secrets; never use `pull_request_target` for
  this. Pull requests are scanned through the Martian GitHub App, which fetches their files on
  Martian's side. Keep the token in an environment that only protected branches can use.
- **Code scanning.** Upload `scan.sarif` with `github/codeql-action/upload-sarif`, which needs
  `security-events: write`, under one stable `category` (`martian-scan` in the example). The SARIF
  is the server's: one rule per rule id with its help text, and a fingerprint per finding in
  `partialFingerprints` that does not depend on the line, so a re-run on unchanged content updates
  the same alerts instead of adding new ones. The command writes the file byte for byte.
- **The JSON export** holds the findings with their excerpts and receipts. Keep it out of artifacts
  that others can download if the scanned files hold anything private.

This repository's CI runs the action twice against a stand-in for the backend's scan routes
(`tests/scan/fake_backend.py`, which reports one seeded finding on every `SKILL.md`) on the fixture
repository in [examples/scan/fixture](../examples/scan/fixture/): without the skill the gate passes,
with it the step fails with exit 1 and writes SARIF with one result. It does not reach a Martian
server.
