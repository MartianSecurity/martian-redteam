"""The Dockerfile of the local edition's image: what it sets must be what `martian ui` reads."""

import re
from pathlib import Path

from martian.evals import local

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def runtime_stage() -> str:
    """The instructions of the last stage, the image that runs."""
    return re.split(r"^FROM ", DOCKERFILE, flags=re.MULTILINE)[-1]


def test_every_base_image_is_pinned_by_digest():
    bases = re.findall(r"^(?:ARG PYTHON_IMAGE=|FROM )(\S+)", DOCKERFILE, flags=re.MULTILINE)
    pinned = [base for base in bases if base != "${PYTHON_IMAGE}"]
    assert pinned and all(re.search(r"@sha256:[0-9a-f]{64}$", base) for base in pinned), pinned


def test_the_image_runs_martian_ui_as_a_user_with_its_data_in_a_volume():
    stage = runtime_stage()
    users = re.findall(r"^USER (\S+)", stage, flags=re.MULTILINE)
    assert users and users[-1] not in ("root", "0"), users
    assert f"{local.BIND_ENV}=0.0.0.0" in stage
    assert "MARTIAN_EVALS_HOME=/data" in stage
    assert 'VOLUME ["/data"]' in stage
    assert 'CMD ["martian", "ui", "--no-open"]' in stage


def test_the_build_context_holds_what_the_build_copies():
    # CI does not build the image, so a file .dockerignore leaves out would fail only a real build.
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    admitted = {line[1:].removesuffix("/**").rstrip("/") for line in ignore if line.startswith("!")}
    sources = re.findall(r"^COPY (?!--from)(.+) \S+$", DOCKERFILE, flags=re.MULTILINE)
    copied = {path for line in sources for path in line.split()}
    assert copied and copied <= admitted, copied - admitted


def test_the_image_installs_the_wheel_in_dist_and_cannot_build_the_package():
    # The release publishes the image with the wheel it checked; a build of the checkout here would
    # put a second build in the image. The context holds no source to build from.
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert sorted(line for line in ignore if line.startswith("!")) == ["!dist/*.whl", "!pyproject.toml", "!uv.lock"]
    instructions = "\n".join(line for line in DOCKERFILE.splitlines() if not line.lstrip().startswith("#"))
    assert "COPY dist/*.whl /dist/" in instructions
    assert not re.search(r"\b(?:uv|pip|python -m) build\b|hatch", instructions)
