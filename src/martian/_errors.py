"""Controlled, compact diagnostics. Never render a response body or vendor exception."""

from __future__ import annotations

import re
from uuid import UUID

_MESSAGES = {
    "connection_error": "Could not reach Martian. Check connectivity and the configured API origin.",
    "invalid_response": "Martian returned an invalid response. Report this error and the SDK version.",
    "invalid_operation_response": "Martian returned an invalid operation receipt. Report this operation ID.",
    "operation_mismatch": "The operation receipt did not match the request. Report this operation ID.",
    "execution_unknown_recover_by_id": (
        "Execution outcome is unknown. Use get_operation with this ID; do not resubmit the operation."
    ),
    "status_unavailable": "Could not read operation status. Use get_operation again with the same ID.",
    "cancellation_unknown": "Cancellation was not confirmed. Check the same operation ID before taking further action.",
    "operation_cancelled": "The operation was cancelled.",
    "execution_cancelled": "The operation was cancelled.",
    "operation_purged": "The stored result expired. It cannot be used as a passing result.",
    "result_expired": "The stored result expired. It cannot be used as a passing result.",
    "execution_result_expired": "The stored result expired. It cannot be used as a passing result.",
    "operation_uncertain": (
        "Execution outcome could not be verified. Check the same operation ID before starting more work."
    ),
    "execution_outcome_unknown": (
        "Execution outcome could not be verified. Check the same operation ID before starting more work."
    ),
    "execution_deadline_exceeded": "The execution deadline expired. Report this operation ID if the problem persists.",
    "operation_failed": "The operation failed. Report this operation ID and the SDK version.",
    "execution_failed": "The operation failed. Report this operation ID and the SDK version.",
    "execution_result_invalid": "The execution result was invalid. Report this operation ID.",
    "thread_preparation_failed": "Martian could not prepare the operation. Report this operation ID.",
    "target_callback_failed": "The target callback failed. Check the application's own logs.",
    "target_timeout": "The target callback timed out. Check its I/O timeouts and target_timeout_seconds.",
}
_HTTP = {
    400: "Martian rejected the request. Check the SDK configuration.",
    401: "Authentication failed. Check the credential for the configured API origin.",
    403: "Permission denied. Check the key's capabilities, agent scope, and organisation access.",
    404: "The requested resource was not found or is outside this key's scope.",
    409: "The request conflicts with existing state. Check the operation or run ID before resubmitting.",
    410: "The stored result expired. It cannot be used as a passing result.",
    413: "The request exceeds the supported size. Reduce the batch or payload.",
    422: "Martian rejected the request format. Check the configuration and SDK/service compatibility.",
    429: "A request limit was reached. Reduce concurrency and check the key's limits.",
}


def error_code(value: object) -> str | None:
    """Only SDK-owned codes and numeric HTTP statuses cross the reporting boundary."""
    if isinstance(value, str) and (value in _MESSAGES or re.fullmatch(r"http_[1-5][0-9]{2}", value)):
        return value
    return None


def reference(value: object) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def saved_error_code(value: str | None) -> str | None:
    """Recover only our controlled diagnostic footer when a saved report is reopened."""
    if value is None:
        return None
    matched = re.search(r"\[([a-z0-9_]+)(?:; (?:operation_id|request_id)=[0-9a-f-]{36})*\]$", value)
    return error_code(matched[1]) if matched else None


def message(code: str, *, operation_id: object = None, request_id: object = None) -> str:
    code = error_code(code) or "execution_failed"
    if code.startswith("http_"):
        status = int(code[5:])
        detail = _HTTP.get(status, "Martian could not complete the API request. Report this error if it persists.")
    else:
        detail = _MESSAGES[code]
    identifiers = [code]
    if operation := reference(operation_id):
        identifiers.append("operation_id=" + operation)
    if request := reference(request_id):
        identifiers.append("request_id=" + request)
    return detail + " [" + "; ".join(identifiers) + "]"


def concise_problems(problems: list[str], *, limit: int = 5) -> list[str]:
    unique = list(dict.fromkeys(problems))
    shown = unique[:limit]
    if len(unique) > limit:
        shown.append(f"{len(unique) - limit} more problem(s); see the saved session receipt.")
    return shown
