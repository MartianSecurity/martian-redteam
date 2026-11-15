# Trace store limits

How far `MlflowStore`, the store on `mlflow-skinny`'s SQL store in the same process
([the in-process binding](evals-core.md#the-in-process-binding)), holds traces on Postgres: write
latency, search latency and database growth as the number of traces, the write rate and the
size of a trace grow, and the volume at which something else is needed.

**Measured on one developer machine** that was running about ten other agents at the same time
(load average 13 to 27 on 12 threads while writing, 3 to 25 while searching). Read the numbers
as orders of magnitude and as the shape of each curve, not as a server benchmark. Anything
beyond 100,000 traces in one project, or beyond the rates in the tables, is extrapolated and
says so.

The binding measured here kept a trace's request and response whole in MLflow's trace metadata.
It now keeps them whole in the trace's root span instead, as MLflow tracing does
([how traces sit in MLflow](evals-core.md#the-in-process-binding)), which lifts the 8,000-character
limit Postgres put on them. That layout has not been load-tested; [trace size](#trace-size) says
what is known.

## Setup

| | |
| --- | --- |
| Store | `MlflowStore`, `mlflow-skinny` 3.18.0, `martian-sdk` 0.1.3, Python 3.12.14, MLflow's default connection pool (5 connections and 10 overflow per process) |
| Database | Postgres 17.12, the `postgres:17` image with default settings (`shared_buffers` 128 MB, `work_mem` 4 MB), in Docker on the native engine with `--cpus 2 --memory 1g`, data on a Docker volume on NVMe |
| Machine | AMD Ryzen 5 3600 (6 cores, 12 threads), 31 GB, Linux 7.0 |
| Traces | Synthetic, through `record_trace`, in one project of one organisation: 5 spans; 1,000 characters of English-like text in the request, the response and each span's input and output; sessions of 5 turns, 1,000 users, 20 values of a `route` tag, every 50th trace an error. No model calls |
| Writers | 4 writer processes with 4 concurrent writes each, unless a table says otherwise; searches one at a time from a fifth process |
| Runs | The search tables come from a run of the binding that pages through MLflow, on a fresh database of the same setup. The write, write-rate and trace-size tables come from a run of the binding before it, which wrote traces the same way |

## Results

### Writes as the project grows

Each row covers the traces written since the row before.

| Traces in the project | Write p50 (ms) | p95 | p99 | Traces/s | Postgres CPU per trace (ms) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 142 | 176 | 222 | 110 | 6.9 |
| 10,000 | 127 | 162 | 320 | 121 | 6.2 |
| 25,000 | 154 | 275 | 357 | 92 | 7.0 |
| 50,000 | 155 | 227 | 336 | 97 | 7.0 |
| 75,000 | 196 | 335 | 450 | 74 | 7.6 |
| 100,000 | 235 | 440 | 613 | 61 | 7.8 |

Throughput fell from about 110 to 61 traces a second while the machine's load average doubled
and the database outgrew the container's 1 GB of memory (at about 60,000 traces). This run does
not separate the two. Postgres's own CPU per trace rose only from about 6 to 8 ms.

### Write rate

Offered for 60 seconds each, with the project at 100,000 to 119,000 traces and at most 32 writes
in flight. Latency counts from when a trace was due, so it includes waiting.

| Offered/s | Written/s | Shed | p50 (ms) | p95 | p99 | Postgres CPU per trace (ms) | Postgres cores busy |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 10.0 | 0 | 124 | 178 | 205 | 7.6 | 0.08 |
| 25 | 25.0 | 0 | 132 | 202 | 267 | 7.6 | 0.19 |
| 50 | 49.9 | 4 | 109 | 219 | 321 | 7.7 | 0.38 |
| 100 | 79.0 | 1,247 | 353 | 491 | 797 | 8.9 | 0.70 |
| 150 | 72.2 | 4,612 | 405 | 585 | 947 | 8.2 | 0.59 |
| 200 | 77.9 | 7,295 | 384 | 565 | 770 | 7.9 | 0.61 |

The four writer processes saturate at 72 to 79 traces a second while Postgres uses at most 0.7
of its 2 cores: on this busy machine the limit is the writers' Python, not the database. One
writer in one process writes a 5-span trace in about 41 ms (the size table below), about 25 a
second. In shorter runs, 4 or 8 concurrent writes in one process reached only 19 to 23 traces a
second, at a p50 of 170 to 400 ms.

### Searches as the project grows

Seconds, median of three runs, each measured right after the writes that brought the project to
its size; one run where the first took more than 30 seconds (text across the whole project at
75,000 and 100,000 traces).

| Traces | Traces page | MLflow's first page | Last page | One session | One user | One tag value | Newest 1,000 | Text, one call | Text, whole project | Sessions page | One trace | Memory of the searching process |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.024 | 0.019 | 0.025 | 0.012 | 0.013 | 0.024 | 0.022 | 0.15 | 0.12 | 0.14 | 0.016 | 0.2 GB |
| 5,000 | 0.020 | 0.028 | 0.034 | 0.012 | 0.018 | 0.026 | 0.020 | 0.14 | 0.91 | 0.15 | 0.014 | 0.2 GB |
| 10,000 | 0.016 | 0.013 | 0.059 | 0.008 | 0.024 | 0.025 | 0.018 | 0.17 | 1.9 | 0.14 | 0.011 | 0.2 GB |
| 25,000 | 0.021 | 0.020 | 0.13 | 0.017 | 0.040 | 0.058 | 0.021 | 0.19 | 5.7 | 0.18 | 0.016 | 0.3 GB |
| 50,000 | 0.011 | 0.012 | 0.15 | 0.008 | 0.047 | 0.036 | 0.011 | 0.13 | 13 | 0.16 | 0.013 | 0.3 GB |
| 75,000 | 0.017 | 0.021 | 0.55 | 0.014 | 0.082 | 0.042 | 0.018 | 0.17 | 39 | 0.15 | 0.021 | 0.3 GB |
| 100,000 | 0.034 | 0.018 | 1.1 | 0.047 | 0.28 | 0.075 | 0.039 | 0.32 | 62 | 0.25 | 0.046 | 0.3 GB |

- **Traces page**: `search_traces` with no filters, first 50. **MLflow's first page**: MLflow's
  own `search_traces` for the same 50 from the same SQL store, what the database alone costs.
  **Last page**: the oldest 50, from a cursor at the project's last 50.
- **One session** matches 5 traces, **one user** 1 in 1,000, **one tag value** 1 in 20 (5,000 at
  100,000 traces), **newest 1,000** is a `since` search over the last 1,000 traces, which is what
  a sampler polling for new traffic asks.
- **Text, one call** looks for text that one trace holds: the call reads the newest 1,000 traces
  and, in a project larger than that, usually returns none with a cursor that reads on. **Text,
  whole project** follows that cursor until it reaches the oldest trace, which holds the text.
- **Sessions page** is `search_sessions` with no filters: the first 1,000 traces, 200 sessions.
  **One trace** is `get_trace` with its spans.
- Memory is the searching process's peak, which only grows.

### Before and after, at 100,000 traces

The same searches on the same 100,000 traces, minutes apart, from a process that only searched
(load average 13 to 20): the binding before this one, which read every trace its MLflow filters
matched and filtered, sorted and paged in the process, and the binding that pages through MLflow.
Seconds, median of three runs, or one run where the first took more than 30 seconds.

| Search | Reading every trace | Paging through MLflow |
| --- | ---: | ---: |
| Traces page | 56 | 0.020 |
| Last page | 55 | 0.36 |
| One session | 0.015 | 0.011 |
| One user | 0.105 | 0.081 |
| One tag value | 2.0 | 0.055 |
| Newest 1,000 | 0.18 | 0.022 |
| Text, one call | 55 | 0.20 |
| Text, whole project | 54 | 54 |
| Sessions page | 49 | 0.16 |
| One trace | 0.017 | 0.019 |
| Memory of the searching process | 1.1 GB | 0.24 GB |

With the earlier binding the Traces page grew from 0.16 seconds at 1,000 traces to 2 at 10,000
and about a minute at 100,000, the Sessions page with it, and the searching process held about
37 MB per 1,000 traces it read.

### Database growth

| Traces | Database | Per trace |
| ---: | ---: | ---: |
| 1,000 | 21 MB | 20.9 KB |
| 10,000 | 180 MB | 18.0 KB |
| 50,000 | 837 MB | 16.7 KB |
| 100,000 | 1,674 MB | 16.7 KB |

At 100,000 traces: `spans` 926 MB, `trace_request_metadata` 346 MB, `trace_info` 274 MB,
`trace_tags` 126 MB, each with its indexes and TOAST. The request and the response were each kept
twice, as MLflow's previews and whole in trace metadata. The binding now keeps them whole in the
trace's root span, so that share moves from `trace_request_metadata` to `spans`, and every trace
has one span more than it was given.

### Trace size

One writer, 200 traces of each shape, with the project at about 119,000 traces. Characters are
those of the request, the response and each span's input and output.

| Spans | Characters | Write p50 (ms) | p95 | Read p50 (ms) | Database per trace |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 200 | 27 | 40 | 12 | 2.3 KB |
| 5 | 1,000 | 41 | 50 | 14 | 20 KB |
| 20 | 1,000 | 66 | 93 | 20 | 47 KB |
| 50 | 1,000 | 79 | 128 | 19 | 98 KB |
| 5 | 4,000 | 54 | 116 | 15 | 23 KB |
| 5 | 7,900 | 86 | 148 | 17 | 37 KB |

Postgres compresses rows over about 2 KB, every span here among them, and the synthetic text
comes from a small vocabulary, so it compresses better than real traffic would. Read every
database size in this report as a lower bound, most of all for the larger fields.

In the measured layout a request or a response over 8,000 characters was refused on Postgres.
The binding now stores a trace whose request and response are 100,000 characters each on SQLite
and Postgres (`tests/evals/test_store_mlflow.py`), and keeps only MLflow's 1,000-character
previews of them in `trace_info`. Its write and read times and its size are not measured here.

## What limits what

- **A page reads only its own traces.** The binding pages through MLflow's own search with every
  filter MLflow has (run, session, user, tags, ids, states, time), so the Traces page and each
  filtered page cost what MLflow's own first page costs: 10 to 80 ms at every size up to 100,000
  traces, one user's page up to 0.3 seconds. The Sessions page reads its 1,000 traces in 0.14
  to 0.25 seconds at every size, and the searching process stays at about 0.3 GB.
- **A later page costs more than the first.** MLflow pages by offset, so the database passes
  over every trace before the page: the last page of 100,000 traces took 0.36 to 1.1 seconds,
  about 4 to 11 µs per trace before it. A cursor on the time of the last trace read would keep
  later pages as cheap as the first; MLflow's page tokens are offsets.
- **Free text is filtered in the process, 1,000 traces a call.** MLflow's SQL store cannot filter
  a trace's request and response text, so a text search reads 1,000 traces a call and returns
  what matched among them with a cursor: 0.13 to 0.32 seconds a call at every size. Searching a
  whole project still reads all of it, in calls, each paying the offset of the one before:
  1.9 seconds at 10,000 traces, 13 at 50,000 and about a minute at 100,000, as before, but no
  call holds more than 1,000 traces.
- **Writes are limited by Python CPU in the writing process**: about 20 to 25 traces a second
  per process, 50 a second sustained through four processes (p99 under 0.35 s) and 72 to 79 at
  saturation. Postgres spent 6 to 9 ms of CPU per 5-span trace.
- **A request or a response has no length limit of its own.** The binding keeps them whole in
  the trace's root span and shortens only MLflow's previews to 1,000 characters. A search returns
  the previews, so its text filter does not see past them. MLflow's own `trace.text` filter
  matches span content, where the whole text now is, but scans it without an index; the binding
  does not use it, and it is not measured here. A value of the trace's own metadata
  is still refused over 8,000 characters on Postgres (`varchar(8000)`), as `invalid`, naming
  `record_trace` and MLflow's `trace_request_metadata` table, never the value
  ([what store errors contain](security-and-data.md#what-store-errors-contain)).
- **Storage is linear**: about 17 KB per trace of 5 spans and 1,000-character fields, more than
  half of it spans; about 1.7 GB per 100,000 traces. The store interface has no way to delete old
  traces.

## Volume ceiling for monitoring

For monitoring production traffic (MorningHQ/martian-notes#196) on this store as it is:

- **Browsing does not limit a project's size up to 100,000 traces**: the Traces page, its filters
  and the Sessions page answer in under 0.3 seconds there, and the last pages in about a second.
  Beyond that, extrapolated: the first pages should stay flat and later pages grow by about 0.4
  to 1 second per 100,000 traces before them.
- **Free-text search reaches about 10,000 traces back in 2 seconds** of calls and 100,000 in
  about a minute, 1,000 traces a call. At one trace a second, 10,000 traces is under three hours
  of traffic: text search across a monitored project needs a narrower time window or a text index.
- **At most about 20 traces a second per writing process, and about 50 a second per
  installation** sustained, measured with four processes. More writer processes raise the second
  figure until Postgres limits it, below.
- **A sampler that reads new traces by time** (`since`) is not limited by project size in the
  measured range: a page of 50 of the newest 1,000 takes 11 to 39 ms, reading 1,000 traces about
  what the Sessions page takes, and reading one trace with its spans 11 to 80 ms. A
  [monitor](evals-core.md#monitors) reads so, at most 1,000 traces a pass, and reads only the
  traces it samples whole.
- **Requests and responses of any length**, at a write time and size per trace that this report
  has measured only up to 7,900 characters.
- **Plan for retention**: at 50 traces a second the store grows by about 4.3 million traces and
  about 70 GB a day (extrapolated from 17 KB a trace).

## When a different trace store is needed

The binding pages through MLflow, so what remains are the limits of the store itself, MLflow's
SQL store on Postgres: its offset paging and its lack of a text index above, and these,
extrapolated from the CPU and bytes measured per trace, not measured:

- **Ingest above a few hundred traces a second per database.** Postgres spent about 8 ms of CPU
  per trace, so one core takes about 125 traces a second and the 2 cores here about 250, before
  disk I/O, which was not measured.
- **Retained traces in the tens of millions per database**: about 170 GB per 10 million traces,
  with MLflow's indexes and Postgres's TOAST, and no partitioning by time.
- **Free-text search or analytics across a whole project** at any size much beyond 10,000 traces.
  MLflow's SQL store has no text index, so the binding filters text in the process and reads
  every trace on the way.

When monitoring needs any of these, it needs a trace store built for append-heavy, time-ordered
data with text search, rather than more Postgres.

## Re-running

`scripts/trace_store_load.py` writes into a schema of its own (`load_<random>`) and drops it at
the end unless given `--keep-schema`; it prints the tables above as Markdown and writes everything
it measured as JSON with `--output`. A Postgres for it, on the native Docker engine:

```bash
docker volume create trace-load-pgdata
docker run -d --name trace-load-pg --memory 1g --cpus 2 \
  -e POSTGRES_USER=load -e POSTGRES_PASSWORD=<password> -e POSTGRES_DB=load \
  -v trace-load-pgdata:/var/lib/postgresql/data -p 127.0.0.1:55432:5432 postgres:17
export MARTIAN_LOAD_DATABASE_URL=postgresql+psycopg://load:<password>@127.0.0.1:55432/load
```

Every phase, about 40 minutes; the search tables above are its growth phase alone
(`--phases growth`, about 20 minutes):

```bash
uv run --extra postgres python scripts/trace_store_load.py \
  --processes 4 --writers 4 --sizes 1000,5000,10000,25000,50000,75000,100000 \
  --spans 5 --payload 1000 --searches 3 \
  --rates 10,25,50,100,150,200 --rate-seconds 60 --in-flight 32 \
  --shapes 0x200,5x1000,20x1000,50x1000,5x4000,5x7900,5x8001 --shape-traces 200 \
  --database-cpu-stat /sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' trace-load-pg).scope/cpu.stat \
  --output trace-load.json
```

`--phases` picks among `growth`, `rate` and `size`; `--max-step-seconds` (600 by default) stops a
step that runs longer, and `--slow-search-seconds` (30) stops repeating a slow search.
`--database-cpu-stat` is optional and needs cgroup v2. A `sqlite:///` URL measures the local
edition's SQLite instead. Remove the container and the volume afterwards:

```bash
docker rm -f trace-load-pg && docker volume rm trace-load-pgdata
```
