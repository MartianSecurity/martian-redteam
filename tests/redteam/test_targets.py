"""Function and HTTP red team targets shared with suite files, with conversations (martian-notes#242)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from martian.evals.runner import TargetCallError, TargetRef, _http_target, parse_suite
from martian.redteam.runfile import RunFile
from martian.redteam.runfile import Target as TargetBlock
from martian.redteam.targets import Target, target_from_run_file
from martian.redteam.targets.function import FunctionTarget
from martian.redteam.targets.http import HttpTarget
from tests.redteam.target_servers import StatefulChat, json_server

PURPOSE = "A support assistant that answers questions about orders."


def block(**fields: Any) -> TargetBlock:
    return TargetBlock.model_validate({"purpose": PURPOSE, **fields})


async def conversation(target: Target, messages: list[str], session: str | None = None) -> list[Any]:
    """One attempt: the messages in order, in one session that the first reply names."""
    replies = []
    for message in messages:
        reply = await target.send(message, session=session)
        session = reply.session
        replies.append(reply)
    return replies


async def test_the_same_target_block_works_in_a_suite_file_and_a_red_team_run():
    with json_server(StatefulChat()) as (url, recorded):
        target_block = {
            "url": url,
            "headers": {"X-Api-Key": "${RT_TARGET_KEY}"},
            "body": {"message": "{{input}}", "session": "{{session_id}}"},
            "response_path": "reply",
        }
        suite = parse_suite(
            yaml.safe_dump(
                {"data": "cases.jsonl", "target": target_block, "checks": [{"type": "contains", "values": ["turn"]}]}
            )
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("RT_TARGET_KEY", "key-1")
            assert suite.target is not None
            suite_reply = _http_target(suite.target, suite.input_key)(message="hello")
            run_file = RunFile.model_validate(
                {
                    "version": 2,
                    "target": {"purpose": PURPOSE, **target_block},
                    "vulnerabilities": [{"name": "PromptLeakage"}],
                    "attacks": [{"name": "PromptInjection"}],
                }
            )
            target = target_from_run_file(run_file.target)
        reply = await target.send("hello")
        await target.close()
    assert isinstance(target, HttpTarget) and isinstance(target, Target)
    assert suite_reply == reply.text == "turn 1"
    assert [body["message"] for body in recorded.bodies] == ["hello", "hello"]
    assert [headers["x-api-key"] for headers in recorded.headers] == ["key-1", "key-1"]


async def test_each_attempt_is_a_fresh_conversation_against_a_stateful_server():
    chat = StatefulChat()
    with json_server(chat) as (url, recorded):
        target = target_from_run_file(
            block(url=url, body={"message": "{{input}}", "session": "{{session_id}}"}, response_path="reply")
        )
        first = await conversation(target, ["a", "b", "c"])
        second = await conversation(target, ["a", "b"])
        await target.close()
    assert [reply.text for reply in first] == ["turn 1", "turn 2", "turn 3"]
    assert [reply.text for reply in second] == ["turn 1", "turn 2"]
    assert len({reply.session for reply in first}) == 1
    assert first[0].session != second[0].session
    assert sorted(chat.turns.values()) == [2, 3]
    assert {body["session"] for body in recorded.bodies} == {first[0].session, second[0].session}


async def test_the_body_chooses_between_resending_the_history_and_only_the_newest_turn():
    with json_server(StatefulChat()) as (url, recorded):
        resend = target_from_run_file(
            block(url=url, body={"messages": "{{messages}}", "session": "{{session_id}}"}, response_path="reply")
        )
        await conversation(resend, ["one", "two", "three"])
        assert [body["messages"][-1]["content"] for body in recorded.bodies] == ["one", "two", "three"]
        assert [len(body["messages"]) for body in recorded.bodies] == [1, 3, 5]
        assert recorded.bodies[2]["messages"][:4] == [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "turn 1"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "turn 2"},
        ]
        recorded.bodies.clear()
        newest = target_from_run_file(
            block(url=url, body={"message": "{{input}}", "session": "{{session_id}}"}, response_path="reply")
        )
        await conversation(newest, ["one", "two"])
        assert recorded.bodies == [
            {"message": "one", "session": recorded.bodies[0]["session"]},
            {"message": "two", "session": recorded.bodies[0]["session"]},
        ]
        await resend.close()
        await newest.close()


async def test_a_session_the_engine_names_is_the_conversation_id():
    with json_server(StatefulChat()) as (url, recorded):
        target = target_from_run_file(
            block(url=url, body={"message": "{{input}}", "session": "{{session_id}}"}, response_path="reply")
        )
        replies = await conversation(target, ["a", "b"], session="attempt-0001")
        await target.close()
    assert [reply.session for reply in replies] == ["attempt-0001", "attempt-0001"]
    assert [body["session"] for body in recorded.bodies] == ["attempt-0001", "attempt-0001"]


async def test_the_conversation_id_can_travel_in_a_header():
    with json_server(StatefulChat()) as (url, recorded):
        target = target_from_run_file(
            block(url=url, body={"message": "{{input}}"}, response_path="reply", session={"header": "X-Session-Id"})
        )
        replies = await conversation(target, ["a", "b"])
        await target.close()
    assert [reply.text for reply in replies] == ["turn 1", "turn 2"]
    assert [headers["x-session-id"] for headers in recorded.headers] == [replies[0].session] * 2


async def test_a_server_that_issues_its_own_ids_gets_them_back_and_a_new_attempt_starts_empty():
    chat = StatefulChat(issues_ids=True)
    with json_server(chat) as (url, recorded):
        target = target_from_run_file(
            block(
                url=url,
                body={"message": "{{input}}", "session": "{{session_id}}"},
                response_path="reply",
                session={"response_path": "session"},
            )
        )
        first = await conversation(target, ["a", "b"])
        second = await conversation(target, ["a"])
        await target.close()
    sent = [body["session"] for body in recorded.bodies]
    assert sent[0] == "" and sent[2] == ""
    assert sent[1].startswith("srv-") and sent[1] != ""
    assert [reply.text for reply in first + second] == ["turn 1", "turn 2", "turn 1"]
    assert len(chat.turns) == 2


async def test_a_reply_without_the_issued_id_is_the_targets_error():
    with json_server(lambda body, headers: (200, {"reply": "hi"})) as (url, _):
        target = target_from_run_file(
            block(url=url, body={"message": "{{input}}"}, response_path="reply", session={"response_path": "session"})
        )
        with pytest.raises(TargetCallError, match="no session id at target.session.response_path"):
            await target.send("hello")
        await target.close()


async def test_a_failed_call_names_the_setting_and_never_the_address():
    with json_server(lambda body, headers: (401, {"error": "secret detail"})) as (url, _):
        target = target_from_run_file(block(url=url, body={"message": "{{input}}"}))
        with pytest.raises(TargetCallError) as raised:
            await target.send("hello")
        await target.close()
    assert "check the credentials in target.headers" in str(raised.value)
    assert url not in str(raised.value) and "secret" not in str(raised.value)


async def test_an_address_policy_pins_the_connection():
    with json_server(StatefulChat()) as (url, _):
        refused = HttpTarget(
            TargetRef(url=url, body={"message": "{{input}}", "session": "{{session_id}}"}, response_path="reply"),
            address_policy=lambda host, address: False,
        )
        with pytest.raises(TargetCallError, match="not one this installation may call"):
            await refused.send("hello")
        allowed = HttpTarget(
            TargetRef(url=url, body={"message": "{{input}}", "session": "{{session_id}}"}, response_path="reply"),
            address_policy=lambda host, address: address == "127.0.0.1",
        )
        assert (await allowed.send("hello")).text == "turn 1"
        await refused.close()
        await allowed.close()


def test_session_settings_apply_to_a_url_target_only(tmp_path: Path):
    with pytest.raises(ValueError, match="session applies to a url target"):
        block(file=str(tmp_path / "agent.py"), session={"header": "X-Session-Id"})


FUNCTION = """
def target(message, history, session_id):
    return f"{len(history)}:{session_id}:{message}"


def stateless(message):
    return {"echo": message}


def broken(message):
    raise KeyError("a secret in the exception")
"""


@pytest.fixture
def agent_file(tmp_path: Path) -> Path:
    path = tmp_path / "agent.py"
    path.write_text(textwrap.dedent(FUNCTION), encoding="utf-8")
    return path


async def test_a_function_target_is_given_the_conversation_and_each_attempt_starts_fresh(agent_file: Path):
    target = target_from_run_file(block(file=agent_file.name), base=agent_file.parent)
    assert isinstance(target, FunctionTarget)
    try:
        first = await conversation(target, ["a", "b"])
        second = await conversation(target, ["c"])
    finally:
        await target.close()
    session = first[0].session
    assert [reply.text for reply in first] == [f"0:{session}:a", f"2:{session}:b"]
    assert second[0].text == f"0:{second[0].session}:c" and second[0].session != session


async def test_a_function_without_conversation_parameters_gets_the_message_only(agent_file: Path):
    target = target_from_run_file(block(file=str(agent_file), function="stateless"))
    try:
        reply = await target.send("hello")
    finally:
        await target.close()
    assert reply.text == '{"echo": "hello"}'


async def test_a_function_that_raises_keeps_only_the_exception_type(agent_file: Path):
    target = target_from_run_file(block(file=str(agent_file), function="broken"))
    try:
        with pytest.raises(TargetCallError) as raised:
            await target.send("hello")
    finally:
        await target.close()
    assert "KeyError" in str(raised.value) and "secret" not in str(raised.value)


def test_a_session_name_bind_tools_cannot_send_as_an_action_id_gets_a_stable_safe_id():
    from martian.redteam.targets.conversations import ACTION_ID, conversation_id

    assert conversation_id("attempt-0001") == "attempt-0001"
    engine_name = "prompt_leakage/instructions/prompt_injection#1"
    safe = conversation_id(engine_name)
    assert ACTION_ID.fullmatch(safe) and safe == conversation_id(engine_name) and safe != engine_name


async def test_a_reply_says_whether_the_target_reports_its_tool_calls():
    with json_server(StatefulChat()) as (url, _):
        target = target_from_run_file(
            block(url=url, body={"message": "{{input}}", "session": "{{session_id}}"}, response_path="reply")
        )
        reply = await target.send("hello")
        await target.close()
    assert reply.tool_calls_observed is False


async def test_a_run_files_function_is_given_its_conversation_and_may_return_the_reply_contract(tmp_path: Path):
    """``martian redteam run`` calls a ``file`` target in process (martian-notes#725)."""
    from martian.redteam.engine.runs import build_target

    path = tmp_path / "agent.py"
    path.write_text(
        textwrap.dedent(
            """
            SEEN = []

            async def target(message, session_id="", history=None):
                SEEN.append((session_id, list(history)))
                call = {"name": "lookup_order", "arguments": {"order": 7}, "executed": True}
                return {"text": f"echo {message}", "tool_calls": [call]}

            def wrong(message):
                return {"reply": message}
            """
        ),
        encoding="utf-8",
    )
    target = build_target(block(file=str(path), function="target"))
    first = await conversation(target, ["a", "b"], session="attempt-1")
    other = await target.send("c", session="attempt-2")
    seen = target._callback.__globals__["SEEN"]  # type: ignore[attr-defined]
    assert seen == [
        ("attempt-1", []),
        ("attempt-1", [{"role": "user", "content": "a"}, {"role": "assistant", "content": "echo a"}]),
        ("attempt-2", []),
    ]
    assert [reply.session for reply in [*first, other]] == ["attempt-1", "attempt-1", "attempt-2"]
    [call] = first[0].tool_calls
    assert (call.name, call.arguments, call.executed) == ("lookup_order", {"order": 7}, True)
    assert first[0].tool_calls_observed and first[0].text == "echo a"
    with pytest.raises(TypeError, match="tool_calls"):
        await build_target(block(file=str(path), function="wrong")).send("a", session="attempt-3")
