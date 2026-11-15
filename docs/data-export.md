# Data export

An organisation can take all of its evals data out, and load it into a fresh install with nothing
lost. `martian.evals.data_export` does both through the evals core's two store interfaces,
`EvalStore` and `RecordStore` ([evals core](evals-core.md)), so it works on every binding: the
local edition's data folder, and any install that runs the core in its own process.

```bash
martian evals export ./evals-export                          # the data folder, as for `martian ui`
martian evals export ./evals-export --data-dir ./evals-data  # run it again to resume an interrupted export
martian evals import martian ./evals-export --data-dir ./new-data
```

`--data-dir` is the data folder, else `MARTIAN_EVALS_HOME`, else `~/.martian/evals`, as for
`martian ui`. Both commands work as the single local user. `martian evals import martian` sits
beside the other importers (`import inspect`, `import mlflow`): it reads the format below. A
problem stops either command with exit code `2` and says why; an interrupted export exits with
`130`.

An install that runs the core in its own process calls the same functions with its own stores and
the organisation's principal:

```python
from pathlib import Path

from martian.evals.data_export import export_data, import_data

exported = await export_data(store, records, principal, Path("evals-export"))
imported = await import_data(new_store, new_records, principal, Path("evals-export"))
imported.references["dataset_id"]  # each source dataset version id and the receiving store's
```

## What an export holds

Everything the store and the record store hold for the principal's organisation (or the single
local user), and nothing of another organisation's: every project the store lists, and in each

- its registered scorers, every version, a judge graded by a
  [panel of judges](evals.md#a-panel-of-judges) with its members; its review questions;
- its dataset versions with their cases, in case order, and its dataset publications: each
  manifest that pins the version publishing wrote, and any publication that never finished;
- its suites, every version; its dataset drafts with their cases; its saved views;
- its runs as the record store holds them and as the store holds them, each with its case
  results, scores, scoring jobs, every review of every chain (replaced ones too), the review
  queues its reviews name with each item's status, and its artifacts;
- its traces, oldest first, each with its spans and its scores: judges' and checks' verdicts and
  people's annotations;
- its [monitors](evals-core.md#monitors) with their progress and every trace each took for
  scoring, its [schedules](evals-core.md#schedules) with their progress and every slot each ran or
  skipped, and the [thresholds](evals-core.md#thresholds-and-alerts) on either with their status
  and every alert, delivered or not;
- its judges' [calibrations](evals.md#calibration), every one with the labelled items it rests
  on, and their re-checks, finished or not.

It leaves out what is not durable data, what the interfaces cannot list or the model does not
describe, and what the two stores do not hold:

- The worker's queue (the record store's `martian_work_queue` table): the runs and scoring jobs no
  worker has finished, and the next turns of monitors and schedules. It is transient; a run
  exported queued or running is finished as interrupted in the fresh install (see Exporting and
  resuming), and a monitor or a schedule comes back paused (see Importing).
- The outbox (the record store's `martian_deliveries` table, `Delivery`): store writes on their
  way from the record store to the store. It is transient. A delivered write is in the store and
  exported from there. A pending one is not carried: the score it carries is exported with the
  record store's scores, but the trace or review queue item it was to change is exported as the
  store holds it. Let the outbox deliver before exporting; it retries while the local edition
  serves (`martian ui`) or the server's outbox loop runs.
- A review queue that no review names (the store cannot list queues; creating one through the
  core adds a pending review per case and question), and a run in the store that the record store
  does not hold.
- What MLflow keeps beyond the model: tags and metadata keys starting `mlflow.`, MLflow runs'
  parameters and metrics (a run's counts are logged again on import), and MLflow's own form of a
  scorer registered outside Martian (its name and version are kept).
- CI gate sessions, which stay with the server's CI gate.
- A threshold's webhook secret. The record store keeps it apart from the threshold
  (`martian_thresholds.secret`) and the export never reads it, so a threshold with a webhook comes
  back without it and its secret is given again (see Importing). The SMTP server's login and the
  rest of the worker's alert settings are not in the stores at all: they are the installation's
  configuration, given again where the fresh install's worker runs.
- The local edition's files beside the stores: its targets and their keys (`targets.json`,
  `target-keys.json`) and its settings (`settings.json`, the default judge model). An export holds
  no credentials; define the targets and the default judge model again in the fresh data folder.

`martian.evals.data_export` names what it covers: `EXPORTED_TABLES` maps each table of the record
store it carries to its file and `EXPORTED_KINDS` each kind of object (model class) the store
and the record store take or return to its files; `LEFT_OUT_TABLES` and `LEFT_OUT_KINDS` give
the rest, each with why. Every table but the worker's queue and the outbox is exported, and every
kind of object but the outbox's deliveries; the other model classes left out are not data of
their own (the store's status, a trace search and the sessions it groups, a page of a listing, a
run's counts). The webhook secret is a column, not an object, and no file carries it. The judges'
agreement with people and the reviewers' agreement with each other are computed from the runs,
scores, reviews and traces when they are read, so they are not stored and come back with those. A
table or a kind added to
either store fails `tests/evals/test_data_export.py` until the export carries it, in a new file
of a new format version, or names it as left out and why.

## The format

An export is a folder:

```text
evals-export/
  manifest.json          written last: the export is complete once it is there
  projects.jsonl ...     one file per kind of object, below
  artifacts/<sha256>     each run artifact's bytes, named by their SHA-256
```

Each `.jsonl` file is [JSON Lines](https://jsonlines.org/): UTF-8, one JSON object per line, each
line ending in `\n`. Every line has `project_id`, the keys that place the object, and the object
itself in the [shared eval model](eval-model.md)'s JSON (`model_dump(mode="json")` of the named
class, with its `store` references as the source store had them). Every file is present, empty
when there is nothing of its kind. An import reads them in this order:

| File | One line per | Line |
| --- | --- | --- |
| `projects.jsonl` | Project | `project_id`, `project`: `Project` |
| `scorers.jsonl` | Version of a registered scorer, oldest first | `project_id`, `scorer`: `ScorerVersion` |
| `questions.jsonl` | Review question | `project_id`, `question`: `ReviewQuestion` |
| `datasets.jsonl` | Dataset version, by name then version | `project_id`, `dataset`: `DatasetVersion` |
| `cases.jsonl` | Case of a dataset version, in order; a version's cases are consecutive, in the order of `datasets.jsonl` | `project_id`, `dataset_id`, `case`: `Case` |
| `manifests.jsonl` | Dataset publication, newest first | `project_id`, `manifest`: `DatasetManifest`, with the `dataset_id` of the version it pins once `published` |
| `suites.jsonl` | Version of a suite, oldest first | `project_id`, `suite`: `Suite` |
| `drafts.jsonl` | Dataset draft | `project_id`, `draft`: `DatasetDraft` |
| `draft_cases.jsonl` | Case of a draft, in order; consecutive per draft, in the order of `drafts.jsonl` | `project_id`, `draft_id`, `case`: `Case` |
| `views.jsonl` | Saved trace view | `project_id`, `view`: `SavedView` |
| `runs.jsonl` | Run, newest first | `project_id`, `run`: `Run` as the record store holds it, `store_run`: `Run` as the store holds it, or `null` for a run that never started there |
| `case_results.jsonl` | Case result, in case order per run | `project_id`, `result`: `CaseResult` |
| `scores.jsonl` | A run's score in the record store: one per case, scorer, scoring job and source, so a person's answer named like a check or a judge is a line beside that scorer's | `project_id`, `score`: `Score` |
| `scoring_jobs.jsonl` | Scoring job | `project_id`, `job`: `ScoringJob` |
| `reviews.jsonl` | Review, each after the review it replaces | `project_id`, `review`: `Review` |
| `queues.jsonl` | Review queue | `project_id`, `queue`: `ReviewQueue`, `items`: a list of `QueueItem`, one per case with its trace and status |
| `artifacts.jsonl` | Run artifact | `project_id`, `run_id`, `name` (a relative path), `sha256`, `size`; the bytes are `artifacts/<sha256>` |
| `traces.jsonl` | Trace, oldest first | `project_id`, `trace`: `Trace` with its `spans` and `scores`, read one at a time, so its `request` and `response` are whole and not a search's previews |
| `monitors.jsonl` | Monitor, by name | `project_id`, `monitor`: `Monitor`, `progress`: `MonitorProgress` |
| `monitored_traces.jsonl` | Trace a monitor took, newest first; consecutive per monitor, in the order of `monitors.jsonl` | `project_id`, `trace`: `MonitoredTrace`, with the scores it stored on the trace |
| `schedules.jsonl` | Schedule, by name | `project_id`, `schedule`: `Schedule`, `progress`: `ScheduleProgress` |
| `scheduled_runs.jsonl` | Slot a schedule ran or skipped, latest first; consecutive per schedule, in the order of `schedules.jsonl` | `project_id`, `scheduled_run`: `ScheduledRun` |
| `thresholds.jsonl` | Threshold, by name | `project_id`, `threshold`: `Threshold`, `status`: `ThresholdStatus`; never the webhook's secret |
| `alerts.jsonl` | Alert, newest first; consecutive per threshold, in the order of `thresholds.jsonl` | `project_id`, `alert`: `Alert` |
| `calibrations.jsonl` | Judge calibration, newest first | `project_id`, `calibration`: `JudgeCalibration`, with its labelled `items` (each a case result's `run_id` and `case_id`, or a trace's `trace_id`), its `report`, who saved it and when |
| `rechecks.jsonl` | Re-check of a calibration, newest first | `project_id`, `recheck`: `CalibrationRecheck`, with its pinned `scorer` and the calibration it saved, `result_id` |

`Monitor`, `Schedule`, `Threshold` and the rest of the monitoring kinds, and `JudgeCalibration`
and `CalibrationRecheck`, are the record store's own classes (`martian.evals.records`), not the
shared model's. A calibration's items name runs and traces by the ids the export keeps, so they
need no new references.

`manifest.json`:

```json
{
  "format": "martian-evals-export",
  "version": 4,
  "model_version": "martian-eval-model-v1",
  "sdk_version": "0.1.3",
  "organisation_id": null,
  "started_at": "2026-11-15T09:30:00.120000+00:00",
  "completed_at": "2026-11-15T09:31:12.480000+00:00",
  "files": {"projects.jsonl": {"lines": 2, "bytes": 512, "sha256": "..."}},
  "artifacts": {"files": 1, "bytes": 14}
}
```

`files` has an entry for every file in the table. `organisation_id` is the organisation's id, or
`null` for the single local user. `version` is the format's version (`VERSION`, here `4`): a new
file, or a change to a file or a line's shape, is a new version, and an import reads only its own.
Version 2 added the six monitoring files, version 3 added `manifests.jsonl`, and version 4 added
`calibrations.jsonl` and `rechecks.jsonl`. An export of an earlier version is refused: import it
with the SDK that wrote it, and export again with this one.

## Exporting and resuming

An export runs in steps, in a fixed order: for each project, its definitions (the project, its
scorers, questions, suites, drafts, dataset publications and views, and its judges' calibrations
and re-checks), then each dataset version, each run, its monitoring (monitors, schedules and
thresholds with their history), and each page of 500 traces. Monitoring comes before the traces,
so every trace a monitor took is among the traces exported. Before the first step it lists the
projects, their dataset versions and their runs
in `export-plan.json`. After each step it flushes the files and records in
`export-progress.json` how long each file is and where the next step starts.

An export that is interrupted, or stops on an error from a store, is resumed by exporting into the
same folder again: each file is cut back to the end of the last finished step and the export
carries on from the next. A resumed export writes the same files as one that was never
interrupted. Once every step is done the export writes `manifest.json` and removes the two progress
files. An export refuses a folder that holds a complete export, anything other than an export in
progress, or another organisation's export in progress.

An export reads the install as it runs. Take it when nothing is writing (no run in progress,
nobody editing) for an exact copy: what changes meanwhile may or may not be in it. A trace of a
run that started after the export did stops the export, since an import could not record that
trace without its run; export again into a new folder. So does a schedule's slot whose run started
after the export did: pause the project's schedules and export again into a new folder.

A run still queued or running is exported as it is, state and all: the export records what the
install holds. Nothing is working on it in the fresh install, so the first time the local edition
opens that data folder (`martian ui`) it finishes the run as `failed` with the interrupted error,
as it does for any run no process is working on. An import does not refuse the export for it.

## Importing

An import goes into a fresh install: one whose projects hold no runs, datasets, traces, suites,
drafts, dataset publications, views, scorers, review questions, monitors, schedules, thresholds,
judge calibrations or re-checks for the principal. A project the install already has
under the same id, such as the local edition's empty default project, is used as it is; one with
another name is refused.

Before anything is written, the import checks the manifest's format and version and each file's
size, line count and SHA-256, and each artifact's SHA-256. It then writes every object through the
interfaces, creating what an object refers to before the object: projects, scorers, questions,
dataset versions, dataset publications (a published one finished again as the receiving store's
version, so a run of that version is still checked against its manifest), suites, runs (in the
store, then in the record store), traces with their scores, case results, scores, scoring jobs,
reviews, queues, artifacts, drafts and views, then monitors with the traces they took, schedules
with their slots, thresholds with their status and alerts, and judge calibrations and their
re-checks, each as it was saved, who saved it and when included; projects that were archived are
archived last. A dataset version that does not come back with its digest,
version and case count, or a suite or scorer version given another version number, stops the
import. An import that stops leaves what it
wrote; import into a fresh install again.

The receiving store assigns its own identifiers and bookkeeping for what it creates; the import
points every reference at them, and `references` in its result maps each source identifier to the
receiving store's:

- `store` references (`StoreRef`): MLflow's experiment, run and assessment ids, and dataset ids.
  A reference the source store could not resolve either is kept as it was.
- A dataset version's `id`, `created_at` and `created_by` (the importing user). Drafts'
  `source_dataset_id`, runs' `store.dataset_id` and publications' `dataset_id` follow the new id.
- A dataset publication's `published_at`, the time of the import. Its `started_at` is kept, so a
  publication that never finished holds no runs back once its lease is over.
- A project's `created_at`, where the store sets it (the MLflow binding: the experiment's creation
  time), and `archived_at`, the time of the import.
- A dataset draft's `revision`, `updated_at` and `updated_by`, and a saved view's `revision` and
  `updated_at`.
- A monitor's and a schedule's `revision` and `updated_at`. A schedule's `dataset_id` follows the
  dataset version's new id.

Four changes are the import's own:

- **Monitors and schedules come back paused** (`state`), whatever they were, and nothing is put in
  the worker's queue for them: an import scores no trace, runs no suite and sends no alert.
  Resuming one (`POST /v1/evals/monitors/{id}/resume`, `/schedules/{id}/resume`) starts it as
  resuming after a pause does: a monitor scores the traces recorded from then on, a schedule runs
  the slots from then on, and an alert still pending in the export is tried again at the next
  turn of its threshold's monitor or schedule. `ImportSummary.paused` names those that were active.
- **A threshold with a webhook comes back without its secret**, so its webhook alerts fail ("The
  threshold has no webhook secret") until it is given one: `POST
  /v1/evals/thresholds/{id}/secret` with `{"secret": "..."}`, once, for a threshold that has none
  ([thresholds](evals-core.md#thresholds-and-alerts)). Until then it shows `"signed": false`.
  `ImportSummary.needs_secret` names them, and `martian evals import martian` says how many.
  Its email recipients come back with it; the SMTP server is the fresh install's worker's.
- **A re-check exported queued or running comes back `failed`**, with the error
  `RECHECK_INTERRUPTED` and the time of the import as its `finished_at`. Nothing works on it in
  the fresh install, and a re-check left queued would refuse every new re-check of its judge
  version ("already queued or running"); ask for it again
  (`POST /v1/evals/judge-rechecks`, [calibration](evals.md#calibration)).
- A run exported queued or running is finished as interrupted when the local edition opens the
  folder (see Exporting and resuming); that comes after the import, not in it.

Every other field of every object comes back as it was exported.

`tests/evals/test_data_export.py` fills an organisation with every kind of object, a person's
answer named like a run's judge among its scores, exports it,
imports it into a fresh install and exports that again, and compares the two exports field by
field with only the fields above set aside (a monitor's and a schedule's `state` too, which it
checks went from active to paused, and an unfinished re-check's state, error and `finished_at`,
which it checks the import failed): on the in-memory store, on the local edition's data
folder (with Inspect AI logs imported into it, and with the Evals UI's routes answering the same
for every run and review, but for the store's own ids and the moment a run exported queued was
finished as interrupted), and on a data folder that `martian evals import mlflow` filled. On the
first two it also checks that every table the export carries holds rows in the source and as many
in the fresh install, that the outbox's deliveries stay behind, that the import queued nothing,
and that no file of the export holds the threshold's webhook secret (a canary), which the fresh
install does not have until it is given again, once; on the in-memory store, that the published
version is still pinned by its manifest in the fresh install, and that the person's answer named
like the judge comes back beside the judge's score in both exports. It checks that every table of the
record store and every kind of object of the two interfaces is exported or left out with a reason.
It interrupts an export inside a run's step, inside the monitoring step and on a page of traces,
resumes it and compares the files byte for byte with an uninterrupted export, and covers what an
export and an import refuse (a slot whose run started after the export did among them) and both
commands.
