"""A panel of judges: several judge models grade one check, and the strict majority of those that
gave a verdict decides it.

With one judge, that judge's error is the verdict. A panel names from
:data:`~martian.evals.model.MIN_PANEL` to :data:`~martian.evals.model.MAX_PANEL` different judge
models, each of which grades the case on its own through the same judge code as a single judge:
an SDK judge (:mod:`martian.evals.judges`, :mod:`martian.evals.grounding`) in a suite file or in
Python, or the evals core worker's judge for a registered judge. Their verdicts are then decided by
one rule, :data:`RULE`:

- **A member that errors does not vote.** Its error is kept as its reason.
- **More than half of the members must vote.** With fewer, the panel has too few votes: one judge
  of three deciding alone is the single judge a panel is there to replace.
- **More than half of the votes must agree.** A tie has no verdict.
- **No majority is an error, never a pass** (:data:`NO_MAJORITY`), so a gate fails closed.

So a panel of three in which one judge errors still decides when the other two agree, and a panel
of four needs at least three votes, two of three or three of four agreeing. The rule reads only
pass/fail verdicts: a member whose value is anything else gives no verdict.

The decision and every member's vote are recorded as flat metadata of strings
(:meth:`Decision.metadata`), on the MLflow feedback, a suite-file run's ``results.jsonl`` and its
scores, and the core's scores alike:

| Key | Value |
| --- | --- |
| ``martian.panel.rule`` | :data:`RULE` |
| ``martian.panel.size`` | The number of members |
| ``martian.panel.passed``, ``failed``, ``errors`` | How many members passed, failed and gave no verdict |
| ``martian.panel.verdict`` | ``pass``, ``fail`` or ``none`` |
| ``martian.panel.<n>.model`` | Member n's model URI, from 1 in the panel's order |
| ``martian.panel.<n>.endpoint`` | Its endpoint, when it names one |
| ``martian.panel.<n>.model_version`` | The model version its provider reported, when it reported one |
| ``martian.panel.<n>.verdict`` | ``pass``, ``fail`` or ``error`` |
| ``martian.panel.<n>.reason`` | Its reason, or why it gave no verdict |

A panel's feedback also keeps the tokens its members used, added up over the members that gave a
verdict, under the keys a judge's feedback keeps its usage in (``mlflow.assessment.judgeInputTokens``
and ``judgeOutputTokens``, as text), so ``max_judge_tokens`` counts every member; when a member that gave a
verdict reported no usage, the panel reports none either.

A panel's score names the panel as its source, ``panel:`` and a digest of its members
(:func:`panel_source`), the same for the same members in any order.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from martian.evals._digits import ascii_digits
from martian.evals.limits import INPUT_TOKENS, OUTPUT_TOKENS
from martian.evals.model import MAX_PANEL, MIN_PANEL, digest

if TYPE_CHECKING:
    from mlflow.entities import Feedback
    from mlflow.genai.scorers import Scorer

RULE = "strict-majority-v1"
"""The rule a panel decides by: the strict majority of the members that gave a verdict, from more
than half of the members."""

NO_MAJORITY = "judge_panel_no_majority"
"""The error code of a panel that reached no majority."""

PREFIX = "martian.panel."
"""The prefix of every metadata key a panel records."""

REASON_LENGTH = 2000
"""The most characters of a member's reason that are recorded."""

JUDGE_VERSION_KEY = "martian.judge.version"
MODEL_VERSION_KEY = "martian.judge.model_version"
"""The judges' metadata keys of their own version and of the model version the provider reported."""


def check_models(models: Sequence[str]) -> list[str]:
    """A panel's model URIs, checked: from MIN_PANEL to MAX_PANEL different nonempty strings."""
    if isinstance(models, str) or not isinstance(models, Sequence):
        raise ValueError("a panel is a list of judge model URIs")
    found = list(models)
    if not all(isinstance(model, str) and model.strip() for model in found):
        raise ValueError("each judge model of a panel is a nonempty model URI")
    if not MIN_PANEL <= len(found) <= MAX_PANEL:
        raise ValueError(f"a panel names {MIN_PANEL} to {MAX_PANEL} judge models")
    if len(set(found)) != len(found):
        raise ValueError("a panel names each judge model once")
    return found


def panel_source(members: Iterable[tuple[str, str | None]]) -> str:
    """The ``source_id`` of a panel's score: ``panel:`` and the first 12 hex digits of a digest of
    its members' models and endpoints, sorted, so the same members in any order give the same."""
    canonical = sorted([model, endpoint or ""] for model, endpoint in members)
    return f"panel:{digest(canonical)[:12]}"


@dataclass(frozen=True)
class Vote:
    """One member's vote: ``passed`` is its verdict, or None when it gave none. ``reason`` is its
    reason, or why it gave no verdict."""

    model: str
    passed: bool | None
    reason: str = ""
    endpoint: str | None = None
    model_version: str = ""


@dataclass(frozen=True)
class Decision:
    """A panel's verdict from its members' votes, in the panel's order. ``passed`` is None when
    there is no majority, and ``error`` then says why."""

    votes: tuple[Vote, ...]
    passed: bool | None
    error: str = ""

    @property
    def counts(self) -> tuple[int, int, int]:
        """How many members passed, failed and gave no verdict."""
        passed = sum(vote.passed is True for vote in self.votes)
        failed = sum(vote.passed is False for vote in self.votes)
        return passed, failed, len(self.votes) - passed - failed

    def metadata(self) -> dict[str, str]:
        """The decision and every vote as flat metadata (the table in the module's docstring)."""
        passed, failed, errors = self.counts
        found = {
            f"{PREFIX}rule": RULE,
            f"{PREFIX}size": str(len(self.votes)),
            f"{PREFIX}passed": str(passed),
            f"{PREFIX}failed": str(failed),
            f"{PREFIX}errors": str(errors),
            f"{PREFIX}verdict": _verdict(self.passed, none="none"),
        }
        for number, vote in enumerate(self.votes, 1):
            key = f"{PREFIX}{number}."
            found[f"{key}model"] = vote.model
            if vote.endpoint:
                found[f"{key}endpoint"] = vote.endpoint
            if vote.model_version:
                found[f"{key}model_version"] = vote.model_version
            found[f"{key}verdict"] = _verdict(vote.passed, none="error")
            found[f"{key}reason"] = vote.reason[:REASON_LENGTH]
        return found

    @property
    def rationale(self) -> str:
        """The decision in words, then each member's vote and reason."""
        passed, failed, errors = self.counts
        outcome = _verdict(self.passed, none="no majority")
        lines = [
            f"Panel of {len(self.votes)} judges ({RULE}): {passed} passed, {failed} failed, "
            f"{errors} gave no verdict. Verdict: {outcome}."
        ]
        for number, vote in enumerate(self.votes, 1):
            said = "passed" if vote.passed is True else "failed" if vote.passed is False else "gave no verdict"
            lines.append(f"{number}. {vote.model} {said}: {vote.reason[:REASON_LENGTH]}")
        return "\n".join(lines)


def _verdict(passed: bool | None, *, none: str) -> str:
    return "pass" if passed is True else "fail" if passed is False else none


def decide(votes: Sequence[Vote]) -> Decision:
    """The panel's verdict by :data:`RULE`: the strict majority of the members that gave a verdict,
    when more than half of the members gave one; otherwise no verdict, with the reason."""
    found = tuple(votes)
    passed = sum(vote.passed is True for vote in found)
    failed = sum(vote.passed is False for vote in found)
    voted = passed + failed
    if 2 * voted <= len(found):
        unheard = "; ".join(f"{vote.model}: {vote.reason}" for vote in found if vote.passed is None)
        error = (
            f"Judge panel has no majority [{NO_MAJORITY}]: {voted} of {len(found)} judges gave a verdict, "
            f"and more than half must. {unheard}"
        ).strip()
        return Decision(found, None, error)
    if 2 * passed > voted:
        return Decision(found, True)
    if 2 * failed > voted:
        return Decision(found, False)
    error = f"Judge panel has no majority [{NO_MAJORITY}]: {passed} passed and {failed} failed, a tie."
    return Decision(found, None, error)


def is_panel(metadata: Mapping[str, Any]) -> bool:
    """Whether a score's metadata is a panel's."""
    return f"{PREFIX}size" in metadata


def member_sources(metadata: Mapping[str, Any]) -> list[str]:
    """The members of a panel's score, each as a single judge's score names its model:
    ``model@model_version``, or the model alone when its provider reported no version. Empty for a
    score that is not a panel's, and for one whose size is not written in the digits 0-9: an
    application writes a live trace's score metadata, so it is read as outside input."""
    if not is_panel(metadata):
        return []
    size = str(metadata[f"{PREFIX}size"])
    if not ascii_digits(size):
        return []
    found = []
    for number in range(1, int(size) + 1):
        model = str(metadata.get(f"{PREFIX}{number}.model", ""))
        version = str(metadata.get(f"{PREFIX}{number}.model_version", ""))
        if model:
            found.append(f"{model}@{version}" if version else model)
    return found


# --- the Python API ------------------------------------------------------------------------------


def _member_vote(model: str, member: Any, call: dict[str, Any]) -> tuple[Vote, str, tuple[int, int] | None]:
    """One member's vote on one case, the judge version it reported, if any, and the tokens its
    provider reported it used, input and output, if it reported both. ``member`` is an MLflow
    scorer; ``Scorer.run`` gives it the arguments it takes, as MLflow's evaluation does."""
    from mlflow.entities import Feedback

    try:
        feedback = member.run(**call)
    except ValidationError:
        # Pydantic's message holds the text it was given.
        return Vote(model, None, "The judge's reply could not be read"), "", None
    except ValueError as error:
        # The SDK judges' own messages: fixed text naming the setting to check, never the
        # provider's error text or the graded text.
        return Vote(model, None, str(error) or "The judge gave no verdict"), "", None
    except Exception as error:  # noqa: BLE001 - a member that fails in any way does not vote
        return Vote(model, None, f"The judge failed: {type(error).__name__}"), "", None
    if not isinstance(feedback, Feedback):
        feedback = Feedback(value=feedback)
    metadata = dict(feedback.metadata or {})
    version = str(metadata.get(MODEL_VERSION_KEY, ""))
    if feedback.error is not None or type(feedback.value) is not bool:
        reason = feedback.error_message if feedback.error is not None else "The judge gave no pass/fail verdict"
        return Vote(model, None, str(reason or "The judge gave no verdict"), model_version=version), "", None
    vote = Vote(model, feedback.value, feedback.rationale or "", model_version=version)
    sent, received = metadata.get(INPUT_TOKENS), metadata.get(OUTPUT_TOKENS)
    try:
        usage = (int(sent), int(received)) if sent is not None and received is not None else None
    except (TypeError, ValueError):
        usage = None
    return vote, str(metadata.get(JUDGE_VERSION_KEY, "")), usage


def _usage(voted: Sequence[tuple[Vote, str, tuple[int, int] | None]]) -> dict[str, str]:
    """The tokens the members that gave a verdict used, added up, under the keys a judge's
    feedback keeps its usage in (:mod:`martian.evals.limits`), so a run's judge token limit counts
    every member a panel asked; as text, like the rest of a panel's metadata. None when no member
    gave a verdict, or when one that did reported no usage: the panel's usage is then unknown, as
    a single judge's is when it reports none."""
    counted = [usage for vote, _, usage in voted if vote.passed is not None]
    if not counted or any(usage is None for usage in counted):
        return {}
    return {
        INPUT_TOKENS: str(sum(usage[0] for usage in counted if usage is not None)),
        OUTPUT_TOKENS: str(sum(usage[1] for usage in counted if usage is not None)),
    }


def judge_panel(
    judge: Callable[..., Scorer],
    *,
    models: Sequence[str],
    name: str | None = None,
    **options: Any,
) -> Scorer:
    """A panel of judges as one MLflow scorer: ``judge`` built once for each of ``models``, each
    grading every case on its own, decided by :data:`RULE`.

    ``judge`` is one of the SDK's pass/fail judges (``response_guidelines``,
    ``response_conditions``, ``response_rubric``, ``groundedness``, ``context_relevance``,
    ``context_sufficiency``, ``task_completion``, ``policy_adherence`` or ``knowledge_retention``),
    or any function that takes ``model=`` and ``name=`` and returns a
    scorer whose value is a boolean. ``models`` are from 2 to 9 different MLflow model URIs.
    ``name`` is the panel's scorer name, by default the judge's own default name. ``options`` are
    the judge's other arguments, the same for every member, and are checked now, as the judge
    checks them.

    The members grade each case at the same time, so the panel takes about as long as its slowest
    member. The feedback's value is the panel's verdict and its rationale every member's vote; its
    metadata holds the decision and each member's model, verdict and reason (``martian.panel.*``),
    and ``martian.judge.version`` when the members report one version. With no majority the
    feedback has no value and the error :data:`NO_MAJORITY`, so ``assert_checks`` and
    ``martian evals run`` count the case as an error.
    """
    from mlflow.entities import AssessmentError, AssessmentSource, AssessmentSourceType, Feedback
    from mlflow.genai.scorers import scorer

    found = check_models(models)
    if "model" in options:
        raise ValueError("a panel names its judge models in models=, not model=")
    if name is None:
        parameter = inspect.signature(judge).parameters.get("name")
        name = parameter.default if parameter is not None and isinstance(parameter.default, str) else None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name the panel's scorer with name=")
    label = name
    members = [(model, judge(model=model, name=label, **options)) for model in found]
    source = AssessmentSource(
        source_type=AssessmentSourceType.LLM_JUDGE, source_id=panel_source((model, None) for model in found)
    )

    @scorer(name=label)
    def assess(inputs: Any, outputs: Any, expectations: dict[str, Any] | None) -> Feedback:
        call = {"inputs": inputs, "outputs": outputs, "expectations": expectations}
        with ThreadPoolExecutor(max_workers=len(members), thread_name_prefix=f"panel-{label}") as pool:
            futures = [pool.submit(copy_context().run, _member_vote, model, member, call) for model, member in members]
            voted = [future.result() for future in futures]
        decision = decide([vote for vote, _, _ in voted])
        metadata: dict[str, Any] = decision.metadata()
        versions = {version for _, version, _ in voted if version}
        if len(versions) == 1:
            metadata[JUDGE_VERSION_KEY] = versions.pop()
        metadata |= _usage(voted)
        if decision.passed is None:
            return Feedback(
                name=label,
                value=None,
                rationale=decision.rationale,
                source=source,
                metadata=metadata,
                error=AssessmentError(error_code=NO_MAJORITY, error_message=decision.error),
            )
        return Feedback(
            name=label, value=decision.passed, rationale=decision.rationale, source=source, metadata=metadata
        )

    return assess


__all__ = [
    "NO_MAJORITY",
    "PREFIX",
    "RULE",
    "Decision",
    "Vote",
    "check_models",
    "decide",
    "is_panel",
    "judge_panel",
    "member_sources",
    "panel_source",
]
