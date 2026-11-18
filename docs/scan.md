# Scanning agent files: `martian scan`

`martian scan` sends a folder's agent files to the Martian Scanner (the Skills / MCP / Plugin Scanner
of Martian Protect), waits for the scan, prints the findings and exits on the gate the server evaluates
against the repository's [policy file](#the-policy-file-martianscanyml). It needs
a **Scanner-enabled Martian Cloud or Enterprise organisation**: the scan runs on Martian's servers, and
the open-source Community edition has no Scanner.

The command is a thin client. It chooses files by path, zips them, uploads them, polls and prints. It
holds no rule and no detection logic, never reads the policy file's fields itself and never judges a
file; the findings, their evidence levels, the policy file's meaning and the gate all come from the
server. A test (`tests/scan/test_scan_cli.py`) fails if the
scan package imports anything beyond the standard library, httpx, pydantic and the SDK's connection
helpers, or if a rule file or a scanner engine reaches the package.

```bash
martian scan                                   # the current folder's agent files
martian scan path/to/repo --sarif results/scan.sarif --json results/scan.json
martian scan --include 'skills/**' --include src/agent/ --kind agent_code --name support-agent
martian scan --dry-run                         # list what would be sent; needs no token
martian scan --policy ci/scan.yml --default-branch main
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
2. **The policy file's `paths` and `include`**, as the server read them (below): only files under
   one of `paths` are candidates, and with the default patterns only files of the add-on families
   `include` names are sent. Each file the default patterns send belongs to one family by its path:
   `plugins` (in a folder holding `.claude-plugin/`), `skills` (in a folder holding `SKILL.md`, or
   under an agent folder's `skills/`), `mcp` (`.mcp.json`, `mcp.json`, `mcp_config.json`),
   `subagents` (an agent folder's `agents/` or `chatmodes/`), else `harness-config`. `agent-code` has
   no default path: name the agent's code with `--include`. `--include` chooses the files itself, so
   families do not apply to it; `paths` still do. The command prints how many files `include` left
   out (`family_of` in `src/martian/scan/files.py`).
3. **The include patterns.** By default, the files coding agents and their harnesses load:
   `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md` and `GEMINI.md` at any depth; `.mcp.json`, `mcp.json`
   and `mcp_config.json` at any depth; `.cursorrules`, `.windsurfrules` and `.clinerules`; everything
   in `.claude/`, `.agents/`, `.cursor/`, `.windsurf/`, `.gemini/`, `.codex/`, `.continue/`, `.roo/`,
   `.kiro/`, `.amazonq/` and `.clinerules/` folders at any depth; and `.github/copilot-instructions.md`
   with `.github/instructions/`, `prompts/`, `chatmodes/` and `agents/`. A folder holding a `SKILL.md`
   or a `.claude-plugin/` folder is sent whole, because a skill or a plugin is its folder
   (`DEFAULT_INCLUDE` and `FOLDER_MARKERS` in `src/martian/scan/files.py`).

   `--include` replaces the defaults; repeat it for more patterns. Patterns are relative to the
   folder: one without a slash matches a name at any depth (`*.md`) and a leading `/` anchors it
   to the folder (`/AGENTS.md`); `*` matches within one folder name, `**` any number of folders
   (`skills/**`), and a trailing `/` a whole folder (`src/agent/`). A pattern with `..` is refused.
4. **Symbolic links are never followed.** A link the patterns could reach that points out of the
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
| `--dry-run` | off | List the files and the digest, upload nothing; needs no token (without one the policy file is not applied) |
| `--policy PATH` | `.martian/scan.yml` in the folder | The gate policy file; the folder's is optional, a named one must exist |
| `--default-branch NAME` | GitHub's event, else `origin/HEAD` | The repository's default branch, stated with the commit for the [baseline](#the-commit-and-the-baseline) |

The scan's `source` is `ci` when `CI` is `true` or `1` or `GITHUB_ACTIONS` is set, else `cli`.

## The policy file: `.martian/scan.yml`

The repository's gate: what to send, the threshold by evidence level, severity and category, the least
coverage, whether only new findings fail, and waivers with a reason and an end date. The server reads
and checks it; this table is its format:

| Field | Default | Meaning |
| --- | --- | --- |
| `version` | `1` | The file format; only `1` |
| `paths` | `["."]` | What the command sends, relative to the repository (no absolute path, no `..`); 1 to 50 |
| `include` | all six | The add-on families the command sends: `skills`, `mcp`, `plugins`, `subagents`, `harness-config`, `agent-code` |
| `external.follow` | `false` | Resolve what the files point at: the gate then requires the `resolve` stage to have run |
| `external.depth` | `1` | Hops of references to follow, 1 to 5 |
| `gate.fail_on.evidence` | `inventory` | The least evidence level that blocks: `inventory`, `pattern`, `structural`, `resolved`, `assessed`, `observed` or `corroborated` |
| `gate.fail_on.severity` | `info` | The least severe severity that blocks: `critical`, `high`, `medium`, `low` or `info` (or `S1` to `S5`); `high` blocks critical and high |
| `gate.fail_on.categories` | all | Only findings of these categories block |
| `gate.min_coverage.stages` | none | Stages that must have run: `inventory`, `parse`, `rules`, `references`, `resolve`, `fetch`, `assess`, `observe` |
| `gate.min_coverage.levels_reached` | none | The least evidence level the stages run must reach, by name |
| `gate.min_coverage.max_skipped_files`, `max_not_inspected_files`, `max_unresolved_references`, `max_not_attempted_references` | none | At most so many |
| `gate.new_findings_only` | `false` | Block only on findings the [baseline](#the-commit-and-the-baseline) does not have |
| `waivers[].rule`, `.path`, `.reason`, `.expires` | none | A finding of `rule` on exactly `path` does not block until `expires` (a date, at most a year ahead); all four required, up to 200, one per rule and path |

An unknown field at any level is refused, so a misspelt threshold never passes silently. YAML anchors,
aliases, tags beyond YAML's own and a key given twice are refused too.

```yaml
version: 1
paths: ["."]
include: [skills, mcp, plugins, subagents, harness-config]
gate:
  fail_on: {evidence: structural, severity: high}
  new_findings_only: true
waivers:
  - {rule: REF-UNPINNED-PACKAGE, path: .mcp.json, reason: "Served from the internal mirror", expires: 2027-01-31}
```

The command reads the file's text (at most 64 KiB, UTF-8, a regular file: a symbolic link is refused)
and sends it to `POST /v1/scanner/gate-policy/validate` before it packs anything. A refused file, an
unknown field at any level among the reasons, exits 2 with each error's place and reason, and nothing
is uploaded. Without a file the text is empty, which the server reads as the strictest gate: every open
finding blocks. After the scan the same text goes to `POST /v1/scanner/scans/{id}/gate`, which judges
the scan and stores the evaluation; the command prints its first line and the evaluation's id.

## The commit and the baseline

Inside a Git work tree the upload names the commit the files are of (the manifest's `git`): `sha`
(`HEAD`), `ref` (`GITHUB_REF` in a GitHub Actions run of that commit, else the checked-out branch),
`default_branch` (`--default-branch`, else the GitHub event's repository, else `origin/HEAD`),
`repository` and `run_url` in GitHub Actions. It names it only when every file sent is that commit's:
a changed, staged or untracked file among them leaves the commit out, and the command says how many
files differ. A scan whose `ref` is its `default_branch` is a baseline; with `gate.new_findings_only`
the server compares a later scan of the same subject with the newest such scan, by fingerprint, so a
finding the default branch already had does not fail and a new one does. The command prints the
baseline the server used, or that there was none.

## The exchange with the server

1. `POST /v1/scanner/gate-policy/validate` with the policy file's text. It changes nothing, and is
   retried like the reads below.
2. `POST /v1/scanner/scans`, `multipart/form-data` with a `manifest` part
   (`{"subject": {"kind", "name", "agent_id"?}, "source", "options": {"model_analysis"}, "git"?}`) and
   then an `archive` part, with an `Idempotency-Key` header: a request id the command chooses once per
   run. On a transport error, 429 (after its `Retry-After`, at most 30 seconds) and 500, 502, 503 and
   504, the same bytes go again under the same id, at most four times; the server answers an upload it
   already has with the scan it created (200), so a lost answer never starts a second scan. A 409
   `request_id_reused` is an error and is not retried. After four failed attempts the command exits 2
   with the request id and the content digest to look for.
3. `GET /v1/scanner/scans/{id}` until the scan is `completed` or `failed`, with a growing interval
   (1 to 10 seconds) and `--timeout` as the deadline. Reads are retried on transport errors and the
   same statuses within the deadline. A scan still queued or running at the deadline is not a result
   (exit 2).
4. For a completed scan, `GET /v1/scanner/scans/{id}/findings` (every page) and
   `GET /v1/scanner/scans/{id}/export?format=json` or `format=sarif` when `--json` or `--sarif` asks.
5. `POST /v1/scanner/scans/{id}/gate` with the policy file's text, once, also for a failed scan (the
   server then answers `not_judged`).

Every route the command calls is in the backend's contract: `tests/scan/contract/scanner-scans.json`
is an unchanged copy of `contracts/scanner-scans.json` in MartianSecurity/MartianBackend, generated
there from the customer `/v1/scanner` routes (the worker's are left out) and versioned. Every request
the client sends (its headers, the manifest and the JSON bodies) and every answer the tests give it are
held to that file, so a route that drifts fails `tests/scan/test_scan.py`. When the backend raises the
contract's version, copy the file again unchanged and run the tests. A server without the gate routes
answers them with a bare `404 Not Found`, and the command exits 2 before uploading; a server without
the findings or export routes is told apart the same way.

Output names files, lines, rule ids, severities, evidence levels, statuses and titles, marks a hunt
finding (`gating: false`, from a rule still being tuned) that never fails the gate, and prints the
gate's reasons. It never prints a finding's excerpt, which can quote the file, and prints no other
server message except a failed scan's reason and a refused policy file's errors; text from the server
is put on one line without control characters, and known credentials are redacted.

## Exit codes and the gate

| Exit | Meaning |
| --- | --- |
| 0 | The server's gate passed |
| 1 | The server's gate failed: blocking findings or a coverage shortfall, with the reasons printed |
| 2 | No verdict: no file to send, a symbolic link out of the folder, a refused or unreadable policy file, missing or refused credentials, a refused upload, a transport error, an invalid answer, a failed scan (`not_judged`), the deadline, files that are not the expected commit (`martian ci`), or an export that could not be written |

The gate is decided on the server and the exit code is its `exit_code`. Each finding at or above the
file's threshold is blocking, waived in the file, waived or accepted in the console, already in the
baseline, or a hunt finding that never blocks; the coverage must meet the file's minimum. The exit
codes match `martian redteam run` and `martian ci`. `martian ci --scan FOLDER` runs the same command as
one test of a CI session ([SDK and CI](sdk-and-ci.md#cli-and-one-session-behavior)).

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
- **Excerpts.** Both exports quote the lines each finding is about: the SARIF shows them in code
  scanning, and the JSON export holds them with the receipts. Keep `scan.json` out of artifacts
  that others can download if the scanned files hold anything private.

This repository's CI runs the action twice against a stand-in for the backend's scan routes
(`tests/scan/fake_backend.py`, which reports one seeded finding on every `SKILL.md`) on the fixture
repository in [examples/scan/fixture](../examples/scan/fixture/): without the skill the gate passes,
with it the step fails with exit 1 and writes SARIF with one result. It does not reach a Martian
server.
