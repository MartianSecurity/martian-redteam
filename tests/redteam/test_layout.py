"""The red team core's package layout, with every old import kept (martian-notes#230)."""

import importlib

import pytest

import martian_redteam

SUBPACKAGES = ["model", "engine", "verify", "catalog", "server", "store"]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_each_part_of_the_core_imports(name):
    module = importlib.import_module(f"martian.redteam.{name}")
    assert module.__doc__
    assert getattr(module, "__all__", None)


@pytest.mark.parametrize("name", martian_redteam.__all__)
def test_every_legacy_name_is_still_importable_from_both_packages(name):
    import martian.redteam

    assert getattr(martian_redteam, name) is not None
    # martian.redteam.Client is the hosted client; the legacy direct-Agents client keeps its name in
    # martian_redteam. Every other name is the same object in both packages.
    if name != "Client":
        assert getattr(martian_redteam, name) is getattr(martian.redteam, name)
    assert name in martian.redteam.__all__


def test_the_catalogue_and_engine_are_the_legacy_objects():
    from martian.redteam import catalog, engine
    from martian_redteam import catalog as legacy_catalog
    from martian_redteam import orchestrator

    assert catalog.PromptLeakage is legacy_catalog.PromptLeakage
    assert catalog.CATALOG_VERSION == legacy_catalog.CATALOG_VERSION
    assert engine.a_red_team is orchestrator.a_red_team


def test_the_core_runs_without_a_martian_backend(monkeypatch):
    monkeypatch.delenv("MARTIAN_API_URL", raising=False)
    monkeypatch.delenv("MARTIAN_REDTEAM_API_URL", raising=False)
    from martian.redteam.model import RedteamRun
    from martian.redteam.verify import load_vectors, run_vector

    assert RedteamRun(id="r", catalog_version="0.2.0").counts.attempts == 0
    assert all(run_vector(vector) == vector["expected"] for vector in load_vectors()["vectors"])
