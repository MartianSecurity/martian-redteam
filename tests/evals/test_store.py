"""The store interface: the memory store passes the contract, and the interface speaks only the model."""

import inspect
import types
import typing

import pytest

from martian.evals import model
from martian.evals.store import STORE_METHODS, EvalStore, MemoryStore
from tests.evals.store_contract import StoreContract


class TestMemoryStore(StoreContract):
    @pytest.fixture
    def store(self) -> EvalStore:
        return MemoryStore()


def _types_in(annotation: object) -> typing.Iterator[type]:
    origin = typing.get_origin(annotation)
    if origin is not None:
        if isinstance(origin, type):
            yield origin
        for argument in typing.get_args(annotation):
            yield from _types_in(argument)
    elif isinstance(annotation, types.UnionType):
        for argument in annotation.__args__:
            yield from _types_in(argument)
    elif isinstance(annotation, type):
        yield annotation


def test_no_interface_method_takes_or_returns_an_mlflow_type():
    namespace = {**vars(model), **vars(typing)}
    from martian.evals.server.principal import Principal
    from martian.evals.store import StoreStatus

    namespace.update(Principal=Principal, StoreStatus=StoreStatus)
    for name in STORE_METHODS:
        hints = typing.get_type_hints(getattr(EvalStore, name), globalns=namespace)
        for annotation in hints.values():
            for kind in _types_in(annotation):
                with_module = f"{kind.__module__}.{kind.__qualname__}"
                assert not with_module.startswith("mlflow"), f"{name} uses {with_module}"


def test_the_memory_store_implements_every_method():
    assert isinstance(MemoryStore(), EvalStore)
    assert len(STORE_METHODS) >= 30
    for name in STORE_METHODS:
        implementation = getattr(MemoryStore, name)
        assert inspect.iscoroutinefunction(implementation), name
        expected = list(inspect.signature(getattr(EvalStore, name)).parameters)
        assert list(inspect.signature(implementation).parameters) == expected, name
