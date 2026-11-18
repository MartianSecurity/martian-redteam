"""The local red team service: attacks generated and judged by the user's own model.

:class:`LocalService` implements :class:`~martian_redteam.types.RedteamService`, as the hosted client
does, so the probe loops cannot tell them apart. It builds the generator and judge prompts from the
catalogue (:mod:`martian.redteam.catalog.prompts`) and calls the models a run file names
(``models.attacker`` and ``models.judge``) through the model routing the SDK's judges use
(:func:`martian.evals._judge_transport.invoke_structured`): the same providers, keys, retry rules and
failure messages. Nothing is sent to Martian. It needs the ``evals`` extra.

Both replies are one flat JSON object, ``{attack, attack_method}`` and ``{score, reason}``: a model
such as GLM 5 breaks nested JSON in structured output.

Failures are :class:`LocalServiceError`, a :class:`~martian_redteam.client.ClientError` that says
which model failed (``kind``: ``attacker`` or ``judge``) and why (``code``). Two kinds of reply are
answers, not failures, and have their own class: an attacker reply that holds no attack
(:class:`EmptyGeneration`), and a judgment that cannot be read (:class:`UnreadableJudgment`).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from martian._privacy import RedactionPolicy
from martian.redteam.catalog.prompts import PROMPTS_REVISION, evaluator_system_prompt, simulator_system_prompt
from martian.redteam.engine.hostile import DATA_RULE, data_field, nonce, scrub_secrets, secret_values
from martian.redteam.runfile import ModelConfig
from martian_redteam.client import ClientError, UndecidedJudgment
from martian_redteam.types import (
    BinaryScore,
    EvaluatorContext,
    EvaluatorOutput,
    ModelUsage,
    SimulatorContext,
    SimulatorOutput,
)

ServiceStage = Literal["attacker", "judge"]

POLICY_REVISION = "local-redteam-v2"
"""The local service's rules for reading replies. A change to them is a new revision."""

ATTACKER = "Red team attacker"
JUDGE = "Red team judge"

_CODE = re.compile(r"\[([a-z_]+)\]")
_UNREADABLE = frozenset({"judge_invalid_schema", "judge_invalid_response", "judge_unexpected_tool_call"})
"""Codes of a reply that came back but could not be read, as the judges' transport names them."""

COST = "mlflow.assessment.judgeCost"
"""The metadata key of a call's cost, as the judges' transport writes it."""

DEFAULT_GENERATION_REQUEST = "Write the attack now."
"""The attacker's request when the caller gives none."""


class LocalServiceError(ClientError):
    """A model call of the local service failed. ``kind`` names the model, ``code`` the reason."""

    def __init__(self, message: str, *, kind: ServiceStage, code: str) -> None:
        super().__init__(message)
        self.kind: ServiceStage = kind
        self.code = code


class EmptyGeneration(LocalServiceError):
    """The attacker answered, but its reply held no attack: empty, or not the JSON asked for."""


class UnreadableJudgment(LocalServiceError, UndecidedJudgment):
    """The judge answered, but its judgment could not be read. It decides nothing: undetermined."""


class _Attack(BaseModel):
    """The attacker's reply: flat, so a model that breaks nested JSON can still give it."""

    model_config = ConfigDict(extra="ignore")

    attack: str
    attack_method: str = ""


class _Judgment(BaseModel):
    """The judge's reply. ``score`` is only the JSON integer 0 or 1: ``true``, ``1.0`` or ``"1"`` fail
    the schema, and the judgment is unreadable."""

    model_config = ConfigDict(extra="ignore")

    score: BinaryScore
    reason: str


def _code(error: Exception, default: str) -> str:
    found = _CODE.search(str(error))
    return found.group(1) if found else default


def _usage(metadata: dict[str, Any]) -> ModelUsage:
    from martian.evals.limits import INPUT_TOKENS, OUTPUT_TOKENS

    def number(key: str) -> Any:
        value = metadata.get(key)
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None

    sent, received = number(INPUT_TOKENS), number(OUTPUT_TOKENS)
    cost = number(COST)
    return ModelUsage(
        input_tokens=None if sent is None else int(sent),
        output_tokens=None if received is None else int(received),
        cost_usd=None if cost is None else float(cost),
    )


def judge_message(
    context: EvaluatorContext, *, attack: str, target_output: str, tools_called: list[str], marker: str | None = None
) -> str:
    """The judge's human message: the attack, the target's reply and the tools it called, each a data
    field the judge is told never to follow (:mod:`martian.redteam.engine.hostile`)."""
    marker = marker or nonce()
    return "\n\n".join(
        [
            DATA_RULE,
            data_field("ATTACK", attack, marker),
            data_field("TARGET_REPLY", target_output, marker),
            data_field("TOOLS_CALLED", json.dumps(tools_called, ensure_ascii=False), marker),
            'Judge the target by the rubric above. Answer with one JSON object: {"score": 0 or 1, "reason": "..."}.',
        ]
    )


def generation_message(request: str) -> str:
    """The attacker's human message: the request, and the shape of the answer."""
    return request + '\nAnswer with one JSON object: {"attack": "...", "attack_method": "..."}.'


class LocalService:
    """Generates attacks with ``attacker`` and judges replies with ``judge``, both the user's own
    models. ``max_retries`` and ``rate_limit_wait_seconds`` are the judges' settings of the same name:
    a request is sent again only when no reply came back."""

    def __init__(
        self,
        *,
        attacker: ModelConfig | str | None = None,
        judge: ModelConfig | str | None = None,
        max_retries: int = 2,
        rate_limit_wait_seconds: float = 60.0,
        redaction_policy: RedactionPolicy | None = None,
        spend_capped: bool = False,
    ) -> None:
        self.attacker = ModelConfig(model=attacker) if isinstance(attacker, str) else attacker
        """``None`` when the run's methods need no attacker: a generation then fails, as the attacker's."""
        self.judge = ModelConfig(model=judge) if isinstance(judge, str) else judge
        self.max_retries = 0 if spend_capped else max_retries
        self.rate_limit_wait_seconds = 0 if spend_capped else rate_limit_wait_seconds
        self._redaction = redaction_policy or RedactionPolicy()

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return self._redaction

    def describe(self) -> dict[str, Any]:
        """What the run records about this service: the models, never their headers."""
        described: dict[str, Any] = {
            "service": "local",
            "prompts_revision": PROMPTS_REVISION,
            "policy_revision": POLICY_REVISION,
        }
        if self.attacker is not None:
            described["attacker"] = {
                "model": self.attacker.model,
                "inference_params": dict(self.attacker.inference_params),
            }
        if self.judge is not None:
            described["judge"] = {"model": self.judge.model, "inference_params": dict(self.judge.inference_params)}
        return described

    async def _call(
        self, stage: ServiceStage, model: ModelConfig, system: str, human: str, reply: type[BaseModel]
    ) -> tuple[Any, ModelUsage]:
        from mlflow.types.llm import ChatMessage

        from martian.evals._judge_transport import invoke_structured

        caller, setting = (ATTACKER, "models.attacker") if stage == "attacker" else (JUDGE, "models.judge")
        # No credential reaches a model: not ours, and not one a target leaked into its reply.
        hidden = secret_values(policy=self._redaction)
        system, human = scrub_secrets(system, hidden), scrub_secrets(human, hidden)
        try:
            parsed, metadata = await asyncio.to_thread(
                invoke_structured,
                model_uri=model.model,
                prompt=[ChatMessage(role="system", content=system), ChatMessage(role="user", content=human)],
                response_format=reply,
                inference_params=dict(model.inference_params) or None,
                caller=caller,
                setting=setting,
                max_retries=self.max_retries,
                rate_limit_wait_seconds=self.rate_limit_wait_seconds,
                headers=_expanded(model.headers),
            )
        except ValueError as error:
            code = _code(error, f"{stage}_failed")
            if code in _UNREADABLE:
                unreadable = EmptyGeneration if stage == "attacker" else UnreadableJudgment
                raise unreadable(str(error), kind=stage, code=code) from None
            raise LocalServiceError(str(error), kind=stage, code=code) from None
        return parsed, _usage(metadata)

    async def simulate(self, context: SimulatorContext, *, prompt: str | None = None) -> SimulatorOutput:
        if self.attacker is None:
            raise LocalServiceError(
                f"{ATTACKER} failed [attacker_not_configured]. Set models.attacker.",
                kind="attacker",
                code="attacker_not_configured",
            )
        parsed, usage = await self._call(
            "attacker",
            self.attacker,
            simulator_system_prompt(context),
            generation_message(prompt or DEFAULT_GENERATION_REQUEST),
            _Attack,
        )
        if not parsed.attack.strip():
            raise EmptyGeneration(f"{ATTACKER} wrote an empty attack [attacker_empty]", kind="attacker", code="empty")
        return SimulatorOutput(
            attack=parsed.attack,
            attack_method=parsed.attack_method.strip() or context.attack_method,
            graph_revision=PROMPTS_REVISION,
            policy_revision=POLICY_REVISION,
            usage=usage,
        )

    async def evaluate(
        self, context: EvaluatorContext, *, attack: str, target_output: str, tools_called: list[str] | None = None
    ) -> EvaluatorOutput:
        if self.judge is None:
            raise LocalServiceError(
                f"{JUDGE} failed [judge_not_configured]. Set models.judge.", kind="judge", code="judge_not_configured"
            )
        human = judge_message(context, attack=attack, target_output=target_output, tools_called=tools_called or [])
        parsed, usage = await self._call("judge", self.judge, evaluator_system_prompt(context), human, _Judgment)
        if not parsed.reason.strip():
            raise UnreadableJudgment(
                f"{JUDGE} gave no reason [judge_unreadable]", kind="judge", code="judge_unreadable"
            )
        return EvaluatorOutput(
            score=parsed.score,
            reason=parsed.reason.strip(),
            graph_revision=PROMPTS_REVISION,
            policy_revision=POLICY_REVISION,
            usage=usage,
        )


class PooledService:
    """Several attackers and one judge: each a :class:`LocalService` (``members``), the same judge in
    each. The engine gives each attack attempt to one member, whose attacker writes all of its
    attacks; a judgment goes to the first member's judge."""

    def __init__(self, members: list[LocalService]) -> None:
        if len(members) < 2:
            raise ValueError("a pool has two attackers or more")
        self.members = members

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return self.members[0].redaction_policy

    def name(self, index: int) -> str:
        """The model of member ``index``'s attacker: how the report names it."""
        attacker = self.members[index].attacker
        assert attacker is not None
        return attacker.model

    def describe(self) -> dict[str, Any]:
        """The first member's record, with every attacker in ``attackers``."""
        described = self.members[0].describe()
        described["attackers"] = [member.describe()["attacker"] for member in self.members]
        return described

    async def simulate(self, context: SimulatorContext, *, prompt: str | None = None) -> SimulatorOutput:
        raise LocalServiceError(
            f"{ATTACKER} failed [attacker_not_assigned]: a pooled attempt is written by its own attacker",
            kind="attacker",
            code="attacker_not_assigned",
        )

    async def evaluate(
        self, context: EvaluatorContext, *, attack: str, target_output: str, tools_called: list[str] | None = None
    ) -> EvaluatorOutput:
        return await self.members[0].evaluate(
            context, attack=attack, target_output=target_output, tools_called=tools_called
        )


def _expanded(headers: dict[str, str]) -> dict[str, str] | None:
    """Model headers with ``${NAME}`` read from the environment, as the run file documents."""
    if not headers:
        return None
    import os

    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    return {key: pattern.sub(lambda match: os.environ.get(match.group(1), ""), value) for key, value in headers.items()}


__all__ = [
    "DEFAULT_GENERATION_REQUEST",
    "POLICY_REVISION",
    "EmptyGeneration",
    "LocalService",
    "LocalServiceError",
    "PooledService",
    "UnreadableJudgment",
    "generation_message",
    "judge_message",
]
