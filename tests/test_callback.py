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
