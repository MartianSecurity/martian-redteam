# Compact action-security model lab

An isolated research package for compact security models: the pinned 149M-class
ModernBERT action-scope experiments, an off-the-shelf injection reference and a
22.7M-parameter MiniLM language-classification experiment. It measures hardware
capacity, grouped partitions, frozen readouts, context ablation and fine-tuning.
This package is not imported
by the SDK and has no service endpoint. Research checkpoints and calibrated estimates
are **not approved for runtime enforcement**.

## Run the real backbone

Build from this directory. The image downloads the public safetensors and tokenizer
at revision `8949b909ec900327062f0ebf497f51aef5e6f0c8`, records SHA-256 hashes,
and verifies them before loading. Runtime is offline and disables remote model code.
The base model is Apache-2.0; dependencies are pinned in `uv.lock`. The image includes
the license, upstream model card and attribution under `/usr/share/licenses/martian-model-lab`.

```sh
docker build -t martian-model-lab:local .
docker run --rm --network none --read-only --tmpfs /tmp:rw,size=128m \
  --cpus 2 --memory 4g martian-model-lab:local \
  --device cpu --tokens 256 512 1024 --threads 2 --iterations 30
```

CPU uses FP32 explicitly. The default `sdpa_math` selection forces PyTorch's math
attention backend. `--attention eager` selects eager attention explicitly; neither
claims FlashAttention. The benchmark uses batch/concurrency one and exact token
buckets built from disclosed synthetic capacity text. This is real encoder execution,
not a mocked inference response or a security accuracy evaluation.

The JSON report separates local initialization, tokenization/transfer/forward and
forward-only p50/p95/p99, sequential throughput and process peak RAM. CUDA runs also
report allocated/reserved memory and device capacity. It does not measure a serving
queue, customer policy construction, network latency, or concurrent throughput.
Record container limits and competing workloads with the report. Thirty iterations
provide a small descriptive sample, not a production tail-latency guarantee.

For the existing local registry, publish the image under the tag in
[kubernetes-benchmark.yaml](kubernetes-benchmark.yaml), then run:

```sh
kubectl --context docker-desktop apply -f kubernetes-benchmark.yaml
kubectl --context docker-desktop -n martian-verify logs -f job/compact-model-benchmark
```

The Job is bounded, runs as a non-root user with a read-only root filesystem, mounts
only temporary storage, and receives no service-account token or customer credentials.
Its image contains the model; no runtime model download is required. Adapt the image
and namespace for another cluster. Kubernetes Jobs have immutable pod templates;
use a new Job name for a different measured configuration.

## GPU and optimizer fit

A GPU-enabled **native Linux Docker Engine with NVIDIA Container Toolkit** is now
available on this host through Docker context `default`: an actual `--gpus all`
container returned the GTX 1660 Ti and 6 GiB capacity on October 17. The separate
`desktop-linux` Docker Desktop VM and its Kubernetes nodes still expose no NVIDIA
device. Host `nvidia-smi` alone does not establish container access. The
benchmark exits if CUDA is requested but unavailable; it does not switch to CPU.

On this host's configured native GPU context:

```sh
docker --context default build --build-arg MODEL_DEVICE=cuda \
  -t martian-model-lab:upgrade-20261017-cuda-v4 .
docker --context default run --rm --gpus all --network none \
  --read-only --tmpfs /tmp:rw,size=128m --memory 4g --cpus 2 \
  martian-model-lab:upgrade-20261017-cuda-v4 \
  --device cuda --precision fp32 --tokens 256 512 1024 --iterations 30
```

The CUDA extra uses PyTorch 2.13.0's CUDA 12.6 wheels. The real GTX run reported
`sm_75` support. Confirm the reported compiled
architectures include the card's compute capability. `--precision fp16` uses CUDA
autocast; master weights remain FP32. Do not call this a 350 MB deployment based only
on half-precision weight arithmetic.

Add `--fit-steps 5 --fit-mode frozen_encoder` to measure an actual linear-head AdamW
workload. `--fit-mode full --checkpointing` measures full-weight AdamW with gradient
checkpointing. The final token bucket is used for optimizer fit. These probes use a
constant compute target, have no security labels and export no checkpoint. An OOM,
nonfinite output or unsupported kernel fails the process; it is a measured failure,
not permission to change the reported configuration. FP16 reports distinguish
attempted steps from completed AdamW updates and overflow-skipped updates; zero
completed updates fails the probe.

### Kubernetes GPU cluster

On October 17, a separate native-Docker Minikube profile, `martian-gpu`, advertised
one GPU and completed NVIDIA's real CUDA vector-add sample in a scheduled pod.
Minikube 1.39.0, Kubernetes 1.37.0 and NVIDIA device plugin 0.20.0 were used. The
six-node Docker Desktop application cluster was not moved. This follows the official
[Minikube GPU setup](https://minikube.sigs.k8s.io/docs/tutorials/nvidia/).

With Minikube installed, use a dedicated kubeconfig and the native daemon explicitly:

```sh
export KUBECONFIG="$HOME/.local/share/martian-security/upgrade-20261017/gpu-kubeconfig"
export DOCKER_HOST=unix:///var/run/docker.sock
minikube start -p martian-gpu --driver=docker --container-runtime=docker \
  --gpus=all --cpus=2 --memory=6144 --kubernetes-version=v1.37.0
minikube image load -p martian-gpu martian-model-lab:upgrade-20261017-cuda-v4
kubectl apply -f kubernetes-gpu-benchmark.yaml
kubectl -n martian-model-lab logs -f job/compact-model-gpu-benchmark-v4
```

The verified Minikube binary is stored locally under that private directory's
`tools/minikube-linux-amd64`; replace `minikube` above with that path if it is not
installed on `PATH`. Its SHA-256 was checked against the upstream release. Keep the
dedicated kubeconfig for GPU commands; the ordinary kubeconfig still selects
`docker-desktop`. Stop this isolated lab when idle with `minikube stop -p martian-gpu`
under the same native Docker environment. The display and other host processes share
the physical GPU outside Kubernetes scheduling, so one advertised GPU does not mean
all 6 GiB is free or that the benchmark is isolated from desktop load.

The [GPU Job](kubernetes-gpu-benchmark.yaml) requests one `nvidia.com/gpu`
device, one CPU (limit two), and 2 GiB RAM (limit 4 GiB). Its image must already be
loaded into the selected GPU cluster; `imagePullPolicy: Never` makes that explicit.
The v4 Job measures FP32 inference and then five full-weight, checkpointed AdamW
capacity updates at its final 1,024-token bucket. It exports no trained checkpoint.
The Job cannot make a CPU-only Docker Desktop node GPU-capable. A GPU node needs
device access, a compatible runtime and NVIDIA device-plugin registration before
the Job can schedule. Local GPU-cluster acceptance is recorded in the workspace
guide linked below.

Security training requires a licensed/authorized dataset, grouped train/calibration/
validation/final-evaluation partitions, reviewed labels or executable ground truth,
context ablations and independent workflow outcomes. Model promotion remains gated
on measured false blocks, task completion, calibration, attack prevention and latency.
See the [suite plan](../../martian-notes/06%20Playground/PLAN_CONTINUOUS_AGENT_SECURITY_SUITE.md).

Actual [Kubernetes CPU measurements](measurements/cpu-kubernetes-20261017.json) and
[CPU optimizer-fit measurements](measurements/cpu-optimizer-fit-20261017.json) retain
the artifact hashes, dependency versions and measured limits. Interpretation belongs
in the workspace [local acceptance guide](../../docs/usage.md#static-inspection-and-compact-model-feasibility).

The [final Kubernetes GPU FP32 and training report](measurements/gpu-kubernetes-fp32-training-20261017.json),
[Docker FP16](measurements/gpu-docker-fp16-20261017.json),
[Docker FP32](measurements/gpu-docker-fp32-20261017.json) and
[full-weight FP32 fit](measurements/gpu-full-training-fp32-512-20261017.json) are real
GTX 1660 Ti measurements. FP32 was faster than FP16 autocast here; the Kubernetes
1,024-token p95 was about 166 ms, missing the proposed 40 ms target. The 512-token
full-weight workload completed five actual updates at about 3.17 GiB sampled process
VRAM. They establish hardware feasibility, not learned security quality or production
tail latency. Current packaging separates cached dependencies, pinned weights and
source so benchmark changes do not require redownloading CUDA libraries.
The corrected [checkpointed 1,024-token fit](measurements/gpu-full-training-fp32-checkpoint-1024-20261017.json)
completed five updates at about 3.02 GiB sampled process VRAM. Checkpoint recomputation
now remains inside the explicitly selected attention context; the earlier failed
attempt is recorded in the acceptance guide and is not counted as a successful fit.
The final v4 Kubernetes Job also completed five checkpointed full-weight updates at
1,024 tokens, at about 3.01 GiB sampled process VRAM. Both retained GPU Jobs completed.

## V1 scoring repair

The current priority is the 149M v1 model; other model and suite work is in the
backlog. Approximately 166 ms at 1,024 tokens is an accepted initial performance
level for this repair. Quality comes first. A 200 ms descriptive p95 screen allows
measurement variation on this shared desktop; it is not a service-level guarantee.
The older 40 ms target remains an optimization aspiration.

`repair_data` generates owned synthetic calibration, validation and reserved
evaluation partitions, with separate task wording and world IDs. Its
`--training-only` mode generates a separate paired continuation-training corpus.
Neither uses public/customer data or model-generated labels. The real SDK grant
predicate supplies the exact-scope labels. These labels do not establish semantic
authorization, NX, Taint, Blast or executed business effects.

Build the dataset image from the SDK root using `Dockerfile.dataset`, then invoke
`python -m action_security_lab.repair_data --output /output/development` by replacing
its entrypoint with `python`. Add `--training-only --output /output/training` for
the separate training corpus. Use a private host directory mounted at `/output`;
each output directory must not already exist.

Build from this directory:

```sh
docker --context default build -f Dockerfile.repair -t martian-model-repair:local .
```

The image's default entrypoint runs the fixed two-epoch paired continuation on CUDA.
Supply `--training-data`, `--development-data`, `--release`, `--release-sha256` and
`--output` using mounted container paths. Pin the **parent release-manifest digest**,
not an image digest. Mount that parent read-only, use `--gpus all --network none`,
a read-only root plus `/tmp` tmpfs, a non-root UID that can read the private weights,
two CPUs and 5 GiB host RAM. Missing CUDA, checksum mismatches and numerical errors
terminate the run. No substitute model/device or final-based checkpoint selection
is implemented. The saved protocol precedes updates; weights and evaluation records
are written to a new release directory.

For recalibration without training, override the entrypoint with
`python -m action_security_lab.repair --phase calibrate` and supply the development
`--dataset`, parent release/digest, `--device cuda` and a new `--output` directory.
This phase does not score reserved evaluation rows. The first such run failed its
quality and calibration screens; see the [model card](MODEL_CARD.md#v1-scoring-repair-and-fresh-development-evidence).

`Dockerfile.repair-evaluate` embeds a frozen release and development corpus through
named build contexts `release` and `dataset`. Its `--phase evaluate` command requires
schema 2, the exact reserved dataset manifest and passing development quality and
calibration screens before final inference. Run it in the dedicated GPU Kubernetes
cluster with one requested GPU, no service-account token, a read-only root and a
bounded writable output volume. It emits the synthetic score report in Job logs;
there is no customer-facing endpoint. Historical challenge evaluation supports both
explicit release schemas and never recalibrates the checkpoint.

## Supervised scope experiment

`contracts.py` defines a versioned customer-local context with an authorized task,
explicit scope, bounded evidence sources, resolved action and rules. Source authenticity,
instruction authority and confidentiality are separate trusted fields. The serializer
inserts the tokenizer's trained CLS/SEP IDs itself, rejects content that tokenizes to
reserved IDs, and rejects missing/stale/conflicting or over-budget context. It never
truncates an action and then scores the remainder. The default budget is 512 total
tokens, at most four sources, eight rules and 32 KiB. Caller text cannot create a
structural token; that property does not guarantee semantic robustness. There are no
new untrained marker embeddings. Mean pooling and a scalar linear readout produce a
single `task_scope` estimate, with no token-by-token generation or execution permit.
NX, learned Taint, consequences and exact argument arithmetic are not learned heads.

### Labels and partitions

The owned synthetic corpus contains 1,920 decisions. The real SDK `ActionGrant`
operation/argument predicates label each row. Payment and CRM decisions occupy
train/calibration/validation/evaluation partitions of 1,024/256/256/256; 128 Docs
sharing decisions with MCP-description carriers form a separate transfer partition.
Complete authored carrier families and world lineages stay within one partition.
All matched allowed/denied and ordinary/override-text variants remain together.
The proposed action is identical across each authorization counterfactual, so an
action-only classifier cannot recover the missing task scope. Ordinary external text
can accompany a violation, and override text can accompany an authorized action.

`task_scope=1` means the resolved operation or at least one exact argument is outside
the trusted grant. It is not an injection-intent label or proof that an agent followed
the carrier. No business tool executes during corpus generation; independent ledger
or document effects remain separate suite acceptance evidence. Public benchmarks,
private customer traces and model-generated labels are not used. Corpus content is
Martian-authored under this repository's Apache-2.0 license. The manifest pins the
corpus, generator and oracle hashes; current builders retain an oracle source snapshot.
The initial v1 experiment's pre-extraction oracle source is retained with its private
local evidence; extracting the SDK wire contract does not change the decision rows.

This is a small, correlated sanity corpus with shared task/rule wording and generator
structure across partitions. It has 25% allowed cases, not production prevalence.
Only two authored families occur in each held-out partition; more generated worlds
are not more independent attack families. Exact matching already solves these labels
deterministically. Successful learning here would establish a numerical contextual
readout experiment, not added protection over the deterministic guard or general
prompt-injection reasoning.

### Containerized data and training

From the SDK repository root, generate the corpus without network access at runtime:

```sh
docker --context default build -f model-lab/Dockerfile.dataset \
  -t martian-scope-dataset:local .
mkdir -p model-data
docker --context default run --rm --network none --read-only \
  --tmpfs /tmp:rw,size=32m --user "$(id -u):$(id -g)" \
  -v "$PWD/model-data:/output" martian-scope-dataset:local \
  --output /output/scope-v1
```

The output path must not exist. Dataset artifacts are research content and do not
belong in customer telemetry. Keep generated weights/data outside the source tree.
Build the CUDA lab with its existing Dockerfile, then from `model-lab/` package the
explicit dataset directory using the named context:

```sh
docker --context default build --build-arg MODEL_DEVICE=cuda \
  -t martian-model-lab:upgrade-20261017-cuda-v5 .
docker --context default build -f Dockerfile.experiment \
  --build-context dataset=../model-data/scope-v1 \
  -t martian-model-scope:upgrade-20261017-v1 .
```

Load that image into the dedicated GPU cluster before applying
[kubernetes-experiment.yaml](kubernetes-experiment.yaml). Use the dedicated kubeconfig
from the GPU setup above. The Job runs FP32/math attention, one example per update,
two full-training epochs with checkpointing, two CPU threads and one GPU. It has a
one-hour deadline, no retry, no service-account token, a read-only root and a separate
2 GiB PVC for artifacts. It does not download weights or contact a model service.
Use a new image tag, Job name and output directory for a changed experiment; existing
outputs cannot be overwritten. The measurement files identify the actual retained
v5 image/source hashes, which can differ from later source improvements.

The protocol is saved before model fitting. Frozen-context and frozen-action-only
linear heads use the same backbone, seed and optimizer settings for 400 AdamW updates;
they form the matched context ablation. A separate full-context candidate uses a fresh head and
full encoder updates. No checkpoint is chosen by examining final results. Each model
uses only calibration rows for probability calibration and only validation rows for
threshold selection. New source produces **schema 2** releases: FP64 raw logits determine
the `>=` decision, and a separately fitted regularized affine logistic mapping supplies
probabilities. Stable sigmoid computation has no artificial clipping; finite-value
validation rejects malformed scores or operating points. Tied scores remain tied.
The fixed 1% empirical false-block budget is evaluated without rounded probabilities.
The final and transfer partitions report recall, false blocks, per-workflow errors,
NLL, Brier and ten-bin ECE/reliability. Few authored families cannot establish a
production 1% risk bound. Historical schema 1 images/reports retain their temperature,
probability thresholds and family-bootstrap diagnostics; their failures remain recorded.
The full candidate is not a controlled full-fine-tuning ablation against the frozen
head; only the frozen pair isolates the effect of context.

The screening target is at least 90% final violation recall, at most 1% empirical false
blocks, a five-percentage-point frozen-context recall improvement and a measured GPU
p95 below 40 ms for the actual serialized workload. These are experimental screens,
not approval for customer enforcement. A final result is never used to move the
threshold. The saved safetensors, tokenizer/config, protocol, operating point and report
receive a checksummed release manifest marked `experimental_not_approved`; checksums
verify integrity, not publisher authenticity. No weights are published automatically.

The research implementation follows the upstream [ModernBERT interface](https://huggingface.co/docs/transformers/model_doc/modernbert)
and uses binary cross-entropy with logits. Latency includes context validation,
serialization, tokenization, transfer, encoder and readout; it excludes network,
queueing and application policy construction. Report the actual token range instead
of treating this short-workload result as the 1,024-token capacity result above.

### Fixed-checkpoint shortcut challenge

The first corpus's violating examples correlate with `read_record`, higher payment
amounts, `Different case` CRM text and owner/reviewer address prefixes. A perfect
score on those partitions does not establish actual scope comparison. The separately
authored `challenge` builder places each operation/value on both sides of symmetric
allowed/violating pairs, across payment, CRM and Docs. It emits 1,152 balanced rows
from 48 fictional world groups with ordinary and override-text carriers. The real
SDK predicate supplies labels. No tool is executed, and API operation compatibility,
injection intent and business effects are outside these labels.

This is a post-v1 diagnostic challenge, not a newly untouched production final set.
Its manifest declares the shortcut hypothesis and quality screen before scoring.
`evaluate_challenge` verifies dataset/artifact checksums and an operator-pinned
release-manifest SHA-256, loads only local safetensors/tokenizer files and keeps the
saved temperature and threshold unchanged. It performs no training, recalibration or
threshold search. `readout.ScopeReadout` is a lab utility with no execution authority,
customer endpoint or runtime enforcement integration. Numeric outputs remain
experimental even if a challenge passes. The mounted release must be trusted and
read-only; its checksums establish integrity, not external publisher authenticity.

From the SDK root, rebuild the dataset image above and choose an unused output path:

```sh
docker --context default run --rm --network none --read-only \
  --tmpfs /tmp:rw,size=32m --user "$(id -u):$(id -g)" --entrypoint python \
  -v "$PWD/model-data:/output" martian-scope-dataset:local \
  -m action_security_lab.challenge --output /output/scope-challenge-v1
docker --context default build -f model-lab/Dockerfile.challenge \
  --build-context dataset="$PWD/model-data/scope-challenge-v1" \
  -t martian-model-challenge:upgrade-20261017-v1 model-lab
```

Load the image into the dedicated GPU cluster, then apply
[kubernetes-challenge.yaml](kubernetes-challenge.yaml) with its dedicated kubeconfig.
That Job pins the locally retained v1 release digest, mounts the existing experiments
PVC read-only and uses one GPU, two CPU cores and a ten-minute deadline with no retry.
The report is emitted to Job logs. New checkpoints/challenges require new image and
Job names and the correct release digest; never overwrite a previously measured
artifact. Read the [model card](MODEL_CARD.md) for measured results and limitations.

## Symmetric v2 experiment

`dataset_v2` defines a new 3,456-row exact-scope corpus after v1 failed its shortcut
challenge. Its train/calibration/validation/evaluation/transfer sizes are
1,536/384/384/768/384. Both labels occur for every proposed action, in symmetric
operation/resource/value pairs, with ordinary and override-text sources balanced
within each group. Complete new authored carrier families and world lineages stay
in their preassigned partition; Docs and MCP descriptions remain transfer-only.
The SDK predicate supplies labels. These are fictional research inputs, with no
business execution or human-reviewed intent labels, and no reused v1 final/challenge
rows. More variants do not establish more independent families. The deterministic
SDK already solves the target predicate; this experiment cannot show added business
protection beyond it.

The experiment loader verifies paired action/evidence identity and opposite labels.
`--full-ablation` additionally trains a full action-only encoder and then reloads the
same pinned base for full-context training. Both use seed 17, the same row order,
two epochs, optimizer, head and calibration/threshold protocol. Only serialized
context inclusion differs. The ablated encoder/head and its operating point are
retained for reproduction; the ordinary release readout still loads `encoder/` and
`full_context.safetensors`. No production promotion is enabled, regardless of scores.
V1's original artifacts and failed challenge remain unchanged.

From the SDK root, rebuild `Dockerfile.dataset`, then select the v2 builder explicitly:

```sh
docker --context default run --rm --network none --read-only \
  --tmpfs /tmp:rw,size=32m --user "$(id -u):$(id -g)" --entrypoint python \
  -v "$PWD/model-data:/output" martian-scope-dataset:local \
  -m action_security_lab.dataset_v2 --output /output/scope-v2
docker --context default build -f model-lab/Dockerfile.experiment \
  --build-context dataset="$PWD/model-data/scope-v2" \
  -t martian-model-scope:upgrade-20261017-v2 model-lab
```

The experiment Dockerfile overlays current lab source onto the pinned existing lab
image. Load the resulting image into `martian-gpu`, then apply
[kubernetes-experiment-v2.yaml](kubernetes-experiment-v2.yaml) using the dedicated
kubeconfig. It uses the existing experiments PVC and a distinct output directory,
with a one-hour deadline, one GPU and no retry. Check available PVC/host capacity
before retaining both full encoders. The completed run passed its short-input within-workflow screen, but missed 63/192
Docs transfer violations. See the [measured v2 model card](MODEL_CARD.md#revised-v2-experiment);
no enforcement promotion is approved.


`challenge_v2` separately authors 1,728 paired decisions with close scalar values,
new read operations, three task/rule paraphrases and changed evidence kinds/counts.
It uses the same exact SDK oracle, with no business execution. Build the dataset image
above, select `-m action_security_lab.challenge_v2` into a fresh output directory,
and build `Dockerfile.challenge` using that directory as the named `dataset` context.
[kubernetes-challenge-v2.yaml](kubernetes-challenge-v2.yaml) pins the saved v2 release
manifest digest and mounts artifacts read-only. The evaluator preserves its original
calibration/threshold and emits a separate report; it performs no training or tuning.

The fixed v2 challenge failed: 4.28% false blocks and 51.62% violation recall. The
[challenge measurement](measurements/scope-v2-challenge-kubernetes-20261017.json) and
model card retain this rejection. Neither experiment is an enforcement release.

## Frozen injection-classifier reference

The separate `injection_reference.py` experiment measures the public
[Protect AI DeBERTa v3 base injection classifier](https://huggingface.co/protectai/deberta-v3-base-prompt-injection-v2),
revision `90c9989b1a342275dd0d1a95aad283c04e075671`. This Apache-2.0, approximately
184M-parameter reference is **archived upstream**. It classifies English input text;
it is not Martian's action-security model, a contextual authorization oracle or a
maintained production dependency. No weights are trained or integrated into the SDK.

The 48 checked-in, Martian-authored examples are 24 benign and 24 injection attempts
across six families. They include quoted security content, ordinary business records,
claimed authority, role delimiters and plausible workflow diversions. These are a
small correlated diagnostic corpus, not a public benchmark or independent production
sample. The 0.5 threshold and 90% recall / 1% false-block screen were fixed before the
first execution. Nothing is calibrated or selected using its results. It cannot be
numerically ranked against the ModernBERT scope corpus because the target labels and
inputs differ.

Download only pinned artifacts into a **new** private directory, outside this checkout:

```sh
python3 download_injection_reference.py "$HOME/.cache/martian-injection-reference-v1"
docker --context desktop-linux build -f Dockerfile.injection-reference \
  --build-context reference="$HOME/.cache/martian-injection-reference-v1" \
  -t 10.96.100.100:5000/martian-injection-reference:upgrade-20261017-v1 .
docker --context desktop-linux run --rm --network none --read-only \
  --cpus 2 --memory 4g \
  10.96.100.100:5000/martian-injection-reference:upgrade-20261017-v1 \
  --threads 2 --iterations 30
```

The downloader verifies every file's SHA-256 and retains a manifest; mismatch or an
existing destination fails. The image includes the upstream license/model card and
hash-locked Python dependencies. Inference verifies model/tokenizer bytes again,
uses ONNX Runtime CPU FP32 and a direct tokenizer, rejects inputs above 512 tokens,
and never loads remote Python code or truncates a decision input. It records the
protocol before evaluation, initialization separately from warmed tokenization plus
forward latency, model scores, family counts and peak process RAM. Repeated-word
capacity buckets are separate from semantic examples. There is no serving queue,
HTTP endpoint, model fallback or runtime policy update.

After publishing this image to the configured local registry, run
[`kubernetes-injection-reference.yaml`](kubernetes-injection-reference.yaml) on
Docker Desktop. The Job has a 15-minute deadline, no retries or service-account
credential, a read-only root and a two-CPU/four-GiB limit. It needs no model downloads
at runtime, but this Job alone does **not** establish network isolation. The retained
v1 run includes ONNX Runtime's read-only telemetry-ID warning; no customer input was
used. Use a new image/Job name for a changed experiment, and preserve the original
report rather than overwriting its evidence.

The [actual Kubernetes report](measurements/injection-reference-kubernetes-20261017.json)
failed the fixed screen: **23/24 injections flagged and 9/24 benign inputs falsely
flagged**. Its 512-token p95 was approximately **2.27 seconds** on this shared local
CPU setup. This reference is not enabled for protection; see the [model card](MODEL_CARD.md#off-the-shelf-injection-reference).


## Small language-classification experiment

The isolated `nx_experiment` compares a frozen readout and a fully fine-tuned
**22,713,601-parameter** encoder/readout on public prompt-injection labels. It uses
[`sentence-transformers/all-MiniLM-L6-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
at revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. This is an English sentence
encoder trained for embeddings, with a new binary head; its upstream 22.7M parameter
count does not imply security quality. This experiment addresses input classification,
not the failed experiments' exact-scope task or contextual action authorization.

The pinned [deepset corpus](https://huggingface.co/datasets/deepset/prompt-injections/blob/4f61ecb038e9c3fb77e21034b22511b523772cdd/README.md)
supplies public binary labels. Its card declares Apache-2.0 at top level and CC-BY-4.0
inside `dataset_info`; the manifest preserves both declarations and attribution.
No redistribution of a derived model is approved here. The separately pinned
[NotInject benchmark](https://huggingface.co/datasets/leolee99/NotInject/blob/847ae76cf8fea5ed325429e569ae8cfef022d2e0/README.md)
(MIT; Hao Li and Xiaogeng Liu) supplies hard benign examples with injection-related
words. Only its three original subsets are read; duplicate train/test/validation
aliases are excluded. All 339 rows are reserved for transfer evaluation.

`download_nx_reference.py` downloads only allowlisted files from pinned revisions,
verifies their published LFS SHA-256 or Git blob identities, and records SHA-256 hashes.
It loads no remote Python code. `prepare_nx_dataset.py` verifies those bytes and groups
normalized text and word-trigram Jaccard matches at 0.5. Groups overlapping final
rows, conflicting development labels and repeated development text are excluded;
remaining groups receive deterministic 70/15/15 train/calibration/validation assignment.
This is lexical leakage screening, not independent semantic-family separation.
Public pretraining contamination and source annotation quality remain unverified.

The resulting counts are 370/83/62 development rows, the original 116-row public
final split, and 339 NotInject rows. Twenty-two conflicting and nine final-overlapping
development rows were removed. The strict serializer rejects over-256-token inputs
and special token IDs, including unknown-token encodings; it never truncates.
Accepted counts are 367/83/59/116/248. Rejections remain explicit in the denominator;
the 91 unsupported transfer inputs are not scored as benign or successful detections.

Use fresh private output directories, outside Git:

```sh
python3 model-lab/download_nx_reference.py /private/nx-inputs
uv run --no-project --with pyarrow==22.0.0 python model-lab/prepare_nx_dataset.py \
  --inputs /private/nx-inputs --output /private/nx-dataset
docker --context default build -f model-lab/Dockerfile.nx \
  --build-context inputs=/private/nx-inputs --build-context dataset=/private/nx-dataset \
  -t martian-model-nx:local model-lab
docker --context default run --rm --gpus all --network none --read-only \
  --tmpfs /tmp:rw,size=128m --cpus 2 --memory 4g --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  -v /private/nx-output:/artifacts martian-model-nx:local \
  --inputs /nx-inputs --dataset /nx-dataset --output /artifacts/release --device cuda
```

The default research base is the existing CUDA lab image `upgrade-20261017-cuda-v5`;
build it using the earlier instructions first, or supply a reviewed `LAB_IMAGE` build
argument. The model/data inputs are copied into this private research image. Do not
publish it as a production image or inference service.

The saved protocol precedes training: seed 31, 300 frozen-head steps, six full epochs,
batch eight, FP32/math attention, masked mean pooling with normalization and a scalar
BCE readout. Full training starts from the frozen head, with separate declared encoder
and head rates. Temperature uses calibration NLL only; the operating threshold uses
validation's 1% false-flag budget only. Final outcomes never select a new checkpoint,
threshold or epoch. The screen requires 90% final recall, at most 1% false flags on
both final sets and 95% accepted-input coverage. Public labels do not prove an actual
agent followed an instruction or caused an effect.

The [real Docker GPU report](measurements/nx-language-docker-20261017.json) failed that
screen for both candidates. Full training completed 276 updates, peaking at about
722 MiB allocated / 886 MiB reserved CUDA memory. Warm 256-token GPU p95 was about
8.92 ms, but the full candidate detected only 41/60 injections and falsely flagged
141/248 accepted hard-benign inputs. No customer integration, blocking threshold,
model endpoint or published weights were enabled. See the [model card](MODEL_CARD.md#small-language-classification-experiment)
for calibration, coverage and interpretation.

`nx_evaluate` verifies an operator-pinned release-manifest SHA-256, every checkpoint
file, and the original dataset hash before fixed CPU evaluation. It performs no
training, threshold tuning, truncation or device substitution. Build
`Dockerfile.nx-evaluate` using named `release` and `dataset` contexts; it defaults to
the existing CPU lab v2 image. Publish to the owned registry, then use
[kubernetes-nx-evaluate.yaml](kubernetes-nx-evaluate.yaml), updating the image and
release hash for a different experiment. The bounded Job has no service-account
credential or business access. The [completed CPU Job report](measurements/nx-language-cpu-kubernetes-20261017.json)
records its actual image digest, Pod identity, resource limits and exit status. Its
fixed-checkpoint errors match the Docker run: 41/60 injections detected and 141/248
accepted hard-benign inputs falsely flagged. Its 256-token CPU p95 was 186.58 ms on
the shared application cluster. This reproduces a rejected result; it does not
promote the model.

The unchanged training protocol also completed in the separate native GPU cluster
using [kubernetes-nx-experiment.yaml](kubernetes-nx-experiment.yaml). Load the exact
locally built image into the dedicated `martian-gpu` profile before applying that
Job; its `imagePullPolicy: Never` does not download or substitute another image.
The Job mounts the existing experiment PVC, requests one GPU, has a ten-minute
deadline and receives no service-account token. Use a fresh Job name and output
directory for a different experiment; do not overwrite a retained release.

The [GPU Kubernetes report](measurements/nx-language-gpu-kubernetes-20261017.json)
records 276 updates in 11.59 seconds and 9.06 ms warmed 256-token GPU p95. All encoder,
head, tokenizer, protocol and operating-point files are byte-identical to the Docker
run, as are every recorded quality score and error count. Only measured timing and
resource observations change the report and release-manifest digest. All nine
release files were checked against the actual cluster files and preserved privately;
identical weights share local storage instead of being duplicated. The idle GPU
profile was stopped with its PVC and Jobs retained. This is reproducibility of a
rejected experiment, not new independent quality evidence.

## Compact pretrained and quantized reference

`wolf_reference.py` compares two pinned exports of
[Wolf Defender Small](https://huggingface.co/patronus-studio/wolf-defender-prompt-injection-small/tree/bcab2eff97bcabd7227849639e2d0d7a61b46c92):
FP32 and INT8 linear operations with INT4 embeddings. The repository's Safetensors
metadata reports 140,642,306 parameters; the selected ONNX files are approximately
563 MB and 96 MB. This is a third-party input classifier, not a Martian-trained
contextual action model. Its model card declares Apache-2.0 with an MIT-derived
foundation; the download/build retain the vendor license, model card and manifests.
No remote Python, installation hook, Joblib or pickle artifact is executed.

The protocol is frozen before inference: the existing 48-row authored reference
corpus, the vendor's 0.5 threshold, no fitting/calibration, and the same 90% recall /
1% false-block screening limits. With only 24 benign examples, even zero false blocks
cannot establish the production target. Both variants score every accepted complete
input separately. Inputs above 1,024 tokens are rejected, never truncated. This does
not reproduce the vendor's 2,048-token sliding-window document benchmark. NotInject,
Deepset and other disclosed training sources are excluded from independent acceptance
for this candidate. Previous final sets are not used to select another threshold.

Thirty sequential warm iterations at four recorded token lengths measure tokenizer,
array conversion and ONNX CPU inference; separate initialization and process peak RAM
are recorded. There is no network, queueing, GPU, action context, causal attribution,
recipient authority or end-to-end prevention claim. The small screen cannot promote
either export, even if it passes. Quantization agreement and errors must be examined
against the FP32 result rather than inferred from smaller file size.

```sh
python download_wolf_reference.py /private/wolf-reference
# From model-lab/, supply the directory created above as the named build context.
docker build -f Dockerfile.wolf-reference \
  --build-context reference=/private/wolf-reference \
  -t martian-wolf-reference:local .
docker run --rm --network none --read-only --cpus=2 --memory=4g \
  martian-wolf-reference:local --variant fp32 --threads 2 --iterations 30
docker run --rm --network none --read-only --cpus=2 --memory=4g \
  martian-wolf-reference:local --variant int8-int4 --threads 2 --iterations 30
```

The [Kubernetes Job](kubernetes-wolf-reference.yaml) has no service-account credential,
no retry, a read-only filesystem and explicit CPU/memory/time limits. Pin the built
registry digest before applying it. Run the variants sequentially; for the quantized
run use a distinct Job name and `--variant int8-int4`. Keep detailed results and image
provenance private until the sanitized measurement report is reviewed. The completed
[measurement report](measurements/wolf-reference-kubernetes-20261017.json) records
FP32 detecting 21/24 injections and falsely flagging 2/24 benign inputs; INT8/INT4
flagged all 48 inputs and matched FP32 on only 23 decisions. A separate Docker process
reproduced the quantized scores exactly. Both fail the frozen screen and remain
outside the runtime. The [model card](MODEL_CARD.md#compact-pretrained-and-quantized-reference)
records error families, CPU latency, memory, the diagnostic reproduction and limits.
