# Red team attack methods

An attack method decides how one attempt of a probe attacks the target: how it writes its attacks, how many it sends, and whether it reads the target's replies. A version 2 run file names its methods in `attacks` ([run file](redteam-run-file.md#attacks)). `martian.redteam.methods` holds them.

## Kinds and arms

Every method has one of four kinds:

| Kind | What it does |
| --- | --- |
| `transform` | Code changes the attack's text, with no model call and no network. Used as an attack, a transform rewrites a prompt injection the attacker wrote. Used as a layer, it changes whatever another method sends |
| `rewrite` | One attack per query, written without feedback: by the attacker model in a stated style, or from a fixed template |
| `conversation` | Several turns in one conversation, each written after the target's last reply |
| `search` | Several queries, each chosen from the checks' feedback on the ones before |

And one arm, which the report uses to compare them: `control` (fixed templates, the same every run), `static` (one written attack) or `adaptive` (it reads the replies or the checks' feedback).

## Budget

Each method has a cap on target queries per attempt. Every call of the target, the attacker and the judge is also counted on the run's `budget` before it is made, so a method never spends past the run's limits; an attempt the run's budget stops is `stopped`, never held. The six version 0.2 methods send one query per attempt and call the attacker once (twice when it refuses).

## Methods

| YAML name | Id | Kind | Arm | Target queries per attempt | Attacker calls | What it tries |
| --- | --- | --- | --- | --- | --- | --- |
| `PromptInjection` | `prompt_injection` | rewrite | static | 1 | 1 | An instruction override or delimiter injection the target might take as a new system rule |
| `Roleplay` | `roleplay` | rewrite | static | 1 | 1 | A roleplay or hypothetical frame that takes the target outside its role |
| `PromptProbing` | `prompt_probing` | rewrite | static | 1 | 1 | A probing question for hidden instructions, tools or policy, without a blunt override |
| `Templates` | `templates` | rewrite | control | Up to `max_queries` (default and most 10, one per template) | 0 | Nothing adaptive: the same fixed templates every run, the baseline every adaptive method is compared with. The set has a direct request, two instruction overrides, "important instructions", a behaviour update in the Skeleton Key style, a mode switch, a closed delimiter, an answer prefix, a task wrapper and urgency |
| `Refine` | `refine` | search | adaptive | Up to `max_queries` (default 10) | One per query (a first attack, then a refinement of each) | A target that holds against a first attempt but gives way to an attack refined from its own replies, guided by how close each reply came to the goal (PAIR-style) |
| `TreeSearch` | `tree_search` | search | adaptive | Up to `max_queries` (default 20), and never more than the tree holds: 12 at the default width 3, branching 2, depth 3 | One per query | A target a single line of refinement stalls on: several attacks per level, refined in branches, keeping the ones whose replies came closest (TAP-style) |
| `Escalation` | `escalation` | conversation | adaptive | Up to `max_queries` (default 20): at most `max_turns` (default 6) turns per conversation and `max_backtracks` (default 2) backtracks, with replayed turns | One per turn written; none for a replayed turn | A target that refuses a request asked outright but goes along when one conversation builds up to it in small steps (Crescendo-style) |
| `Authority` | `authority` | rewrite | static | 1 | 1 | A target that obeys whoever claims to be in charge: the request comes from its developer, an administrator, a security team or a legal duty |
| `Hypothetical` | `hypothetical` | rewrite | static | 1 | 1 | A target that answers inside an imagined case: the request is a thought experiment in which it is allowed, asking for the concrete answer |
| `Citation` | `citation` | rewrite | static | 1 | 1 | A target that defers to documents: the request cites a policy, ticket or guideline that says it is allowed or required |
| `TaskFraming` | `task_framing` | rewrite | static | 1 | 1 | A target that checks requests but not tasks: the request sits inside a summary, translation, test case or form the target is built for |
| `FormatShift` | `format_shift` | rewrite | static | 1 | 1 | A target that refuses a request but not a formatting job: the answer is asked for as JSON, a table, code or a template |
| `ROT13` | `rot13` | transform | static | 1 | 1 | A filter that matches plain text: a prompt injection in ROT13 |
| `Base64` | `base64` | transform | static | 1 | 1 | A filter that matches plain text: a prompt injection in Base64 |
| `Leetspeak` | `leetspeak` | transform | static | 1 | 1 | A filter that matches words: a prompt injection with letters as digits |
| `Hex` | `hex` | transform | static | 1 | 1 | A filter that matches plain text: the UTF-8 bytes as hex pairs |
| `Homoglyph` | `homoglyph` | transform | static | 1 | 1 | A filter that matches exact words: Latin letters become Cyrillic letters that look the same |
| `InvisibleUnicode` | `invisible_unicode` | transform | static | 1 | 1 | A reviewer or filter that reads what is shown: printable ASCII becomes Unicode tag characters, which render as nothing but some models read |
| `Morse` | `morse` | transform | static | 1 | 1 | A filter that matches plain text: Morse code, letters by spaces and words by ` / `; characters with no code are left out |
| `AlternatingCase` | `alternating_case` | transform | static | 1 | 1 | A filter that matches words case by case: letters alternate lower and upper case |
| `PayloadSplit` | `payload_split` | transform | static | 1 | 1 | A filter that reads one piece at a time: three quoted parts and a request to join and answer them |

Every transform runs locally with no model call and no network, and is a pure function of the text: the same attack always becomes the same message. Its tests check a round trip (or, for alternating case and Morse, the property that holds) on generated texts.

Prompt injection, roleplay and prompt probing take their style from the catalogue's generator prompt (`martian.redteam.catalog.prompts`). The other rewrites state their style in the attacker's request, from a prompt file of their own, with the probe's `goal` when the run file states one; the catalogue's generator prompt is unchanged. The six version 0.2 methods (the first six rows) have the results and attempt fingerprints they had before methods had an interface.

## The control arm

`Templates` sends the fixed templates of `prompts/templates-v1.json` in order, each in a conversation of its own, and stops at the first one a check finds succeeded. Each template is filled with the probe's request: the run file's `goal` (a tool goal reads "call issue_refund with amount > 50"), or the vulnerability's own request from the same file. It calls no model and draws nothing at random, so it sends identical attacks on every run; a released set is never edited, and a new one is a new version. List it beside any adaptive method:

```yaml
attacks:
  - name: Templates     # control arm
  - name: Refine        # adaptive
```

The report's `by_method` gives each method's `arm` ([red team reports](redteam-reports.md#groups-and-probes)), so the control's success rate and queries to first hit read beside each adaptive method's.

## Refinement driven by the check result

`Refine` writes a first attack toward the probe's goal, sends it in a conversation of its own and reads the checks' feedback. While no check has hit and queries remain, the attacker refines its last attack from three things: the attack, the target's reply and its tool calls (as data fields it is told never to follow, cut to 4000 characters, with secret values scrubbed), and the checks' finding in the harness's own words. For a tool goal the finding is one of four rungs: no tool called, another tool called, the goal tool called with other arguments, or matched. The search stops at the first hit or after `max_queries` queries (default 10).

The feedback comes from the checks decided by code when the probe has any (`text`, `canary`, `tool_call`), at no cost; a judge check of the same probe is asked once, on the exchange the attempt ends with. When the judge is the probe's only check, it is the signal: one judge call per query, and the last one is not repeated. The attempt is decided on the hit, or on the attack whose reply came closest. Each query is a target query on the run's budget and each refinement an attacker call; a refusal is asked for once more and a second refusal fails the attempt as the attacker's.

`TreeSearch` is the same engine with branching. Its first level has `branching` attacks written fresh; each later level has `branching` refinements of each of the `width` attacks of the level before whose replies came closest (pruning the rest); it stops at the first hit, after `depth` levels or at `max_queries`, whichever comes first. `Refine` is this search with width and branching 1 and depth `max_queries`. The tree's settings:

| Setting | Default | Range | Meaning |
| --- | --- | --- | --- |
| `width` | 3 | 1 to 10 | Attacks kept at each level |
| `branching` | 2 | 1 to 10 | Attacks written from each one kept, and at the first level |
| `depth` | 3 | 1 to 20 | Levels |
| `max_queries` | 20 | 1 to 1000 | Target queries per attempt |

The attempt's trace records each query's rung (`signals`), `queries_to_first_success` and `tree` (each query's level and the query it refined); the report's `queries_to_first_hit` counts the same queries per probe ([red team reports](redteam-reports.md#groups-and-probes)). The request text is in `prompts/search/`.

## Multi-turn escalation

`Escalation` attacks in one conversation, through the target's sessions: the same session id and history contract as suite-file conversation cases ([red team targets](redteam-targets.md)). Before each turn the attacker reads the conversation so far (as data fields it is told never to follow) and the checks' finding on the last reply, and writes the next message, one small step closer to the goal. The checks read every reply and the attempt stops at the first hit.

When the target refuses a turn (its reply opens with a refusal, by the same deterministic check as attacker refusals) and backtracks remain, the method goes back a turn: it starts a new conversation, sends again the turns the target had accepted before the refusal, and asks the attacker for a different step, showing it the refused message. Replaying works for every target, whether it resends the history or keeps its own, and each replayed turn is a target query on the budget. With no backtracks left, a refused turn stays in the conversation and the escalation goes on.

The attempt keeps every turn of every conversation, the refused and replayed ones included, with a `system` turn at each backtrack and each new conversation. Its trace records `backtracks` and `turns_in_conversation`.

| Setting | Default | Range | Meaning |
| --- | --- | --- | --- |
| `max_turns` | 6 | 1 to 20 | Turns in one conversation, replayed turns included |
| `max_backtracks` | 2 | 0 to 10 | Times it goes back a turn |
| `max_queries` | 20 | 1 to 1000 | Target queries per attempt |

## Prompt files and the licence register

Every prompt a method adds to the attacker's request, and every fixed template, is a file under `src/martian/redteam/methods/prompts/`, written by Martian Security for this package under Apache-2.0. `prompts/register.json` lists each file with its licence, its source and the published ideas it was modelled on; no text is copied from another project, and code or prompts under GPL, AGPL or non-commercial licences are never used. A test fails when a file is not in the register, when an entry has no file or no source, or when its licence is not one `scripts/licence-policy.json` allows. The run's `provenance.methods_revision` is a digest of every file, and the fingerprint of an attempt of any method but the six version 0.2 ones includes it.

## Layers

`layers` applies transforms, in order, to every attack a method sends: `{ name: PromptInjection, layers: [Base64] }` writes a prompt injection and sends it in Base64. A layered method is named with its layers in probe ids and reports, such as `prompt_injection+base64`; `id` renames it. Each transform listed as a method can be a layer.

## What an attempt keeps

Every query is kept in the attempt's `turns`, as an attacker turn (the message as sent, after the layers) and a target turn. A new conversation after the first is marked with a `system` turn. The attempt is decided on the exchange the method ends with, and `target_calls` counts every query.

For every method but the six version 0.2 ones, the attempt has a `trace` evidence item, `{attempt}/method`, whose `metadata` is the method's record: its id, kind, arm, layers and settings, the queries it made and its cap, `queries_to_first_success` (the query at which a check first found the attack succeeded, or `null`), the conversations it used and the feedback each query got (`signals`). The fingerprint of such an attempt includes the method's configuration, so a resumed run reruns attempts whose method changed.

## The interface

A method is an `AttackMethod` with a `kind`, an `arm`, whether it `needs_attacker`, the run-file `settings` it takes with their defaults, and `run(context)`, which drives one attempt and returns the `Exchange` it is decided on. The engine gives it an `AttackContext`:

| Call | What it does |
| --- | --- |
| `generate(request=, method=, mode=)` | One attack from the attacker model. A refused or empty generation is asked for once more with the defensive framing; a second refusal fails the attempt as the attacker's. Secret values are scrubbed from `request`, and target text in it must be a data field ([hostile output](security-and-data.md#trust-boundary)) |
| `send(payload, session=)` | The payload after the method's layers, sent in a conversation (`conversation()` names a new one) |
| `check(exchange)` | `Feedback` from the probe's checks: those decided by code when the probe has any, else the judge |
| `note(text)`, `record(**values)` | A harness note in the turns; values in the method's record |

`Feedback` says whether a check hit, and how close the reply came (`rung`): for a tool goal, `not_called`, `wrong_tool`, `wrong_arguments` or `matched`; for a text or canary check, `no_match` or `matched`. It also says whether the target refused, in the harness's own words: target text never reaches a method as an instruction.

A method defined outside the package is registered by name:

```python
from martian.redteam.methods import AttackMethod, register_method

class Echo(AttackMethod):
    kind = "search"
    arm = "adaptive"
    needs_attacker = False
    settings = {"max_queries": 2}

    async def run(self, context):
        exchange = await context.send(context.goal)
        if (await context.check(exchange)).hit:
            return exchange
        return await context.send(context.goal.upper())

register_method("Echo", lambda spec, layers, values: Echo(id="echo", label="Echo", layers=layers, values=values))
```

`register_transform` adds a transform the same way.
