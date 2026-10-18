"""Customer-owned value disclosure checks at a nonstreaming output boundary.

No semantic detection, recipient authentication, redaction, or learned inference.
Protected values remain local and never appear in decisions or exception text.
"""

from __future__ import annotations

import base64
import html
import json
import unicodedata
import uuid
from pathlib import Path
from typing import Literal, Self
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

OutputReason = Literal["output_clear", "output_restricted", "output_over_budget", "output_invalid"]


class OutputRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    rule_id: uuid.UUID
    value: SecretStr = Field(min_length=8, max_length=4096, repr=False)


class OutputPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1, 2] = 1
    policy_id: uuid.UUID
    rules: tuple[OutputRule, ...] = Field(min_length=1, max_length=128)
    max_output_bytes: int = Field(default=65536, ge=1, le=262144, strict=True)
    normalize_unicode: bool = Field(default=False, strict=True)
    encodings: tuple[Literal["base64", "hex", "url", "html", "json"], ...] = ()

    @model_validator(mode="after")
    def distinct_rules(self) -> Self:
        if self.schema_version == 1 and (self.normalize_unicode or self.encodings):
            raise ValueError("Normalization and encodings require policy schema version 2")
        if len(set(self.encodings)) != len(self.encodings):
            raise ValueError("Duplicate output encoding")
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("Duplicate disclosure rule ID")
        values = [rule.value.get_secret_value() for rule in self.rules]
        if len(set(values)) != len(values):
            raise ValueError("Duplicate protected value")
        if any(not value.strip() for value in values):
            raise ValueError("Protected values cannot be whitespace only")
        if self.normalize_unicode:
            normalized = [self._normalize(value) for value in values]
            if any(len(value) < 8 for value in normalized) or len(set(normalized)) != len(normalized):
                raise ValueError("Normalized protected values require eight characters and must be distinct")
        if sum(len(value.encode("utf-8")) for value in values) > 65536:
            raise ValueError("Protected values exceed 64 KiB")
        return self

    def _normalize(self, value: str) -> str:
        if not self.normalize_unicode:
            return value
        # Explicit policy choice: case, compatibility characters, format controls
        # and whitespace cease to distinguish a configured value.
        value = unicodedata.normalize("NFKC", value).casefold()
        return " ".join("".join(c for c in value if unicodedata.category(c) != "Cf").split())

    def _matches(self, value: str, compact: str, hex_text: str, views: tuple[str, ...]) -> bool:
        normalized = self._normalize(value)
        if any(normalized in view for view in views):
            return True
        # Search complete encodings of the configured UTF-8 value. Do not decode
        # arbitrary nested attacker data or assume an encoding changes entitlement.
        raw = value.encode("utf-8")
        if "base64" in self.encodings and any(
            encoded in compact
            for encoded in (
                base64.b64encode(raw).decode().rstrip("="),
                base64.urlsafe_b64encode(raw).decode().rstrip("="),
            )
        ):
            return True
        return "hex" in self.encodings and raw.hex() in hex_text

    @classmethod
    def from_file(cls, path: Path) -> OutputPolicy:
        try:
            with path.open("rb") as source:
                data = source.read(131073)
            if len(data) > 131072:
                raise ValueError("Policy exceeds 128 KiB")
            return cls.model_validate_json(data)
        except (OSError, ValueError, ValidationError):
            raise ValueError("Output policy could not be loaded; check its private configuration") from None

    def evaluate(self, text: str) -> OutputDecision:
        reason: OutputReason = "output_clear"
        matched: tuple[uuid.UUID, ...] = ()
        if not isinstance(text, str):
            reason = "output_invalid"
        elif len(text) > self.max_output_bytes:
            reason = "output_over_budget"
        else:
            try:
                size = len(text.encode("utf-8"))
            except UnicodeError:
                reason = "output_invalid"
            else:
                if size > self.max_output_bytes:
                    reason = "output_over_budget"
                else:
                    views = [text]
                    if "url" in self.encodings:
                        # One decoding layer, strict UTF-8; malformed sequences
                        # cannot become a valid decoded comparison view.
                        try:
                            views.append(unquote(text, encoding="utf-8", errors="strict"))
                        except UnicodeError:
                            return OutputDecision(policy_id=self.policy_id, reason="output_invalid")
                    if "html" in self.encodings:
                        views.append(html.unescape(text))
                    if "json" in self.encodings:
                        # JSON escaping is compared as text, including embedded
                        # quoted values; no JSON object or arbitrary code is run.
                        views.append(text.replace("\\/", "/"))
                    normalized_views = tuple(dict.fromkeys(self._normalize(view) for view in dict.fromkeys(views)))
                    compact = "".join(text.split()) if {"base64", "hex"} & set(self.encodings) else ""
                    hex_text = compact.lower() if "hex" in self.encodings else ""
                    matched = tuple(
                        rule.rule_id
                        for rule in self.rules
                        if self._matches(rule.value.get_secret_value(), compact, hex_text, normalized_views)
                        or (
                            "json" in self.encodings
                            and json.dumps(rule.value.get_secret_value(), ensure_ascii=True)[1:-1]
                            in text.replace("\\/", "/")
                        )
                    )
                    if matched:
                        reason = "output_restricted"
        return OutputDecision(policy_id=self.policy_id, reason=reason, matched_rule_ids=matched)


class OutputDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_id: uuid.UUID
    reason: OutputReason
    matched_rule_ids: tuple[uuid.UUID, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.reason == "output_clear"


class OutputDenied(RuntimeError):
    def __init__(self, decision: OutputDecision) -> None:
        self.decision = decision
        super().__init__("Response withheld by the configured output policy (" + decision.reason + ")")
