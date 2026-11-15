# Trace store limits

How far `MlflowStore`, the store on `mlflow-skinny`'s SQL store in the same process
([the in-process binding](evals-core.md#the-in-process-binding)), holds traces on Postgres: write
latency, search latency and database growth as the number of traces, the write rate and the
size of a trace grow, and the volume at which something else is needed.

**Measured on one developer machine** that was running about ten other agents at the same time
(load average 13 to 27 on 12 threads). Read the numbers as orders of magnitude and as the shape
of each curve, not as a server benchmark. Anything beyond 100,000 traces in one project, or
beyond the rates in the tables, is extrapolated and says so.

## Setup

| | |
| --- | --- |
| Store | `MlflowStore`, `mlflow-skinny` 3.18.0, `martian-sdk` 0.1.3, Python 3.12.14, MLflow's default connection pool (5 connections and 10 overflow per process) |
| Database | Postgres 17.12, the `postgres:17` image with default settings (`shared_buffers` 128 MB, `work_mem` 4 MB), in Docker on the native engine with `--cpus 2 --memory 1g`, data on a Docker volume on NVMe |
| Machine | AMD Ryzen 5 3600 (6 cores, 12 threads), 31 GB, Linux 7.0 |
| Traces | Synthetic, through `record_trace`, in one project of one organisation: 5 spans; 1,000 characters of English-like text in the request, the response and each span's input and output; sessions of 5 turns, 1,000 users, 20 values of a `route` tag, every 50th trace an error. No model calls |
| Writers | 4 writer processes with 4 concurrent writes each, unless a table says otherwise; searches one at a time from a fifth process |

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

Seconds, median of three runs; one run where the first took more than 30 seconds (the Traces
page, text and Sessions page at 100,000 traces, and text at 75,000).

| Traces | Traces page | MLflow's first page | One session | One user | One tag value | Newest 1,000 | Text | Sessions page | One trace | Memory of the searching process |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.16 | 0.015 | 0.010 | 0.010 | 0.021 | 0.14 | 0.13 | 0.14 | 0.013 | 0.2 GB |
| 5,000 | 0.88 | 0.013 | 0.010 | 0.015 | 0.051 | 0.14 | 0.71 | 0.86 | 0.013 | 0.3 GB |
| 10,000 | 1.8 | 0.036 | 0.014 | 0.028 | 0.13 | 0.18 | 1.6 | 4.2 | 0.036 | 0.4 GB |
| 25,000 | 5.3 | 0.018 | 0.012 | 0.040 | 0.30 | 0.14 | 5.2 | 5.1 | 0.014 | 0.7 GB |
| 50,000 | 13 | 0.073 | 0.012 | 0.055 | 0.69 | 0.16 | 14 | 24 | 0.051 | 1.7 GB |
| 75,000 | 29 | 0.028 | 0.010 | 0.095 | 1.3 | 0.25 | 33 | 29 | 0.025 | 2.8 GB |
| 100,000 | 65 | 0.021 | 0.029 | 0.100 | 2.6 | 0.26 | 55 | 55 | 0.026 | 3.7 GB |

- **Traces page**: `search_traces` with no filters, first 50. **MLflow's first page**: MLflow's
  own `search_traces` for the same 50 from the same SQL store, which the binding does not use
  today; it shows what the database alone costs.
- **One session** matches 5 traces, **one user** 1 in 1,000, **one tag value** 1 in 20 (5,000 at
  100,000 traces), **newest 1,000** is a `since` search over the last 1,000 traces, which is what
  a sampler polling for new traffic asks. **Text** matches one trace. **Sessions page** is
  `search_sessions` with no filters. **One trace** is `get_trace` with its spans.
- Memory is the searching process's peak, which only grows.

### Database growth

| Traces | Database | Per trace |
| ---: | ---: | ---: |
| 1,000 | 21 MB | 20.9 KB |
| 10,000 | 180 MB | 18.0 KB |
| 50,000 | 837 MB | 16.7 KB |
| 100,000 | 1,674 MB | 16.7 KB |

At 100,000 traces: `spans` 926 MB, `trace_request_metadata` 346 MB, `trace_info` 274 MB,
`trace_tags` 126 MB, each with its indexes and TOAST. The request and the response are each kept
twice, as MLflow's previews and whole in trace metadata.

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
| 5 | 8,001 | refused | | | |

Postgres compresses rows over about 2 KB, every span here among them, and the synthetic text
comes from a small vocabulary, so it compresses better than real traffic would. Read every
database size in this report as a lower bound, most of all for the larger fields.
Of the 8,001-character traces, 195 of 200 were refused; the other 5 ended in a space, which
Postgres drops.

## What limits what

- **Search is limited by the binding, not the database.** Every search reads every trace its
  MLflow filters match into the process, then filters text, sorts and pages there. The Traces
  page, text search and the Sessions page therefore read the whole project: about 1 second at
  5,000 traces, 2 at 10,000, 5 at 25,000 and about a minute at 100,000, holding about 37 MB of
  memory per 1,000 traces. Their cost grows faster than the trace count. MLflow pages by offset,
  so each later page of 500 re-reads the rows before it, and the process holds every trace read;
  this run did not separate the two. MLflow's own first page of the same project stays at 15 to
  75 ms up to 100,000 traces, and one session and one trace stay under 100 ms.
- **Filtered searches cost about 0.15 to 0.5 ms per matching trace**: one tag value at 100,000
  traces (5,000 matches) takes 2.6 seconds, and the newest 1,000 takes 0.13 to 0.4 seconds at
  every size.
- **Writes are limited by Python CPU in the writing process**: about 20 to 25 traces a second
  per process, 50 a second sustained through four processes (p99 under 0.35 s) and 72 to 79 at
  saturation. Postgres spent 6 to 9 ms of CPU per 5-span trace.
- **A request or a response over 8,000 characters is refused on Postgres** as `invalid`, because
  the binding keeps them whole in MLflow's trace metadata (`varchar(8000)`). Nothing of the trace
  is kept. The refusal's detail carries MLflow's SQL error, including the SQL parameters, so it
  quotes the start of the request.
- **Storage is linear**: about 17 KB per trace of 5 spans and 1,000-character fields, more than
  half of it spans; about 1.7 GB per 100,000 traces. The store interface has no way to delete old
  traces.

## Volume ceiling for monitoring

For monitoring production traffic (MorningHQ/martian-notes#196) on this store as it is:

- **At most about 10,000 traces in a project** that people browse. There the Traces page and
  text search take about 2 seconds and the Sessions page longer, and all three grow faster than
  the project from there. At one trace a
  second that is under three hours of traffic, so a monitored project outgrows the console's
  unfiltered pages within a day until the binding stops reading every trace.
- **At most about 20 traces a second per writing process, and about 50 a second per
  installation** sustained, measured with four processes. More writer processes raise the second
  figure until Postgres limits it, below.
- **A sampler that reads new traces by time** (`since`) is not limited by project size in the
  measured range: 0.15 to 0.4 seconds per 1,000 new traces, and reading one trace with its spans
  takes 15 to 80 ms.
- **Requests and responses of at most 8,000 characters each** on Postgres.
- **Plan for retention**: at 50 traces a second the store grows by about 4.3 million traces and
  about 70 GB a day (extrapolated from 17 KB a trace).

## When a different trace store is needed

The search ceiling above belongs to the binding, and paging through MLflow instead of reading
every trace would lift it for every search except free text: MLflow's own indexed reads did not
slow down up to 100,000 traces. The limits of the store itself, MLflow's SQL store on Postgres,
are further out. These are extrapolated from the CPU and bytes measured per trace, not measured:

- **Ingest above a few hundred traces a second per database.** Postgres spent about 8 ms of CPU
  per trace, so one core takes about 125 traces a second and the 2 cores here about 250, before
  disk I/O, which was not measured.
- **Retained traces in the tens of millions per database**: about 170 GB per 10 million traces,
  with MLflow's indexes and Postgres's TOAST, and no partitioning by time.
- **Free-text search or analytics across a whole project** at any size much beyond 10,000 traces.
  The binding filters text in the process, and MLflow's SQL store has no text index, so these
  read every trace whatever the binding does.

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

The run above, about 40 minutes:

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
