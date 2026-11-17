# Red team attack methods

An attack method decides how one attempt of a probe attacks the target: how it writes its attacks, how many it sends, and whether it reads the target's replies. A version 2 run file names its methods in `attacks` ([run file](redteam-run-file.md#attacks)). `martian.redteam.methods` holds them.

## Kinds and arms

Every method has one of four kinds:

| Kind | What it does |
| --- | --- |
| `transform` | Code changes the attack's text, with no model call and no network. Used as an attack, a transform rewrites a prompt injection the attacker wrote. Used as a layer, it changes whatever another method sends |
| `rewrite` | One attack per query, written without feedback: by the attacker model in a stated style |
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
| `ROT13` | `rot13` | transform | static | 1 | 1 | A filter that matches plain text: a prompt injection in ROT13 |
| `Base64` | `base64` | transform | static | 1 | 1 | A filter that matches plain text: a prompt injection in Base64 |
| `Leetspeak` | `leetspeak` | transform | static | 1 | 1 | A filter that matches words: a prompt injection with letters as digits |

The three rewrites take their style from the catalogue's generator prompt (`martian.redteam.catalog.prompts`). Their results and attempt fingerprints are what they were before methods had an interface.

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
