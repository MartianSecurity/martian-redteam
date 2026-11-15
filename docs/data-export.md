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

- its registered scorers, every version; its review questions;
- its dataset versions with their cases, in case order;
- its suites, every version; its dataset drafts with their cases; its saved views;
- its runs as the record store holds them and as the store holds them, each with its case
  results, scores, scoring jobs, every review of every chain (replaced ones too), the review
  queues its reviews name with each item's status, and its artifacts;
- its traces, oldest first, each with its spans and its scores: judges' and checks' verdicts and
  people's annotations.

It leaves out what is not durable data, what the interfaces cannot list or the model does not
describe, and what the two stores do not hold:

- The worker's queue (the record store's `martian_work_queue` table): the runs and scoring jobs no
  worker has finished. It is transient; a run exported queued or running is finished as
  interrupted in the fresh install (see Exporting and resuming).
- A review queue that no review names (the store cannot list queues; creating one through the
  core adds a pending review per case and question), and a run in the store that the record store
  does not hold.
- What MLflow keeps beyond the model: tags and metadata keys starting `mlflow.`, MLflow runs'
  parameters and metrics (a run's counts are logged again on import), and MLflow's own form of a
  scorer registered outside Martian (its name and version are kept).
- CI gate sessions, which stay with the server's CI gate.
- Monitors, their progress and their record of the traces they took
  ([monitors](evals-core.md#monitors)), and the thresholds on their scores with their status and
  alerts ([thresholds](evals-core.md#thresholds-and-alerts)); exporting them is not built yet. The
  scores monitors stored are on their traces, which are exported. A threshold's webhook secret is
  never exported.
- Schedules, their progress and their history of slots ([schedules](evals-core.md#schedules)), and
  the thresholds on their gate results; exporting them is not built yet. The runs they started,
  and their scoring jobs, are exported.
- The local edition's files beside the stores: its targets and their keys (`targets.json`,
  `target-keys.json`) and its settings (`settings.json`, the default judge model). An export holds
  no credentials; define the targets and the default judge model again in the fresh data folder.

`martian.evals.data_export` names what it covers: `EXPORTED_TABLES` maps each table of the record
store it carries to its file and `EXPORTED_KINDS` each kind of object (model class) the store
and the record store take or return to its files; `LEFT_OUT_TABLES` and `LEFT_OUT_KINDS` give
the rest, each with why. Every table but the worker's queue is exported, and every kind of
object; the model classes left out are not data of their own (the store's status, a trace search
and the sessions it groups, a page of a listing, a run's counts). A table or a kind added to
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
| `suites.jsonl` | Version of a suite, oldest first | `project_id`, `suite`: `Suite` |
| `drafts.jsonl` | Dataset draft | `project_id`, `draft`: `DatasetDraft` |
| `draft_cases.jsonl` | Case of a draft, in order; consecutive per draft, in the order of `drafts.jsonl` | `project_id`, `draft_id`, `case`: `Case` |
| `views.jsonl` | Saved trace view | `project_id`, `view`: `SavedView` |
| `runs.jsonl` | Run, newest first | `project_id`, `run`: `Run` as the record store holds it, `store_run`: `Run` as the store holds it, or `null` for a run that never started there |
| `case_results.jsonl` | Case result, in case order per run | `project_id`, `result`: `CaseResult` |
| `scores.jsonl` | A run's score in the record store | `project_id`, `score`: `Score` |
| `scoring_jobs.jsonl` | Scoring job | `project_id`, `job`: `ScoringJob` |
| `reviews.jsonl` | Review, each after the review it replaces | `project_id`, `review`: `Review` |
| `queues.jsonl` | Review queue | `project_id`, `queue`: `ReviewQueue`, `items`: a list of `QueueItem`, one per case with its trace and status |
| `artifacts.jsonl` | Run artifact | `project_id`, `run_id`, `name` (a relative path), `sha256`, `size`; the bytes are `artifacts/<sha256>` |
| `traces.jsonl` | Trace, oldest first | `project_id`, `trace`: `Trace` with its `spans` and `scores`, read one at a time, so its `request` and `response` are whole and not a search's previews |

`manifest.json`:

```json
{
  "format": "martian-evals-export",
  "version": 1,
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
`null` for the single local user. `version` is the format's version (`VERSION`, here `1`): a new
file, or a change to a file or a line's shape, is a new version, and an import reads only its own.

## Exporting and resuming

An export runs in steps, in a fixed order: for each project, its definitions (the project, its
scorers, questions, suites, drafts and views), then each dataset version, each run, and each page
of 500 traces. Before the first step it lists the projects, their dataset versions and their runs
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
trace without its run; export again into a new folder.

A run still queued or running is exported as it is, state and all: the export records what the
install holds. Nothing is working on it in the fresh install, so the first time the local edition
opens that data folder (`martian ui`) it finishes the run as `failed` with the interrupted error,
as it does for any run no process is working on. An import does not refuse the export for it.

## Importing

An import goes into a fresh install: one whose projects hold no runs, datasets, traces, suites,
drafts, views, scorers or review questions for the principal. A project the install already has
under the same id, such as the local edition's empty default project, is used as it is; one with
another name is refused.

Before anything is written, the import checks the manifest's format and version and each file's
size, line count and SHA-256, and each artifact's SHA-256. It then writes every object through the
interfaces, creating what an object refers to before the object: projects, scorers, questions,
dataset versions, suites, runs (in the store, then in the record store), traces with their scores,
case results, scores, scoring jobs, reviews, queues, artifacts, drafts and views; projects that
were archived are archived last. A dataset version that does not come back with its digest,
version and case count, or a suite or scorer version given another version number, stops the
import. An import that stops leaves what it
wrote; import into a fresh install again.

The receiving store assigns its own identifiers and bookkeeping for what it creates; the import
points every reference at them, and `references` in its result maps each source identifier to the
receiving store's:

- `store` references (`StoreRef`): MLflow's experiment, run and assessment ids, and dataset ids.
  A reference the source store could not resolve either is kept as it was.
- A dataset version's `id`, `created_at` and `created_by` (the importing user). Drafts'
  `source_dataset_id` and runs' `store.dataset_id` follow the new id.
- A project's `created_at`, where the store sets it (the MLflow binding: the experiment's creation
  time), and `archived_at`, the time of the import.
- A dataset draft's `revision`, `updated_at` and `updated_by`, and a saved view's `revision` and
  `updated_at`.

Every other field of every object comes back as it was exported. The one change comes after the
import, not in it: a run exported queued or running is finished as interrupted when the local
edition opens the folder (see Exporting and resuming).

`tests/evals/test_data_export.py` fills an organisation with every kind of object, exports it,
imports it into a fresh install and exports that again, and compares the two exports field by
field with only the fields above set aside: on the in-memory store, on the local edition's data
folder (with Inspect AI logs imported into it, and with the Evals UI's routes answering the same
for every run and review, but for the store's own ids and the moment a run exported queued was
finished as interrupted), and on a data folder that `martian evals import mlflow` filled. On the
first two it also checks that every table the export carries holds rows in the source and as many
in the fresh install. It checks that every table of the record store and every kind of object of
the two interfaces is exported or left out with a reason. It interrupts an export inside a run's
step and on a page of traces, resumes it and compares the files byte for byte with an
uninterrupted export, and covers what an export and an import refuse and both commands.
