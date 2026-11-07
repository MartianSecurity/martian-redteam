"""Origin-bound Backend client implementing PLAN_MARTIAN_SDK_SINGLE_KEY.md."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator, model_validator

from martian._errors import error_code, message, reference
from martian._privacy import RedactionPolicy
from martian.config import ConfigError, MartianConfig
from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION
from martian_redteam.client import ClientError
from martian_redteam.types import EvaluatorContext, EvaluatorOutput, SimulatorContext, SimulatorOutput

OperationState = Literal[
    "admitted", "dispatching", "running", "cancel_requested", "succeeded", "failed", "cancelled", "uncertain", "purged"
]


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SimulatorResult(_Dto):
    attack: str
    attack_method: str
    graph_revision: str
    policy_revision: str

    @field_validator("*")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("result fields must not be blank")
        return value


class EvaluatorResult(_Dto):
    score: Literal[0, 1]
    reason: str
    graph_revision: str
    policy_revision: str

    @field_validator("score", mode="before")
    @classmethod
    def binary_integer(cls, value: object) -> object:
        if type(value) is not int or value not in (0, 1):
            raise ValueError("score must be the integer 0 or 1")
        return value

    @field_validator("reason", "graph_revision", "policy_revision")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("result fields must not be blank")
        return value


class OperationFailure(_Dto):
    code: str
    execution_state: OperationState
    retryable: Literal[False] = False


class RedteamOperation(_Dto):
    """Safe status receipt; absent or expired results never represent a passing probe."""

    operation_id: UUID
    kind: Literal["simulate", "evaluate"]
    state: OperationState
    deadline_at: datetime
    poll_after_ms: int | None = None
    result_expires_at: datetime | None = None
    result: SimulatorResult | EvaluatorResult | None = None
    error: OperationFailure | None = None

    @model_validator(mode="after")
    def consistent(self) -> RedteamOperation:
        if self.deadline_at.tzinfo is None or (
            self.result_expires_at is not None and self.result_expires_at.tzinfo is None
        ):
            raise ValueError("operation times must include their timezone")
        if self.state == "succeeded" and (self.result is None or self.result_expires_at is None):
            raise ValueError("successful operations require a result and expiry")
        if self.result is not None:
            expected = SimulatorResult if self.kind == "simulate" else EvaluatorResult
            if self.state != "succeeded" or not isinstance(self.result, expected) or self.error is not None:
                raise ValueError("result must match a successful operation kind")
        return self


class RedteamOperationError(ClientError):
    """Controlled failure retaining an operation ID for explicit status recovery."""

    def __init__(
        self, operation_id: UUID, code: str, *, reason_code: str | None = None, request_id: str | None = None
    ) -> None:
        self.operation_id = str(operation_id)
        self.code = code
        self.reason_code = error_code(reason_code)
        self.request_id = reference(request_id)
        super().__init__(message(self.reason_code or code, operation_id=operation_id, request_id=self.request_id))


# Import adapter only: the modern namespace cannot configure direct Agents.
RedteamConfig = MartianConfig


def settings() -> MartianConfig:
    return MartianConfig.from_env()


class Client:
    """Use one Martian Backend token for generation and judging; targets stay local.

    Execution POSTs are sent once. Ambiguous responses are recovered by looking
    up the same operation ID, never by starting another model call. HTTPX phase
    timeouts and polling share one absolute local wait deadline.
    """

    def __init__(
        self,
        *,
        config: MartianConfig | None = None,
        api_url: str | None = None,
        token: str | SecretStr | None = None,
        agent_id: UUID | str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        api_key: str | None = None,
    ) -> None:
        if api_key is not None:
            raise ConfigError(
                "martian.redteam uses token= with MartianConfig and a Backend origin; "
                "use martian_redteam.Client(Settings(...)) for legacy Agents"
            )
        if config is not None and (api_url is not None or token is not None):
            raise ConfigError("pass config or api_url/token, not both")
        self.config = config or MartianConfig.from_env(api_url=api_url, token=token)
        try:
            self.agent_id = UUID(str(agent_id)) if agent_id is not None else None
        except ValueError:
            raise ConfigError("agent_id must be a UUID") from None
        self._http = httpx.AsyncClient(
            base_url=self.config.api_url,
            headers={"Authorization": f"Bearer {self.config.token.get_secret_value()}", "Accept": "application/json"},
            timeout=httpx.Timeout(self.config.request_timeout_seconds),
            follow_redirects=False,
            trust_env=self.config.trust_env,
            transport=transport,
        )

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return self.config.redaction_policy

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _parse(self, response: httpx.Response, operation_id: UUID, kind: str | None = None) -> RedteamOperation:
        if response.status_code not in (200, 202, 410):
            # Never include vendor bodies, URLs, headers, or supplied credentials.
            raise RedteamOperationError(
                operation_id,
                f"http_{response.status_code}",
                request_id=reference(
                    response.headers.get("x-request-id"), exclude=self.config.token.get_secret_value()
                ),
            )
        try:
            operation = RedteamOperation.model_validate(response.json())
        except (ValueError, ValidationError):
            raise RedteamOperationError(operation_id, "invalid_operation_response") from None
        if operation.operation_id != operation_id or (kind is not None and operation.kind != kind):
            raise RedteamOperationError(operation_id, "operation_mismatch")
        if response.status_code == 410 and operation.state != "purged":
            raise RedteamOperationError(operation_id, "invalid_operation_response")
        if operation.result is not None and (
            operation.result_expires_at is None or operation.result_expires_at <= datetime.now(UTC)
        ):
            raise RedteamOperationError(operation_id, "result_expired")
        return operation

    async def get_operation(self, operation_id: UUID | str) -> RedteamOperation:
        """Read an admitted operation without repeating its model work."""
        identifier = UUID(str(operation_id))
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                response = await self._http.get(f"/v1/redteam/operations/{identifier}")
        except (httpx.RequestError, TimeoutError):
            raise RedteamOperationError(identifier, "status_unavailable") from None
        return self._parse(response, identifier)

    async def cancel_operation(self, operation_id: UUID | str) -> RedteamOperation:
        """Request cancellation once; cancellation is not a passing result."""
        identifier = UUID(str(operation_id))
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                response = await self._http.post(f"/v1/redteam/operations/{identifier}/cancel")
        except (httpx.RequestError, TimeoutError):
            raise RedteamOperationError(identifier, "cancellation_unknown") from None
        return self._parse(response, identifier)

    async def _execute(
        self, kind: Literal["simulate", "evaluate"], body: dict[str, object], operation_id: UUID | str | None = None
    ) -> RedteamOperation:
        try:
            identifier = UUID(str(operation_id)) if operation_id is not None else uuid4()
        except ValueError:
            raise ConfigError("operation_id must be a UUID") from None
        payload = {"operation_id": str(identifier), "catalog_version": CATALOG_VERSION, **body}
        if self.agent_id is not None:
            payload["agent_id"] = str(self.agent_id)
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                try:
                    response = await self._http.post(f"/v1/redteam/{kind}", json=payload)
                except httpx.RequestError:
                    # The server may have admitted it. Only GET is safe here.
                    response = await self._http.get(f"/v1/redteam/operations/{identifier}")
                if response.status_code in (500, 502, 503, 504):
                    response = await self._http.get(f"/v1/redteam/operations/{identifier}")
                operation = self._parse(response, identifier, kind)
                while operation.state in ("admitted", "dispatching", "running", "cancel_requested"):
                    await asyncio.sleep(min(max((operation.poll_after_ms or 100) / 1000, 0.01), 5))
                    response = await self._http.get(f"/v1/redteam/operations/{identifier}")
                    operation = self._parse(response, identifier, kind)
        except (httpx.RequestError, TimeoutError):
            raise RedteamOperationError(identifier, "execution_unknown_recover_by_id") from None
        if operation.state != "succeeded" or operation.result is None:
            raise RedteamOperationError(
                identifier,
                f"operation_{operation.state}",
                reason_code=operation.error.code if operation.error else None,
            )
        return operation

    async def simulate(
        self, context: SimulatorContext, *, prompt: str | None = None, operation_id: UUID | str | None = None
    ) -> SimulatorOutput:
        body: dict[str, object] = {"context": context.model_dump(exclude_none=True)}
        if prompt is not None:
            body["prompt"] = prompt
        operation = await self._execute("simulate", body, operation_id)
        assert isinstance(operation.result, SimulatorResult)
        return SimulatorOutput(**operation.result.model_dump(), operation_id=str(operation.operation_id))

    async def evaluate(
        self,
        context: EvaluatorContext,
        *,
        attack: str,
        target_output: str,
        tools_called: list[str] | None = None,
        operation_id: UUID | str | None = None,
    ) -> EvaluatorOutput:
        operation = await self._execute(
            "evaluate",
            {
                "context": context.model_dump(exclude_none=True),
                "attack": attack,
                "target_output": target_output,
                "tools_called": tools_called or [],
            },
            operation_id,
        )
        assert isinstance(operation.result, EvaluatorResult)
        return EvaluatorOutput(**operation.result.model_dump(), operation_id=str(operation.operation_id))
