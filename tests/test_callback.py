import asyncio
import threading

import pytest

from martian_redteam.callback import wrap_callback
from martian_redteam.types import Turn


@pytest.mark.asyncio
async def test_wraps_sync_string() -> None:
    wrapped = wrap_callback(lambda prompt: f"echo:{prompt}")
    turn = await wrapped("hello", None)
    assert turn == Turn(role="ai", content="echo:hello")


@pytest.mark.asyncio
async def test_wraps_async_turn() -> None:
    async def target(prompt: str, turns=None) -> Turn:
        return Turn(role="ai", content="no", tools_called=["add"])

    wrapped = wrap_callback(target)
    turn = await wrapped("x", None)
    assert turn.tools_called == ["add"]


@pytest.mark.asyncio
async def test_rejects_non_string_return() -> None:
    wrapped = wrap_callback(lambda prompt: 123)  # type: ignore[arg-type, return-value]
    with pytest.raises(TypeError, match="str or Turn"):
        await wrapped("x", None)


@pytest.mark.asyncio
async def test_sync_callback_does_not_block_other_work_on_the_event_loop() -> None:
    released = threading.Event()

    def target(prompt: str) -> str:
        return "released" if released.wait(timeout=2) else "blocked"

    async def release() -> None:
        await asyncio.sleep(0)
        released.set()

    turn, _ = await asyncio.gather(wrap_callback(target)("x", None), release())
    assert turn.content == "released"
