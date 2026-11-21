# Local findings and remediation

Open **Red Team → Findings** in `martian ui` with the corresponding UI build. The inventory
shows findings from finished scans in the selected project, including runs saved by the CLI.
Re-judging a scan produces its own report and does not add a new scan observation.
Search by finding, target or method. Filter by target, severity, status, category,
vulnerability type, evidence or recurrence. A report's **Findings of this run** limits the list
to findings observed in that scan. The count and evidence totals above the list describe the
whole project. **Clear filters** retains the selected scan. Choose **Show all scans** to return
to the project-wide inventory.

## Track a finding

The inventory groups observations by recorded target identity, vulnerability, type and objective.
It keeps the original scan, method, evidence level and attempt link for each occurrence. New
function runs use a digest of the absolute path and function name. HTTP and HTTP MCP runs with
a known route use the saved target ID or a digest of the endpoint address and, for MCP, the tool
name. Runs using custom headers, query parameters, URL credentials, or stdio MCP stay scoped to
their own evidence. Older network runs without a recorded route and resumed runs whose endpoint
identity changed also stay scoped to their run.

For Promptfoo and Marsfoo results, assign the same target key at import to track the target
across scans. [Group archived scans](redteam-interop.md#group-archived-scans-for-the-same-target)

Use **Save triage** to add a comment, change the finding's status or override its severity.
Every change appends to the finding's history. Fixed, false positive, ignored and accepted are
review decisions. The original scan reports, evidence and gate results retain their recorded
values, including any annotation previously made on an individual scan.
If the result of a browser submission is uncertain, **Retry saved triage** sends the same
decision and returns the original entry if it was already saved.

A finding marked fixed shows **Seen after fix** when a later scan records another observation.
For an ordinary review, the cutoff is when the reviewer saved the decision. For a verified fix,
the cutoff is the start of the follow-up scan. If a subsequent scan is imported after that review,
the warning uses when the scan started. The recommended severity is the highest severity
recorded among its observations. **Restore scan severity** removes a reviewer's override.

**Export CSV** and **Export SARIF** download all findings matching the current filters across
pages. They include finding names, evidence labels and remediation guidance, and omit transcripts and reviewer
comments. Inventory SARIF uses stable finding fingerprints. Use the
[per-run SARIF export](redteam-reports.md#output-formats) with a run file path when a CI consumer
requires a source location.

### Export NeMo Guardrails policies

In **Export Colang**, choose **Colang 1.0** or **Colang 2.x**. The archive covers every
finding that matches the current filters. It includes a manifest of finding IDs, evidence
and triage states, and the rules mapped from confirmed findings with supported message
policies. Attempted and suspected findings are marked for confirmation. Ignored findings
and false positives remain dismissed. Findings that need application controls, such as
tenant isolation or tool authorization, are listed for application review.

A bundle with mapped rules includes `config.yml`, `martian_findings.co`, and integration
instructions. Review the policies and supply your application's model configuration.
The export runs offline. The model configured in NeMo receives the messages that its input
and output rails check. A selection without mapped rules produces the review manifest and
its instructions.

NeMo Guardrails 0.25 deprecates the legacy Colang engines. The Colang 1.0 YAML
also runs its input and output checks on NeMo's I/O engine. Colang 2.x hooks are for
an existing Colang 2.x application's `main` flow.
[NeMo Guardrails 0.25 migration guide](https://docs.nvidia.com/nemo/guardrails/latest/reference/0-25)

## Use Findings from the CLI

With `martian-sdk[evals]`, the CLI reads the same local findings, observations and triage as
the workbench. Use `--data-dir` or `MARTIAN_EVALS_HOME` to point to the folder with saved scans
and `--project-id` to scope the command to a project. The default folder is `~/.martian/evals`.

```bash
martian redteam findings list --status open --evidence confirmed
martian redteam findings list --format json --search help-desk
martian redteam findings show FINDING_ID
martian redteam findings triage FINDING_ID --status fixed --comment 'Reviewed with the service owner'
martian redteam findings list --status open --format sarif -o findings.sarif
```

`list` uses the workbench filters and sort order. Table and JSON output are paged using
`--offset` and `--limit`. Export with `--format csv`, `sarif`, `colang-v1`, or `colang-v2`
and `--output` to save every finding that matches the filters. Use a new path for the export.
`show` returns the observation references and private triage history as JSON.
`triage` returns the new history entry as JSON. Use `--severity` to override severity or
`--restore-severity` to return to the recorded value. Each change preserves the original scan evidence.

An MCP client can also read the selected project's finding summaries, review decisions, recorded
evidence references and remediation comparisons. [Connect an MCP client](sdk-and-ci.md#connect-an-mcp-client-to-local-reports)

## Review remediation for a scan

From a finished scan's report choose **Remediation**, or open the latest remediation report from
a finding. The report is built on demand from that scan's saved evidence. This works with local
and CLI scans, including incomplete scans with recorded evidence.

- The executive summary shows the highest recorded severity among confirmed findings, or
  **Unrated** when no confirmed finding has a severity. Attempted and suspected findings have
  separate counts. A partial scan or one with missing or unresolved attempts is marked **Incomplete scan**.
- The mapping links findings to their recorded evidence and recommended actions. Actions are
  ordered by evidence classification, recorded severity and evidence level, and include
  confirmed severity, a typical effort estimate, implementation and verification steps, and
  examples where available.
- **Export report → Download PDF** downloads the scan's evidence summary and remediation plan.
  **Print remediation plan** opens the plan in a standalone browser view. Finished version 2 runs
  write `redteam-remediation.json` and `redteam-remediation.html` with their standard output.
  [Export from the CLI](redteam-reports.md#download-a-scan-pdf)

The guidance is deterministic and works without a model or any outbound call. Review the examples
against your application.

### Draft a hardened system prompt

**Harden prompt** appears in the Remediation header when the scan confirms instruction hijacking,
indirect prompt injection or private instruction disclosure with a supported finding type. Each
suggested instruction links to the finding that prompted it. Paste the current system prompt,
choose the instructions to add and review the candidate before copying it. The draft keeps the
text you enter and appends the selected instructions that are missing. Closing the dialog clears
its draft. The UI keeps prompt text in the dialog and drafts it without a model call.

For a saved run and a UTF-8 system prompt file, the CLI can create the same candidate offline.

```bash
martian redteam harden-prompt redteam-run.json system-prompt.txt -o candidate-prompt.txt
```

The base SDK can draft from a saved scan file. With `martian-sdk[evals]`, the command also accepts
a run ID from the local store. Use `--project-id` for a run in another project and repeat
`--control` to select IDs from `prompt_controls` in `redteam-remediation.json`. With no selection,
the command uses every suggested instruction. Test the revised prompt with your application's
ordinary requests and use the recorded coverage comparison below to review a follow-up scan.
Apply the report's application controls for authorization, sensitive data and tool access.

### Verify a remediation

The report shows **Newer scan available** when an original scan of the same recorded target
started after this scan ended and has a recorded finish. **Open newer report** opens that
scan's own evidence and recommendations. **Check for newer scans** refreshes the list after
a run finishes in another window or through the CLI.

In the baseline report, choose **Compare scans** to check the suggested follow-up, or enter
a different **Follow-up run ID**. The SDK compares two saved scans in the same project using
their recorded target identity. It displays the result per baseline finding and links to
any recorded evidence of recurrence.

- **Still observed** means the original probe configuration is present and the follow-up recorded
  the same kind of finding. The result names it when the follow-up evidence came from another probe.
- **Not observed** means both scans completed with matching probe definitions and recorded
  settings, every planned attempt lined up by its configuration fingerprint, and the follow-up
  resisted each attempt. A configured benign task must also complete on every attempt.
- **Inconclusive** means the evidence is incomplete or the probe or settings changed. The
  result shows the reason and the number of planned attempts that reached a verdict.

The comparison also shows the highest recorded confirmed severity and evidence counts for
each scan. **New in follow-up** marks a finding absent from the baseline's saved findings.
**Newly confirmed** marks a previously unconfirmed finding. **Higher severity** means the
follow-up's confirmed severity exceeds the baseline's highest assigned confirmed severity for
that finding. These classifications use the inventory's vulnerability, type and objective
grouping for the same target. Open the follow-up evidence from the comparison.

**Limited comparison coverage** appears when a scan is incomplete, the settings or probes differ,
or recorded attempts lack matching verdict coverage. A full comparison requires a decided attempt
in every planned slot on both sides, matching fingerprints and a successful configured benign
task for each resisted attempt. You can still review the recorded findings from either scan.

Older scans can lack a stable target identity or attempt fingerprints. A re-judged report
uses previously recorded observations. Cross-scan verification needs original scans with a
recorded target identity. A saved target ID alone cannot establish a network route when headers,
query parameters or credentials may select a different service. A result applies to the recorded
probes and attempts.

Choose **Record verified fix** on a **Not observed** row to save the decision in the finding's
triage history. The SDK checks the saved scans again when you record the fix. Use the latest
scan that observed the finding as the baseline, and choose a follow-up that covers all the
finding's recorded probes. The SDK rejects the decision when the inventory shows another
observation during or after the follow-up. The history links both scans and records the matched attempt count.
You can reopen the comparison from **View saved verification**.

```bash
martian redteam verify-remediation BASELINE FOLLOW_UP -o verification
```

Each argument is a saved `redteam-run.json` file or a local run ID. Use `--project-id`
when looking up IDs in another local project. The command writes
`redteam-remediation-verification.json` when `-o` is set. It exits 0 when every baseline
finding is **Not observed**, 1 when a finding recurs and every result is conclusive, or 2
when evidence is inconclusive, either scan is ineligible, or the baseline has no findings.
Verification reads both scans offline and preserves their recorded verdicts and triage.

Use `--fail-on-regression` to gate the whole comparison, including a baseline with zero findings.
With matched, conclusive coverage, the command exits 1 for a recurring finding, a new confirmed
finding, a newly confirmed finding or an increase in recorded confirmed severity. It exits 2 when
comparison coverage is limited or any baseline result is inconclusive. A conclusive comparison
without those changes exits 0. Both modes write the same comparison JSON.

To record a verified fix from the terminal, use local run IDs and the finding ID from the
baseline scan's comparison results.

```bash
martian redteam findings record-fix BASELINE_RUN_ID FOLLOW_UP_RUN_ID SCAN_FINDING_ID
```

The command prints the inventory finding ID and saved triage entry as JSON.
Use the same `--request-id` when retrying a request whose outcome is uncertain.

## Move review history

Use a triage archive to move comments, status and severity decisions, and verified fixes between
local SDK installations. Each entry keeps its original author, timestamp and review ID. The
archive contains review history and saved observation references. Keep the matching scan records
with your local data backup.

```bash
martian redteam findings export-triage --data-dir old-data -o reviews.json
martian redteam findings import-triage reviews.json --data-dir new-data --dry-run
martian redteam findings import-triage reviews.json --data-dir new-data
```

Export uses the `default` project unless you pass `--project-id`. Use `--finding-id` to export the
reviews for a single finding. Import uses the project named in the archive and requires matching
saved observations in that project. First restore the evidence, for example by [pushing the saved
scans to the local server](redteam-api.md#pushed-runs) or reimporting [the original Promptfoo or
Marsfoo exports](redteam-interop.md#group-archived-scans-for-the-same-target) with the same target key.
Verified fixes also require both scans from the original comparison. The importer rechecks their
coverage and preserves any subsequent observations that the destination already recorded.

Dry runs check the archive and report the number of reviews to add or reuse. Import appends new
reviews in one transaction and reuses matching review IDs. An ID with different content causes
the import to fail. Use an archive you trust and handle it as private because it includes review
comments. The checksum detects changes to the archive's contents.
