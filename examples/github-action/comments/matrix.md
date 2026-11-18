<!-- martian-evals-comment: examples/evals/matrix/evals.yaml -->
### Martian evals failed

Suite `examples/evals/matrix/evals.yaml`: 4 combinations of `model` and `prompt`, 6 cases each; 1 passed, 3 failed.

| Combination | Passed | Failed | Errors | Pass rate (95% CI) | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| `model=small-model,prompt=terse` | 4 | 2 | 0 | 67% (29–91%) | failed |
| `model=small-model,prompt=friendly` | 3 | 3 | 0 | 50% (18–82%) | failed |
| `model=large-model,prompt=terse` | 6 | 0 | 0 | 100% (60–100%) | passed |
| `model=large-model,prompt=friendly` | 5 | 1 | 0 | 83% (43–97%) | failed |

#### `model=small-model,prompt=terse`

Failed cases: `warranty` (`states_the_fact`), `payment` (`states_the_fact`).

#### `model=small-model,prompt=friendly`

Failed cases: `hours` (`brief`), `warranty` (`states_the_fact`), `payment` (`states_the_fact`).

#### `model=large-model,prompt=friendly`

Failed cases: `hours` (`brief`).

Commit `0123456` · [Workflow run](https://github.com/example/agent/actions/runs/1) · Updated on every push
