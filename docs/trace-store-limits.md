# Trace store limits

How far `MlflowStore`, the store on `mlflow-skinny`'s SQL store in the same process
([the in-process binding](evals-core.md#the-in-process-binding)), holds traces on Postgres: write
latency, search latency and database growth as the number of traces, the write rate and the
size of a trace grow, and the volume at which something else is needed.

**Measured on one developer machine** that was running about ten other agents and their test
runs at the same time (load average 7 to 33 on 12 threads). Read the numbers as orders of
magnitude and as the shape of each curve, not as a server benchmark. Anything beyond 100,000
traces in one project, or beyond the rates in the tables, is extrapolated and says so.

The binding measured here is the one in this repository: it keeps a trace's request and response
whole in a root span it writes, as MLflow tracing does, and as MLflow's 1,000-character previews
in `trace_info` ([how traces sit in MLflow](evals-core.md#the-in-process-binding)), so every trace
has one span more than it was given.

## Setup

| | |
| --- | --- |
| Store | `MlflowStore`, `mlflow-skinny` 3.18.0, `martian-sdk` 0.1.3, Python 3.12.14, MLflow's default connection pool (5 connections and 10 overflow per process) |
| Database | Postgres 17.12, the `postgres:17` image with default settings (`shared_buffers` 128 MB, `work_mem` 4 MB), in Docker on the native engine with `--cpus 2 --memory 1g`, data on a Docker volume on NVMe |
| Machine | AMD Ryzen 5 3600 (6 cores, 12 threads), 31 GB, Linux 7.0 |
| Traces | Synthetic, through `record_trace`, in one project of one organisation: 5 spans under the binding's root span; 1,000 characters of English-like text in the request, the response and each span's input and output; sessions of 5 turns, 1,000 users, 20 values of a `route` tag, every 50th trace an error. No model calls |
| Writers | 4 writer processes with 4 concurrent writes each, unless a table says otherwise; searches one at a time from a fifth process |
| Run | Every table comes from one run of every phase, about 33 minutes, on a fresh database |

## Results

### Writes as the project grows

Each row covers the traces written since the row before.

| Traces in the project | Write p50 (ms) | p95 | p99 | Traces/s | Postgres CPU per trace (ms) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 165 | 205 | 438 | 93 | 7.1 |
| 5,000 | 164 | 209 | 381 | 95 | 6.5 |
| 10,000 | 181 | 225 | 293 | 86 | 7.0 |
| 25,000 | 177 | 225 | 424 | 88 | 7.0 |
| 50,000 | 186 | 259 | 423 | 82 | 7.5 |
| 75,000 | 165 | 226 | 340 | 94 | 7.0 |
| 100,000 | 150 | 191 | 339 | 103 | 6.9 |

Throughput stayed between 82 and 103 traces a second, and Postgres's own CPU per trace at about
7 ms, though the database outgrew the container's 1 GB of memory at about 60,000 traces.

### Write rate

Offered for 60 seconds each, with the project at 100,000 to 118,000 traces and at most 32 writes
in flight. Latency counts from when a trace was due, so it includes waiting.

| Offered/s | Written/s | Shed | p50 (ms) | p95 | p99 | Postgres CPU per trace (ms) | Postgres cores busy |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 10.1 | 0 | 59 | 96 | 138 | 6.6 | 0.07 |
| 25 | 25.0 | 0 | 54 | 74 | 106 | 7.7 | 0.19 |
| 50 | 50.0 | 4 | 56 | 111 | 392 | 6.7 | 0.34 |
| 100 | 79.6 | 1,213 | 354 | 484 | 681 | 8.4 | 0.67 |
| 150 | 69.0 | 4,844 | 426 | 669 | 936 | 8.7 | 0.60 |
| 200 | 69.0 | 7,839 | 439 | 598 | 965 | 8.4 | 0.58 |

The four writer processes saturate at 69 to 80 traces a second while Postgres uses at most 0.7
of its 2 cores: on this busy machine the limit is the writers' Python, not the database. In the
growth phase each writer process wrote 20 to 26 traces a second.

### Searches as the project grows

Seconds, median of three runs, each measured right after the writes that brought the project to
its size; one run where the first took more than 30 seconds (text across the whole project at
100,000 traces).

| Traces | Traces page | MLflow's first page | Last page | One session | One user | One tag value | Newest 1,000 | Text, one call | Text, whole project | MLflow's text filter | Sessions page | One trace | Memory of the searching process |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.019 | 0.014 | 0.021 | 0.008 | 0.009 | 0.016 | 0.017 | 0.13 | 0.14 | 0.20 | 0.13 | 0.014 | 0.2 GB |
| 5,000 | 0.015 | 0.016 | 0.028 | 0.010 | 0.014 | 0.024 | 0.020 | 0.15 | 0.76 | 1.1 | 0.16 | 0.017 | 0.2 GB |
| 10,000 | 0.016 | 0.019 | 0.062 | 0.010 | 0.021 | 0.051 | 0.015 | 0.16 | 1.9 | 1.7 | 0.14 | 0.014 | 0.3 GB |
| 25,000 | 0.015 | 0.014 | 0.10 | 0.009 | 0.026 | 0.045 | 0.014 | 0.15 | 5.0 | 4.4 | 0.12 | 0.013 | 0.3 GB |
| 50,000 | 0.017 | 0.012 | 0.17 | 0.015 | 0.045 | 0.038 | 0.015 | 0.15 | 14 | 5.7 | 0.13 | 0.013 | 0.3 GB |
| 75,000 | 0.019 | 0.015 | 0.24 | 0.011 | 0.046 | 0.038 | 0.015 | 0.17 | 24 | 7.7 | 0.15 | 0.013 | 0.3 GB |
| 100,000 | 0.015 | 0.013 | 0.45 | 0.009 | 0.040 | 0.039 | 0.017 | 0.16 | 44 | 10 | 0.16 | 0.015 | 0.3 GB |

- **Traces page**: `search_traces` with no filters, first 50. **MLflow's first page**: MLflow's
  own `search_traces` for the same 50 from the same SQL store, what the database alone costs.
  **Last page**: the oldest 50, from a cursor at the project's last 50.
- **One session** matches 5 traces, **one user** 1 in 1,000, **one tag value** 1 in 20 (5,000 at
  100,000 traces), **newest 1,000** is a `since` search over the last 1,000 traces, which is what
  a sampler polling for new traffic asks.
- **Text, one call** looks for text that one trace holds: the call reads the newest 1,000 traces
  and, in a project larger than that, usually returns none with a cursor that reads on. **Text,
  whole project** follows that cursor until it reaches the oldest trace, which holds the text.
- **MLflow's text filter** is MLflow's own `search_traces` with `trace.text ILIKE`, for the
  newest 50 traces holding text that one trace holds: the database looks for it in the content
  of every span of the project, where the whole request and response are, in one call. The
  binding does not use it; it is here to decide whether it should.
- **Sessions page** is `search_sessions` with no filters: the first 1,000 traces, 200 sessions.
  **One trace** is `get_trace` with its spans.
- Memory is the searching process's peak, which only grows.

### Database growth

| Traces | Database | Per trace |
| ---: | ---: | ---: |
| 1,000 | 22 MB | 21.7 KB |
| 10,000 | 175 MB | 17.5 KB |
| 50,000 | 815 MB | 16.3 KB |
| 100,000 | 1,616 MB | 16.2 KB |

At 100,000 traces: `spans` 1,109 MB, `trace_info` 273 MB, `trace_tags` 129 MB,
`trace_request_metadata` 102 MB, each with its indexes and TOAST. The request and the response
are each kept twice, whole in the trace's root span and as MLflow's previews in `trace_info`.

### Trace size

One writer, 200 traces of each shape, with the project at about 118,000 traces. Spans are the
trace's own, under the binding's root span; characters are those of the request, the response and
each span's input and output. The machine's load average was about 33 as this phase began, so
read the write times against each other rather than as one writer's speed.

| Spans | Characters | Write p50 (ms) | p95 | Read p50 (ms) | Database per trace |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 200 | 95 | 140 | 20 | 2.7 KB |
| 5 | 1,000 | 94 | 139 | 29 | 20 KB |
| 20 | 1,000 | 83 | 157 | 23 | 46 KB |
| 50 | 1,000 | 111 | 200 | 30 | 101 KB |
| 5 | 4,000 | 103 | 189 | 38 | 30 KB |
| 5 | 7,900 | 104 | 161 | 23 | 43 KB |
| 5 | 8,001 | 104 | 161 | 21 | 44 KB |
| 5 | 32,000 | 115 | 163 | 22 | 117 KB |
| 5 | 100,000 | 215 | 296 | 27 | 334 KB |

Postgres compresses rows over about 2 KB, every span here among them, and the synthetic text
comes from a small vocabulary, so it compresses better than real traffic would. Read every
database size in this report as a lower bound, most of all for the larger fields.

## What limits what

- **A page reads only its own traces.** The binding pages through MLflow's own search with every
  filter MLflow has (run, session, user, tags, ids, states, time), so the Traces page and each
  filtered page cost what MLflow's own first page costs: 10 to 50 ms at every size up to 100,000
  traces. The Sessions page reads its 1,000 traces in 0.12 to 0.16 seconds at every size, and the
  searching process stays at about 0.3 GB.
- **A later page costs more than the first.** MLflow pages by offset, so the database passes
  over every trace before the page: the last page of 100,000 traces took 0.31 to 0.84 seconds,
  about 3 to 8 µs per trace before it. A cursor on the time of the last trace read would keep
  later pages as cheap as the first; MLflow's page tokens are offsets.
- **Free text looks at the start of a request and a response, in the process, 1,000 traces a
  call.** MLflow's SQL store cannot filter `trace_info`'s previews, so a text search reads 1,000
  traces a call, matches the text against the trace's name and what the previews hold of its
  request and response (up to 1,000 characters, or the first 997 of a longer one), and returns
  what matched with a cursor: 0.13 to 0.17 seconds a call at every size. Searching a whole
  project reads all of it, in calls, each paying the offset of the one before: 1.9 seconds at
  10,000 traces, 14 at 50,000 and 44 at 100,000, but no call holds more than 1,000 traces.
- **MLflow's own text filter reaches the whole text, at the cost of the whole project every
  call.** `trace.text` looks in the content of every span, without an index: 0.2 seconds at
  1,000 traces, 1.7 at 10,000 and 10 at 100,000, for every call, growing with the project. It
  answers a whole project faster than paging through it beyond about 10,000 traces, but no call
  is bounded, and it matches more than a request and a response: every span's inputs, outputs,
  attributes and the JSON they are kept in. The binding does not use it, so every binding
  searches the same reach ([free text](evals-core.md#the-in-process-binding)). An index for it,
  such as a trigram index on `spans.content`, would be a change to MLflow's own tables that its
  migrations do not know, and Postgres's alone.
- **Writes are limited by Python CPU in the writing process**: about 20 to 26 traces a second
  per process, 50 a second sustained through four processes (p99 under 0.4 s) and 69 to 80 at
  saturation. Postgres spent 6.5 to 9 ms of CPU per 5-span trace.
- **A request or a response has no length limit of its own.** The binding keeps them whole in
  the trace's root span and shortens only MLflow's previews to 1,000 characters. A trace whose
  request, response and 5 spans' inputs and outputs are 100,000 characters each took about twice
  the write time of one with 1,000 (215 ms against 94), the same read time, and 17 times the
  space (334 KB). A value of the trace's own metadata is still refused over 8,000 characters on
  Postgres (`varchar(8000)`), as `invalid`, naming `record_trace` and MLflow's
  `trace_request_metadata` table, never the value
  ([what store errors contain](security-and-data.md#what-store-errors-contain)).
- **Storage is linear**: about 16 KB per trace of 5 spans and 1,000-character fields, more than
  two thirds of it spans; about 1.6 GB per 100,000 traces. The store interface has no way to
  delete old traces.

## Volume ceiling for monitoring

For monitoring production traffic (MartianSecurity/martian-notes#196) on this store as it is:

- **Browsing does not limit a project's size up to 100,000 traces**: the Traces page, its filters
  and the Sessions page answer in under 0.2 seconds there, and the last pages in under a second.
  Beyond that, extrapolated: the first pages should stay flat and later pages grow by about 0.3
  to 0.8 seconds per 100,000 traces before them.
- **Free-text search reaches about 10,000 traces back in 2 seconds** of calls and 100,000 in
  about 45 seconds, 1,000 traces a call, and only the first 1,000 characters of each request and
  response. At one trace a second, 10,000 traces is under three hours of traffic: text search
  across a monitored project needs a narrower time window or a text index. MLflow's own filter
  would take about 10 seconds a call at 100,000 traces and, extrapolated, about 100 at a million.
- **At most about 20 to 25 traces a second per writing process, and about 50 a second per
  installation** sustained, measured with four processes. More writer processes raise the second
  figure until Postgres limits it, below.
- **A sampler that reads new traces by time** (`since`) is not limited by project size in the
  measured range: a page of 50 of the newest 1,000 takes 14 to 20 ms, reading 1,000 traces about
  what the Sessions page takes, and reading one trace with its spans 13 to 17 ms. A
  [monitor](evals-core.md#monitors) reads so, at most 1,000 traces a pass, and reads only the
  traces it samples whole. Its thresholds and its [score trends](evals-core.md#score-trends) read
  no traces: they read the monitor's own record of the traces it scored, by time, at most 5,000
  rows.
- **Requests and responses of any length**, measured up to 100,000 characters each.
- **Plan for retention**: at 50 traces a second the store grows by about 4.3 million traces and
  about 70 GB a day (extrapolated from 16 KB a trace). The store deletes a project's traces older
  than a cutoff, 1,000 a call; the edition decides when
  ([deleting runs and traces](evals-core.md#deleting-runs-and-traces)).

## When a different trace store is needed

The binding pages through MLflow, so what remains are the limits of the store itself, MLflow's
SQL store on Postgres: its offset paging and its lack of a text index above, and these,
extrapolated from the CPU and bytes measured per trace, not measured:

- **Ingest above a few hundred traces a second per database.** Postgres spent about 7 to 9 ms of
  CPU per trace, so one core takes about 125 traces a second and the 2 cores here about 250,
  before disk I/O, which was not measured.
- **Retained traces in the tens of millions per database**: about 160 GB per 10 million traces,
  with MLflow's indexes and Postgres's TOAST, and no partitioning by time.
- **Free-text search or analytics across a whole project** at any size much beyond 10,000 traces,
  or past the first 1,000 characters of a request or a response. MLflow's SQL store has no text
  index: the binding filters text in the process and reads every trace on the way, and MLflow's
  own filter scans every span.

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

Every phase, about 35 minutes; the search tables above are its growth phase alone
(`--phases growth`, about 25 minutes):

```bash
uv run --extra postgres python scripts/trace_store_load.py \
  --processes 4 --writers 4 --sizes 1000,5000,10000,25000,50000,75000,100000 \
  --spans 5 --payload 1000 --searches 3 \
  --rates 10,25,50,100,150,200 --rate-seconds 60 --in-flight 32 \
  --shapes 0x200,5x1000,20x1000,50x1000,5x4000,5x7900,5x8001,5x32000,5x100000 --shape-traces 200 \
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
