# How it works

1. Load YAML/environment settings and resolve the callback file relative to the configuration file.
2. Expand vulnerability types × attacks × repeat count, bounded by max_concurrent. Attack weights are stored but are not a sampling policy in this implementation.
3. Ask simulator for an attack using vulnerability/type/purpose/attack_method context. Apply local encoding for the corresponding attack.
4. Invoke the local callback with the prompt. It returns str or Turn; this implementation passes turns=None when supported.
5. Send prompt/response context to evaluator. Expect a binary score and nonempty reason. Parse protocol output and retain graph revision where supplied.
6. Record scored probes, infrastructure errors and target errors separately; aggregate per vulnerability type.
7. The CLI writes risk-assessment.json and risk-assessment.md, then applies the configured gate. A failed run still leaves reports.

Defaults fail on infrastructure/schema errors, do not ignore target errors, and require 1.0 pass rate for gated types. API/configuration options can change policies; inspect the actual settings. The Python red_team/a_red_team APIs return an assessment; call evaluate_gate explicitly if a Python integration requires enforcement.

[README](../README.md) · [Architecture](architecture.md) · [Usage](usage.md)
