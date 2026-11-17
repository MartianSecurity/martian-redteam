"""Build the target a run file's ``target`` block names.

``file`` and ``url`` are a suite file's target block, read by the suite file's own rules
(:class:`martian.evals.runner.TargetRef`), so the same block works in an eval suite and a red team
run. Nothing here calls the target: building one reads ``${NAME}`` from the environment and checks
the settings, and the first message connects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from martian.evals._pinned_address import AddressPolicy
from martian.redteam import runfile
from martian.redteam.targets import Target


def target_from_run_file(
    block: runfile.Target,
    *,
    base: str | Path = ".",
    private: Iterable[str | Path] = (),
    address_policy: AddressPolicy | None = None,
    environ: Mapping[str, str] | None = None,
) -> Target:
    """The target of ``block``. ``base`` is the run file's folder, for a relative ``file``;
    ``private`` what a function target's process must not reach; ``address_policy`` pins the
    connections of an HTTP target (:mod:`martian.redteam.targets.http`); ``environ`` stands in for the
    environment when an HTTP or MCP target's ``${NAME}`` is read. An MCP target needs the ``mcp``
    extra; its stdio server starts in ``base``."""
    from martian.evals.runner import DEFAULT_TARGET_TIMEOUT_SECONDS, TargetRef

    if block.kind == "function":
        from martian.redteam.targets.function import FunctionTarget

        return FunctionTarget(TargetRef.model_validate(block.suite_target_fields()), base=base, private=private)
    if block.kind == "http":
        from martian.redteam.targets.http import HttpTarget

        return HttpTarget(
            TargetRef.model_validate(block.suite_target_fields()),
            session=block.session,
            tool_calls_path=block.tool_calls_path,
            address_policy=address_policy,
            environ=environ,
        )
    from martian.redteam.targets.mcp import McpServerTarget

    assert block.mcp is not None
    return McpServerTarget(
        block.mcp,
        timeout_seconds=block.timeout_seconds or DEFAULT_TARGET_TIMEOUT_SECONDS,
        base=base,
        environ=environ,
    )


__all__ = ["target_from_run_file"]
