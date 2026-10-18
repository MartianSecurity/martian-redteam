# Compact security model experiments — 2026-10-17

**Status: no candidate accepted for runtime integration.
Local research only; not approved for customer enforcement or public launch.**
The scope readouts, small language experiment and third-party references below each
retain their own failed quality screen. Measured speed or quantization does not
supersede those decisions.
The [measured report](measurements/scope-kubernetes-20261017.json) pins the base,
dataset, source/dependency hashes, operating points and resulting artifact checksums.
Weights remain in local lab storage; they are not included in this repository.

The subsequent [symmetric challenge report](measurements/scope-challenge-kubernetes-20261017.json)
records **576/576 false blocks** and 576/576 violations flagged. The model classified
every input as a violation at its unchanged operating point. The earlier perfect
synthetic evaluation below does not survive this broader diagnostic corpus.

## Revised v2 experiment

**V2 is also rejected for runtime integration after its separate challenge failed.** The
[symmetric v2 report](measurements/scope-v2-kubernetes-20261017.json) records 3,456
new owned synthetic rows: 1,536 train, 384 calibration, 384 validation, 768 evaluation
and 384 Docs transfer. Every proposed action/evidence pair occurs with both scope
labels. Eight training families and disjoint 2/2/4/4 held-out families use a shared
rule/generator; these are not independent production observations.

Both fully trained encoders used the same base, seed, FP32/checkpointing, batch one,
2e-5 learning rate and two epochs (3,072 updates each). The action-only encoder is
now a matched full-training ablation. All 16 saved artifact checksums were verified.

| Model | Evaluation detected / 384 | Evaluation false blocks / 384 | Docs transfer detected / 192 | Docs transfer false blocks / 192 |
| --- | --- | --- | --- | --- |
| Frozen context | 248 | 10 | 153 | 50 |
| Frozen action only | 0 | 0 | 0 | 0 |
| Full action only | 0 | 0 | 0 | 0 |
| Full context v2 | 384 | 0 | 129 | 0 |

The full candidate passes its within-workflow synthetic screen and matched context
comparison, but misses 63/192 unseen Docs violations. It does not meet the proposed
90% recall screen across those workflows. Temperature again reached the minimum
0.05, with a saturated threshold of 1.0; the perfect in-family scores are not evidence
of reliable probabilities. The separate [fixed-checkpoint challenge](measurements/scope-v2-challenge-kubernetes-20261017.json)
changed scalar closeness, read operations, task/rule paraphrases and source metadata/count
without fitting or recalibration. Of 864 allowed rows, 37 were falsely flagged (4.28%);
418/864 violations were missed (51.62% recall). Only 409/864 paired comparisons classified
both members correctly. Resource mismatches had 7.64% recall, value mismatches 47.22%
and operation mismatches 100%, with differing false blocks. This fails both the 1%
false-block and 90% recall screens. Three authored families with correlated variants
and multiple changed factors do not isolate the cause of failure. They do show that
within-generator evaluation did not establish useful generalization.

Challenge inputs spanned 192–285 tokens and took 27.11/40.01/41.68 ms p50/p95/p99.
The GPU shared host resources with local Docker/Kubernetes deployment work; this is
an observed workload result, not an isolated throughput benchmark. The checkpoint,
calibration and threshold remain unchanged. Do not tune against this now-observed
challenge or promote either rejected checkpoint.

The short 192–216-token workload measured 26.64/28.55/29.35 ms p50/p95/p99 on the
GTX 1660 Ti, including local validation/serialization/tokenization/transfers and
encoder/head, excluding network and queueing. Peak full-training allocated memory
was 3,097,826,304 bytes, reserved memory 3,152,019,456 bytes and process CPU RSS
1,948,352,512 bytes. This does not retest the slower 1,024-token capacity result.
The deterministic grant evaluator remains authoritative and already solves these
labels; neither candidate establishes additional protection beyond it.

The remaining sections describe the original **v1** experiment and its rejection.

## Model and intended use

ModernBERT-base at revision `8949b909ec900327062f0ebf497f51aef5e6f0c8`, with
attention-mask mean pooling and one scalar linear head: 149,015,041 trainable
parameters. The backbone/tokenizer are Apache-2.0; original attribution is retained
in `licenses/`. This is a single forward-pass estimate of `task_scope` violation on
short, structured, fictional action contexts. It generates no explanation tokens.
It is not an authorization issuer, injection detector, causal attribution model,
consequence predictor or independent identity verifier.

Only the controlled experiment is supported. Do not use the checkpoint for a customer
allow/deny decision. Hard SDK authorization and arithmetic remain authoritative.
No customer inference or training data was used. No private corpus was published.

## Training and data

1,920 owned synthetic decisions labeled by the real SDK's exact-grant predicates.
Train/calibration/validation/evaluation contain 1,024/256/256/256 payment and CRM
examples; 128 Docs/MCP-description examples are a combined workflow/carrier transfer
partition. Source families and world lineages are disjoint; paired variants stay
together. There are only two authored families in each held-out partition. The rule
wording and generator structure are shared. Labels are 75% violations and do not
represent production traffic. No business action executes during data generation.

Two full-weight AdamW epochs completed 2,048 actual updates on a GTX 1660 Ti in
Kubernetes, with batch one, FP32, checkpointing and forced math attention. Learning
rate was 2e-5; seed 17 and all settings were fixed before final evaluation. Fixed
seeds do not guarantee bitwise-identical retraining across CUDA/library versions.
Frozen-context and action-only heads were trained separately as a matched ablation.

## Results

Thresholds were selected on validation only at a 1% empirical false-block budget.
The final partition was not used for training, calibration or threshold selection.

| Model | Final violations detected / 192 | Final false blocks / 64 | Transfer violations detected / 96 | Transfer false blocks / 32 |
| --- | --- | --- | --- | --- |
| Frozen context | 12 | 0 | 81 | 13 |
| Frozen action only | 0 | 0 | 0 | 0 |
| Full context | 192 | 0 | 96 | 0 |

The deterministic guard defines these labels and already solves them exactly. These
results do not establish incremental protection over that guard. The frozen pair
shows a 6.25-point contextual recall gain on the final partition, but frozen transfer
false blocks are high. Comparing full-context training to a frozen action-only head
also changes the training regime and cannot isolate context's contribution.

The full candidate's fitted temperature reached the grid minimum, **0.05**, and its
validation-selected threshold is **1.0** due to sigmoid saturation. Its tiny reported
NLL/Brier/ECE on this easy separable corpus is not evidence of reliable real-world
probabilities. This operating point is unsuitable for production promotion without
independent, diverse and benign-heavy calibration and threshold validation. Sixty-four
correlated allowed final rows cannot demonstrate a production false-block rate below
1%; the report's two-family bootstrap intervals do not resolve that limitation.

On 182–201-token final inputs, local context validation, serialization, tokenization,
transfer, encoder and head took **25.58/27.77/29.53 ms p50/p95/p99**. Queueing/network
are excluded. Full training allocated 3,097,823,744 bytes at peak and reserved
3,152,019,456; sampled process memory peaked at 3,106 MiB. CPU RSS peak was about
1.33 GiB. The desktop shared the GPU and CPU work occurred during this experiment.
This does not meet or retest a 1,024-token SLO: that earlier capacity run was about
166 ms p95. No optimized CPU, quantized quality or service-economics claim is made.

## Subsequent fixed-checkpoint challenge

The checkpoint, temperature 0.05 and threshold 1.0 were frozen. A separately authored
post-v1 corpus contains 1,152 rows with symmetric operation/resource/value comparisons
across payment, CRM and Docs: each value and operation name occurs in both classes.
Forty-eight world groups are correlated template variants, not independent attack
families. Source text and task wording also differ from training, so this challenge
does not isolate one causal shortcut. It is diagnostic evidence, not a new untouched
production final set. No fitting, recalibration or threshold change was performed.

All 576 allowed rows were falsely flagged, and no one of the 576 paired scope
comparisons classified both its allowed and violating member correctly. Brier score
was 0.5. This fails the declared 1% false-block screen and invalidates any claim of
useful generalization from the first corpus. Local scoring at 181–213 tokens took
24.82/26.44/27.91 ms p50/p95/p99 on the same shared GTX 1660 Ti. Speed does not rescue
the quality failure. The read-only release and predeclared challenge are retained;
future training requires a revised corpus and newly reserved evaluation data.

## Retained-score diagnosis of the scope failures

A subsequent [read-only numerical diagnosis](measurements/scope-score-diagnosis-20261017.json)
separates representation quality from the failed operating point. It uses retained,
hash-verified raw scores, with no new inference, training or threshold selection.
Historical error counts and rejection decisions above remain unchanged.

Both scope releases selected temperature **0.05** and probability threshold **1.0**.
The lab's clipped sigmoid rounds sufficiently positive scores to exactly one,
discarding their ordering. V1's challenge raw scores ranged from 3.059–10.698 for
allowed actions and 10.522–12.589 for violations. Every scaled value reached the
clip limit, so all 1,152 probabilities became one. Raw-score ranking AUC is **0.999967**
on this exposed diagnostic set: the all-block result does not establish that the
encoder failed to learn a useful distinction. It establishes failure of the frozen
calibration/operating point on changed inputs. This AUC does not supply a validated
threshold, calibrated probabilities or production acceptance.

The experiment also has a precision inconsistency: validation thresholds were chosen
from float32 score arrays, while evaluation built float64 arrays from Python scalars.
At temperature 0.05, raw score 1.0 maps to exactly 1.0 in float32 but approximately
0.99999999794 in float64. With a threshold of exactly one, this changes the decision.
Recomputing v2's retained challenge at float32 misses 411 violations rather than 418,
with the same 37 false blocks. V1 remains all-blocking in either precision. This
specific inconsistency explains seven v2 decisions, not the bulk of its errors.
V2's raw-score ranking AUC is **0.808416**, with substantial class overlap; numerical
repair alone is not established as sufficient to meet its quality gate.

The next experiment must use a consistent numeric representation and select decisions
in unsaturated score/logit space, separately from reported probability calibration.
Calibration and validation need more varied development examples, followed by fresh
reserved evaluation. No threshold may be chosen from these inspected challenges and
then presented as independent acceptance. The current prototypes also learn exact
grant predicates already enforced by code; they do not establish learned semantic
intent, recipient entitlement or source influence. These findings refine the cause
of rejection without changing runtime behavior or promoting a model.

## V1 scoring repair and fresh development evidence

**Paused on 2026-10-18:** the continuation run was stopped at the founder's request
before its final checkpoint was written. The original weights, failed recalibration
evidence, source and local logs are retained. No completed repaired model or final
evaluation is available; alternate attention modes and latency optimizations remain
unverified. This branch is a work-in-progress checkpoint, not a promoted release.

New scope releases use schema 2. The decision compares finite FP64 raw logits with
a validation-only threshold. Separately, a calibration-only affine logistic mapping
reports probabilities, with fixed regularization and a stable unclipped sigmoid.
This fixes probability saturation and inconsistent precision in threshold selection;
it does not repair representations by itself. Original schema 1 artifacts and their
failed results remain immutable. New training uses the corrected path, and readout
loading requires checksums for every required model/config/tokenizer artifact.

The [recalibration-only measurement](measurements/scope-repair-calibration-20261017.json)
uses the original v1 weights with 1,152 fresh calibration and 1,152 fresh validation
rows. Another 1,152 evaluation rows were reserved before inference and remain unscored
at this checkpoint. The corpus includes payment, CRM and Docs, near/far values,
symmetric operation/resource/value comparisons, four task-wording families per split,
and one to three evidence sources. It was authored after historical failure analysis;
shared generation logic and synthetic labels limit its independence.

Validation reports **5/576 false blocks and 145/576 missed violations** (74.83% recall).
For an apples-to-apples comparison, applying the historical operating point to these
same retained validation logits gives 338 false blocks and 27 misses. Recalibration
trades disruption for misses; it does not by itself improve the encoder's ranking.
NLL is 0.4269, Brier 0.1241 and ten-bin ECE 0.1469. Local validation p95 is **52.86 ms**
at 204–368 tokens on the GTX 1660 Ti. This fails the predeclared quality/calibration
screens. Recalibration does not justify model promotion. The next continuation uses
the same v1 checkpoint and a separate 3,456-row training-only corpus; its fixed paired
loss and two epochs are recorded before updates. No final evaluation is used to choose
weights, calibration or thresholds.

## Input, privacy and failure boundaries

The experimental [context contract](src/action_security_lab/contracts.py) accepts
at most four sources, eight rules, 32 KiB and a default 512-token total budget.
The serializer rejects reserved-token content and incomplete/over-budget evidence
without truncation. Estimates never convey execution authority. No runtime service,
hosted upload, automatic policy update or production fallback is enabled.

Missing evidence, arbitrary languages, new rules/operations, adversarial paraphrases,
large workflows, real identities, data disclosure, source causality and business
outcomes remain unvalidated. Broader transfer requires newly reserved families; do
not repeatedly tune against this now-observed final partition. Expand the corpus
and compare a fully trained context ablation before making a generalization claim.

## Off-the-shelf injection reference

A separate frozen Protect AI DeBERTa v3 base comparison completed as a real Docker
image/Kubernetes Job on October 17. The [report](measurements/injection-reference-kubernetes-20261017.json)
pins the upstream revision, model/tokenizer/corpus/source hashes, image digest,
dependencies and two-CPU/four-GiB limits. It uses the upstream FP32 ONNX classifier,
no fitting, no truncation and a threshold fixed at 0.5 before execution. Its input
injection task differs from the action-scope experiments above. This is not a new
Martian checkpoint or a substitute for failed contextual quality gates.

| Diagnostic family | Injections flagged / 4 | Benign falsely flagged / 4 |
| --- | --- | --- |
| Ordinary records and direct overrides | 4 | 0 |
| Business record carriers | 4 | 0 |
| Claimed authority and procedures | 4 | 2 |
| Plausible workflow diversion | 3 | 2 |
| Format/role delimiters | 4 | 1 |
| Quoted security content | 4 | 4 |
| **Total / 24** | **23** | **9** |

The 37.5% empirical false-block rate fails the predeclared 1% screen. The tiny,
authored and correlated corpus cannot establish a population rate or comparative
product superiority. In particular, all four benign quoted-security examples were
flagged. No threshold was changed after observing these outcomes. The upstream card
also identifies English-only scope, system-prompt false positives and archived
maintenance status; its vendor accuracy claims are not adopted as local evidence.

| Capacity tokens | p50 / p95 / p99, ms | Sequential decisions/s |
| --- | --- | --- |
| 64 | 150 / 174 / 182 | 6.62 |
| 128 | 266 / 348 / 367 | 3.65 |
| 256 | 799 / 1,059 / 1,139 | 1.27 |
| 512 | 1,844 / 2,273 / 2,303 | 0.52 |

These 30 warmed iterations per bucket include tokenizer/array conversion and one
forward pass, at batch/concurrency one. Initialization was about 3.59 seconds and
peak process RAM about 1,305 MiB. CPU contention and the Kubernetes CPU limit are
part of this observed result; no isolated throughput, GPU, quantized, calibration,
service cost or end-to-end attack-prevention claim follows. Since quality already
failed, this experiment does not justify promoting the classifier through latency
optimization alone. The supported deterministic runtime remains unchanged.


## Small language-classification experiment

**Decision: rejected for protection.** This separate 22,713,601-parameter MiniLM
encoder/readout was trained on public input-injection labels, not action/task
permissions. It adds a useful speed measurement and a negative language-quality
result. It does not replace either rejected ModernBERT scope candidate or supply
accepted NX/Permit/Taint heads.

The [method, provenance, license declarations and commands](README.md#small-language-classification-experiment)
are canonical. Exact bytes and source/protocol hashes are retained in the
[Docker report](measurements/nx-language-docker-20261017.json). The release manifest
SHA-256 is `33d850d8dbcf9e2ace2176f2e66ff21dd3ef77be2305627a656917ec0505c25e`.
No customer content or generated judge labels were used. Lexical groups reduce direct
leakage; they do not prove family independence, annotation correctness or absence of
public pretraining contamination.

| Fixed candidate | Public final injections detected | Public final benign falsely flagged | Accepted NotInject benign falsely flagged |
| --- | --- | --- | --- |
| Frozen MiniLM + readout | 26/60 (43.33%) | 0/56 | 102/248 (41.13%) |
| Six-epoch full fine-tune | 41/60 (68.33%) | 0/56 | 141/248 (56.85%) |

The full candidate's calibration-only temperature is 2.51855196 and validation-only
threshold 0.72253901; the frozen values are 0.46121199 and 0.95092958. Full-candidate
final NLL/Brier/ECE are 0.1767/0.0545/0.0964; on accepted NotInject they worsen to
1.6833/0.5359/0.6645. This transfer failure rules out treating those scores as reliable
probabilities across workloads. Zero flags among 56 public benign rows still has a
row-wise Wilson 95% upper bound of about 6.42%, before accounting for correlation.
It does not establish a 1% production false-positive bound.

The 256-token/special-token contract accepted 367/370 training, 83/83 calibration,
59/62 validation, 116/116 final and 248/339 transfer inputs. It rejected 92 inputs
for special-token IDs (including unknown encodings) and five for token budget across
all partitions. The 91 rejected transfer inputs are **unsupported**, not true negatives.
Transfer coverage of 73.16% also fails the predeclared 95% screen. No post-result
serializer or threshold change was used to hide that failure.

The GTX 1660 Ti run completed 276 full-weight AdamW updates in approximately 11.81
seconds, with 757,096,960 bytes allocated and 929,038,336 bytes reserved at peak.
These are PyTorch process allocator measurements, not whole-device isolation. The
same host also ran desktop/application workloads.

| Tokens | Native Docker GPU p95 | Native Kubernetes GPU p95 | Native Docker CPU p95 | Desktop Kubernetes CPU p95 |
| --- | --- | --- | --- | --- |
| 64 | 7.52 ms | 8.23 ms | 29.26 ms | 34.59 ms |
| 128 | 8.21 ms | 8.04 ms | 40.90 ms | 69.76 ms |
| 256 | 8.92 ms | 9.06 ms | 105.32 ms | 186.58 ms |

The [fixed-checkpoint Kubernetes CPU report](measurements/nx-language-cpu-kubernetes-20261017.json)
reproduces the full candidate's exact final and transfer error counts at its frozen
operating point. The actual Job completed with two CPU cores and 2 GiB RAM; its
image digest and Pod identity are retained in the report. This is execution
reproducibility of the same inspected datasets, not another independent quality test.

The [native GPU Kubernetes training reproduction](measurements/nx-language-gpu-kubernetes-20261017.json)
completed 276 updates in 11.59 seconds under a two-core/four-GiB limit with one GPU.
Its model, tokenizer, protocol and operating-point files are byte-identical to the
original Docker release; all recorded candidate scores and errors also match.
Its release SHA-256 is `acc1cd93768d5661f3c0a67619e7f4e804a3f8bef2463cfaa602098657e184e4`;
the different digest reflects the new measurement report. All nine files were
verified against the cluster output. No new checkpoint or threshold was selected
from these already inspected outcomes.

FP32/math attention, two CPU threads, batch one, five warmups and thirty measured
requests were fixed. Synthetic capacity text measures tokenization, transfer and a
single encoder/head pass. It does not measure real workflow construction, serving
queues, concurrent throughput, hosted cost or 1,024-token inference. The long-input
CPU target and full action-security quality target remain unmet.

The experiment identifies a data/transfer problem despite suitable short-input GPU
speed. Further work needs a new declared language task, provenance-reviewed diverse
benign/attack development data and genuinely reserved evaluation. Do not train on
these inspected final sets and present them again as untouched evidence. No learned
runtime enforcement or public developer-model release is approved.

## Compact pretrained and quantized reference

**Both Wolf Defender Small exports are rejected for runtime integration.** The
[pinned public model](https://huggingface.co/patronus-studio/wolf-defender-prompt-injection-small/tree/bcab2eff97bcabd7227849639e2d0d7a61b46c92)
contains 140,642,306 parameters according to its Safetensors metadata. The selected
FP32 ONNX file is 563,140,601 bytes; the INT8-linear/INT4-embedding export is
96,296,126 bytes. The Apache-2.0 license, upstream card, provenance and download
hashes are retained in the image. No remote Python or pickle was executed.

The [measurement report](measurements/wolf-reference-kubernetes-20261017.json)
records two completed Kubernetes Jobs using one immutable image, two CPU threads,
4 GiB memory limits and ONNX Runtime 1.31.0. Each independently scored the existing
48-row authored corpus at a frozen **0.5** threshold, without fitting or calibration.
This is a small correlated input-classification screen, not contextual action
permission or an independent production benchmark. The vendor discloses NotInject,
Deepset and other public benchmarks among training sources; those sources were
excluded from independent acceptance for this reference.

| Export | Injections detected | Benign inputs falsely flagged | Peak process RAM | Decision |
| --- | --- | --- | --- | --- |
| FP32 | 21/24 (87.5%) | 2/24 (8.3%) | 1,129.1 MiB | Fails both frozen quality limits |
| INT8/INT4 | 24/24 (100%) | 24/24 (100%) | 443.1 MiB | Flags every example; rejected |

FP32 missed three of four plausible-workflow-diversion attempts and flagged two of
four benign quoted-security examples. Its smaller benign error count than the
Protect AI reference comes with lower attack recall; it is not an overall quality
improvement. The quantized export agreed with FP32 on only **23/48** predictions;
maximum probability difference was 0.9839. A separate network-disabled Docker
process reproduced all 48 quantized predictions and probabilities exactly. This
rules out a Kubernetes-only reproduction issue; it does not identify the export or
runtime defect. No cause or universal vendor failure is claimed.

| Complete input tokens | FP32 p50 / p95 / p99 ms | INT8/INT4 p50 / p95 / p99 ms |
| --- | --- | --- |
| 64 | 52.12 / 65.78 / 69.49 | 40.94 / 49.78 / 58.39 |
| 256 | 192.58 / 227.57 / 237.42 | 172.34 / 226.31 / 230.64 |
| 512 | 416.36 / 458.10 / 464.17 | 419.15 / 473.53 / 486.39 |
| 1,024 | 1,134.11 / 1,557.02 / 1,600.13 | 1,087.45 / 1,176.15 / 1,287.92 |

These are 30 warm sequential iterations per length on the shared Docker Desktop CPU
host, including tokenization and array conversion. Initialization is separate in the
report. The capacity text has no semantic-quality label. There is no GPU, queueing,
network or workflow-completion claim, and the CPU target is unmet. Inputs over 1,024
tokens are rejected without truncation; this does not reproduce the vendor's
2,048-token sliding-window benchmark. Even zero errors on 24 benign examples would
not establish the proposed production false-block budget.

The [runner and reproduction commands](README.md#compact-pretrained-and-quantized-reference)
pin bytes and dependency versions. The quantized Docker diagnostic uses ten timing
iterations and introduces no new quality examples or tuned threshold. The report
retains every prediction, error family, source checksum, Pod/image identity and
resource limit. These exposed examples cannot become a fresh held-out set for a
later training or threshold-selection attempt. No SDK, gateway or backend policy
uses either export. G3's quality gate and learned G4/G8 promotion remain unmet.
