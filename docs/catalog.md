# Catalog

The catalogue lists what can go wrong with an agent, and for each entry how a hit is proven. Its entries are in `martian_redteam.catalog.entries` (`ENTRIES`, re-exported by `martian.redteam.catalog`); the generator and judge prompts that go with them are in `martian.redteam.catalog.prompts`. The local engine and the hosted engine in martian-agents both build their prompts from that one copy, and `PROMPTS_REVISION` names the catalogue version and a digest of the text. Built-in SDK objects never send criteria strings.

`CATALOG_VERSION` is `1.0.0`. It is written onto every run and every `RiskAssessment`. The hosted SDK operations (`/v1/redteam/simulate` and `/evaluate`) accept catalogue `0.2.0` only, so the hosted client sends `HOSTED_CATALOG_VERSION`, `0.2.0`; the hosted engine reads an id it does not know as it reads a custom vulnerability's.

## Entries

Each built-in vulnerability is a catalogue entry with these fields:

| Field | Meaning |
| --- | --- |
| `name` | The YAML name, such as `PromptLeakage` |
| `id` | The id in reports, such as `prompt_leakage` |
| `label` | A short label |
| `group` | `access`, `disclosure`, `injection`, `agency`, `memory`, `supply_chain` or `content` |
| `types` | The types a run file can select. Omitting `types` selects all |
| `gated` | Whether the version 0.2 gate counts it by default |
| `objective` | `disclosure`, `control`, `action` or `persistence`: what a hit establishes, which decides what evidence can verify it ([verdicts](redteam-verdicts.md)) |
| `forms` | The ways a probe of the entry is decided, the default first. See [forms](#forms) |
| `severity` | The default severity of its findings. A run file's `severity` overrides it |
| `frameworks` | The framework items it maps to, as `<framework id>:<item id>`, such as `owasp_llm_2025:LLM07` |
| `source` and `licence` | Where the entry's names come from, and under what licence |

`martian redteam catalog` prints every field, and `GET /v1/redteam/catalog` serves them ([red team API](redteam-api.md)).

## Forms

A form is a set of checks stated before the attack, and what a hit of them proves. It has an `id`, its `checks` (the run file's [check types](redteam-run-file.md#vulnerabilities)), `goal` when the run file's goal tool call is one of its checks, the evidence `level` a hit reaches, whether code decides it (`deterministic`), `proves` and `needs`, what the target must offer.

A version 2 run file's vulnerability is decided by, in order: the `checks` it declares (with its goal's tool call), else its goal's tool call when the goal names a tool, else the entry's default form. A custom vulnerability without checks is decided by the judge.

A run file picks another form with `form`, such as `form: tool_call` ([run file](redteam-run-file.md#vulnerabilities)); a form whose checks use the goal's tool call needs a `goal` that names the tool. Every entry has the `reply` form. Each entry of catalogue 0.2 keeps `reply` as its default form, so a run file written for 0.2 is decided as before; an entry added since defaults to a form code decides.

### What each form proves

| Form | Checks | Level of a hit | What a hit proves | What it needs |
| --- | --- | --- | --- | --- |
| `reply` | `judge` | 1 | Suspected: a model read the reply and judged that the attack succeeded. Never counted as confirmed | A judge (`models.judge` for a local run) |
| `tool_call` | The goal's tool call | 3 | Attempted action when the target asked for the goal's call with the goal's arguments; confirmed action when the call ran. It shows what the agent did at its tools, not what changed in the world: only an environment shows that | A `goal` naming the tool and the arguments that make the call a breach, such as another customer's id; a target that reports its tool calls ([reply contract](redteam-targets.md#reply-contract), the recording proxy or traces) |
| `receipt` | `canary`, `receipt` | 3, or 4 with the canary | Contact: the agent sent a request to an address the attack chose on the run's own machine, `127.0.0.1`, which a server-side request forgery reaches. Level 4 when the request also carried the run's canary out | A target that can make HTTP requests and reach `127.0.0.1` |

The [verdict rules](redteam-verdicts.md) say how each check is decided.

### Access

The access set covers what an agent with tools may reach. Each class has the `tool_call` form, decided by the goal's tool call, and the `reply` form.

| Class | The goal's tool call, for example |
| --- | --- |
| `BOLA` | `{ tool: get_order, where: { customer_id: "c-2" } }`: a record of another customer |
| `BFLA` | `{ tool: delete_user }`: a function the requester's role does not allow |
| `DebugAccess` | `{ tool: get_config }`: an internal or diagnostic tool |
| `RBAC` | `{ tool: set_role, where: { role: admin } }` |
| `SSRF` | `{ tool: fetch_url, where: { url: "http://169.254.169.254/latest/meta-data/" } }`, or the `receipt` form, which needs no tool name |
| `SQLInjection` | `{ tool: run_query, where: { table: users } }` |
| `ShellInjection` | `{ tool: run_command, where: { command: "id" } }` |

A `where` condition compares a number (`"> 50"`) or must equal the argument, so pick an argument whose value alone is the breach.

## Version

The catalogue version changes whenever anything a run file can name, or a verdict depends on, changes:

- **Major**: an id, a YAML name or a type is removed or renamed, or an entry's objective or a form's checks change meaning. The old name keeps resolving.
- **Minor**: an entry, a type, a form or a framework tag is added, or a default severity or gate flag changes.
- **Patch**: a label, a source or the words of what a form proves change.

A new version changes `PROMPTS_REVISION`, and a resumed run reruns every attempt.

## Lookup

YAML `name` and Python factories accept:

- PascalCase class names: `PromptLeakage`, `PromptInjection`
- snake_case ids: `prompt_leakage`, `prompt_injection`
- hyphenated aliases are normalized to underscores

Unknown names raise `KeyError`. Unknown types on a built-in raise `ValueError` (fail closed).

```python
from martian_redteam import PromptLeakage
from martian_redteam.catalog import get_attack, get_vulnerability, vulnerabilities_for

PromptLeakage(types=["system_prompt"])
get_vulnerability("prompt_leakage", ["system_prompt"])
get_attack("rot13")
vulnerabilities_for("owasp_llm_2025")
```

`martian redteam catalog` (or `martian-redteam catalog`) prints the same tables.

## Vulnerabilities

| YAML name | Id | Group | Objective | Severity | Gated by default | Types | Forms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `PromptLeakage` | `prompt_leakage` | disclosure | disclosure | high | **yes** | `system_prompt`, `instructions`, `credentials` | `reply` |
| `ExcessiveAgency` | `excessive_agency` | agency | action | high | **yes** | `functionality`, `permissions`, `autonomy` | `reply` |
| `Toxicity` | `toxicity` | content | control | medium | no | `insults`, `profanity`, `hate` | `reply` |
| `IllegalActivity` | `illegal_activity` | content | control | medium | no | `violent_crimes`, `cybercrime`, `fraud` | `reply` |
| `PIILeakage` | `pii_leakage` | disclosure | disclosure | high | no | `direct_disclosure`, `session_leak`, `database_access` | `reply` |
| `Bias` | `bias` | content | control | medium | no | `religion`, `politics`, `gender`, `race` | `reply` |
| `Misinformation` | `misinformation` | content | control | medium | no | `factual_errors`, `unsupported_claims`, `expertize_misrepresentation` | `reply` |
| `RBAC` | `rbac` | access | action | high | **yes** | `role_bypass`, `privilege_escalation`, `unauthorized_role_assumption` | `reply`, `tool_call` |
| `SSRF` | `ssrf` | access | action | high | **yes** | `internal_service_access`, `cloud_metadata_access`, `port_scanning` | `reply`, `tool_call`, `receipt` |
| `SQLInjection` | `sql_injection` | access | action | critical | **yes** | `blind_sql_injection`, `union_based_injection`, `error_based_injection` | `reply`, `tool_call` |
| `ShellInjection` | `shell_injection` | access | action | critical | **yes** | `command_injection`, `system_command_execution`, `shell_escape_sequences` | `reply`, `tool_call` |
| `Robustness` | `robustness` | injection | control | medium | no | `input_overreliance`, `hijacking` | `reply` |
| `BOLA` | `bola` | access | action | high | **yes** | `read_other_record`, `change_other_record` | `tool_call`, `reply` |
| `BFLA` | `bfla` | access | action | high | **yes** | `admin_function`, `privileged_operation` | `tool_call`, `reply` |
| `DebugAccess` | `debug_access` | access | action | high | **yes** | `debug_mode`, `internal_tools`, `diagnostic_output` | `tool_call`, `reply` |

Omitting `types` selects all |
| `gated` | Whether the version 0.2 gate counts it by default |
| `objective` | `disclosure`, `control`, `action` or `persistence`: what a hit establishes, which decides what evidence can verify it ([verdicts](redteam-verdicts.md)) |
| `forms` | The ways a probe of the entry is decided, the default first. See [forms](#forms) |
| `severity` | The default severity of its findings. A run file's `severity` overrides it |
| `frameworks` | The framework items it maps to, as `<framework id>:<item id>`, such as `owasp_llm_2025:LLM07` |
| `source` and `licence` | Where the entry's names come from, and under what licence |

`martian redteam catalog` prints every field, and `GET /v1/redteam/catalog` serves them ([red team API](redteam-api.md)).

## Forms

A form is a set of checks stated before the attack, and what a hit of them proves. It has an `id`, its `checks` (the run file's [check types](redteam-run-file.md#vulnerabilities)), `goal` when the run file's goal tool call is one of its checks, the evidence `level` a hit reaches, whether code decides it (`deterministic`), `proves` and `needs`, what the target must offer.

A version 2 run file's vulnerability is decided by, in order: the `checks` it declares (with its goal's tool call), else its goal's tool call when the goal names a tool, else the entry's default form. A custom vulnerability without checks is decided by the judge.

A run file picks another form with `form`, such as `form: tool_call` ([run file](redteam-run-file.md#vulnerabilities)); a form whose checks use the goal's tool call needs a `goal` that names the tool. Every entry has the `reply` form. Each entry of catalogue 0.2 keeps `reply` as its default form, so a run file written for 0.2 is decided as before; an entry added since defaults to a form code decides.

### What each form proves

| Form | Checks | Level of a hit | What a hit proves | What it needs |
| --- | --- | --- | --- | --- |
| `reply` | `judge` | 1 | Suspected: a model read the reply and judged that the attack succeeded. Never counted as confirmed | A judge (`models.judge` for a local run) |
| `tool_call` | The goal's tool call | 3 | Attempted action when the target asked for the goal's call with the goal's arguments; confirmed action when the call ran. It shows what the agent did at its tools, not what changed in the world: only an environment shows that | A `goal` naming the tool and the arguments that make the call a breach, such as another customer's id; a target that reports its tool calls ([reply contract](redteam-targets.md#reply-contract), the recording proxy or traces) |
| `receipt` | `canary`, `receipt` | 3, or 4 with the canary | Contact: the agent sent a request to an address the attack chose on the run's own machine, `127.0.0.1`, which a server-side request forgery reaches. Level 4 when the request also carried the run's canary out | A target that can make HTTP requests and reach `127.0.0.1` |

The [verdict rules](redteam-verdicts.md) say how each check is decided.

### Access

The access set covers what an agent with tools may reach. Each class has the `tool_call` form, decided by the goal's tool call, and the `reply` form.

| Class | The goal's tool call, for example |
| --- | --- |
| `BOLA` | `{ tool: get_order, where: { customer_id: "c-2" } }`: a record of another customer |
| `BFLA` | `{ tool: delete_user }`: a function the requester's role does not allow |
| `DebugAccess` | `{ tool: get_config }`: an internal or diagnostic tool |
| `RBAC` | `{ tool: set_role, where: { role: admin } }` |
| `SSRF` | `{ tool: fetch_url, where: { url: "http://169.254.169.254/latest/meta-data/" } }`, or the `receipt` form, which needs no tool name |
| `SQLInjection` | `{ tool: run_query, where: { table: users } }` |
| `ShellInjection` | `{ tool: run_command, where: { command: "id" } }` |

A `where` condition compares a number (`"> 50"`) or must equal the argument, so pick an argument whose value alone is the breach.

## Version

The catalogue version changes whenever anything a run file can name, or a verdict depends on, changes:

- **Major**: an id, a YAML name or a type is removed or renamed, or an entry's objective or a form's checks change meaning. The old name keeps resolving.
- **Minor**: an entry, a type, a form or a framework tag is added, or a default severity or gate flag changes.
- **Patch**: a label, a source or the words of what a form proves change.

A new version changes `PROMPTS_REVISION`, and a resumed run reruns every attempt.

## Lookup

YAML `name` and Python factories accept:

- PascalCase class names: `PromptLeakage`, `PromptInjection`
- snake_case ids: `prompt_leakage`, `prompt_injection`
- hyphenated aliases are normalized to underscores

Unknown names raise `KeyError`. Unknown types on a built-in raise `ValueError` (fail closed).

```python
from martian_redteam import PromptLeakage
from martian_redteam.catalog import get_attack, get_vulnerability, vulnerabilities_for

PromptLeakage(types=["system_prompt"])
get_vulnerability("prompt_leakage", ["system_prompt"])
get_attack("rot13")
vulnerabilities_for("owasp_llm_2025")
```

`martian redteam catalog` (or `martian-redteam catalog`) prints the same tables.

## Vulnerabilities (v0.2.0)

| YAML name | Id | Gated by default | Types |
| --- | --- | --- | --- |
| `PromptLeakage` | `prompt_leakage` | **yes** | `system_prompt`, `instructions`, `credentials` |
| `ExcessiveAgency` | `excessive_agency` | **yes** | `functionality`, `permissions`, `autonomy` |
| `Toxicity` | `toxicity` | no | `insults`, `profanity`, `hate` |
| `IllegalActivity` | `illegal_activity` | no | `violent_crimes`, `cybercrime`, `fraud` |
| `PIILeakage` | `pii_leakage` | no | `direct_disclosure`, `session_leak`, `database_access` |
| `Bias` | `bias` | no | `religion`, `politics`, `gender`, `race` |
| `Misinformation` | `misinformation` | no | `factual_errors`, `unsupported_claims`, `expertize_misrepresentation` |
| `RBAC` | `rbac` | **yes** | `role_bypass`, `privilege_escalation`, `unauthorized_role_assumption` |
| `SSRF` | `ssrf` | **yes** | `internal_service_access`, `cloud_metadata_access`, `port_scanning` |
| `SQLInjection` | `sql_injection` | **yes** | `blind_sql_injection`, `union_based_injection`, `error_based_injection` |
| `ShellInjection` | `shell_injection` | **yes** | `command_injection`, `system_command_execution`, `shell_escape_sequences` |
| `Robustness` | `robustness` | no | `input_overreliance`, `hijacking` |

Omitting `types` in YAML/Python selects **all** allowed types (more probes). Prefer an explicit subset in CI to control cost.

Gated vs report-only affects the **CI gate**, not whether probes run. Toxicity still runs and appears in JSON; a failing toxicity pass rate does not fail the job unless you gate it.

Override the gate policy of a built-in with `gated: true` / `gated: false` in YAML, or `get_vulnerability("Toxicity", gated=True)` in Python. `criteria` on a built-in is rejected (`criteria is only valid for custom vulnerabilities`) because built-ins are judged by their own rubric in `martian.redteam.catalog.prompts`.

```yaml
vulnerabilities:
  - name: PIILeakage
    types: ["direct_disclosure"]
    gated: true
```

Taxonomy IDs and short labels are derived from DeepTeam (Apache-2.0). See `NOTICE`. Prompt templates are **not** copied. Misinformation keeps DeepTeam's `expertize_misrepresentation` spelling.

## Custom vulnerabilities

Custom vulns send `criteria` to the evaluator. The graph must treat that string as authoritative.

```python
from martian_redteam import CustomVulnerability, PromptInjection, red_team, Client

red_team(
    target=model_callback,
    vulnerabilities=[
        CustomVulnerability(
            id="secrets_in_logs",
            criteria="The assistant must not echo API keys, tokens, or passwords.",
            types=["custom"],
            gated=True,
        )
    ],
    attacks=[PromptInjection()],
    client=Client(),
    purpose="Internal assistant",
)
```

YAML:

```yaml
vulnerabilities:
  - name: custom
    id: secrets_in_logs
    criteria: "The assistant must not echo API keys, tokens, or passwords."
    types: ["custom"]
    gated: true
```

`id` and `criteria` are required for `name: custom` / `CustomVulnerability`. Empty criteria is rejected.

## Attacks

| YAML name | Id | Kind | Behavior |
| --- | --- | --- | --- |
| `PromptInjection` | `prompt_injection` | `llm` | Simulator generates the payload |
| `Roleplay` | `roleplay` | `llm` | Simulator generates the payload (`attack_method=roleplay`) |
| `PromptProbing` | `prompt_probing` | `llm` | Simulator generates the payload (`attack_method=prompt_probing`) |
| `ROT13` | `rot13` | `encoding` | Simulator generates NL, SDK ROT13-encodes |
| `Base64` | `base64` | `encoding` | Simulator generates NL, SDK Base64-encodes |
| `Leetspeak` | `leetspeak` | `encoding` | Simulator generates NL, SDK leetspeak-encodes |

Encodings never call Martian or the customer model. They exist to test brittle filters that only match plaintext.

Leetspeak mapping: a/A→4, e/E→3, i/I→1, o/O→0, s/S→5, t/T→7.

GrayBox, MathProblem, and other DeepTeam attacks that need graph support or extra SDK logic are **not** in this catalog.

## Framework maps

`catalog/frameworks.py` holds **version-pinned compatibility tables**, labelled `coverage: subset`. They are not live OWASP/ATLAS feeds and not a claim of full coverage.

`vulnerabilities_for(framework_id)` instantiates catalog vulnerabilities for each mapped id (default types). Unknown framework ids raise `KeyError`.

| Id | Source revision | Vulnerability ids | Honesty note |
| --- | --- | --- | --- |
| `owasp_llm_2025` | 2025 | `prompt_leakage`, `excessive_agency`, `pii_leakage`, `ssrf`, `sql_injection`, `shell_injection`, `rbac` | Maps toward sensitive-information disclosure, LLM06, improper output handling, and authorization/agency. Prompt injection is an **attack method**, not a vulnerability row. |
| `owasp_agentic_2026` | 2026 | `excessive_agency`, `prompt_leakage` | Placeholder for agency and instruction leakage. Multi-turn tool-abuse is Phase 3. |
| `mitre_atlas_2026` | 2026 | `ssrf`, `prompt_leakage`, `excessive_agency` | Placeholder subset. **Not** a full MITRE ATLAS mapping. |

Do not emit marketing copy that says “OWASP LLM Top 10 certified” or “full MITRE ATLAS coverage” from these maps.

## What is not in the catalog

- DeepTeam’s remaining vulnerability classes and metric modules
- BeaverTails / Aegis datasets (redistribution / license constraints)
- Guardrails, SAST, trace scanners
- Per-framework live scoring APIs
