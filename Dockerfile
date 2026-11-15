# The local edition (`martian ui`) for one user on a small server: the wheel in dist/ with the
# `evals` extra, its dependencies at the versions uv.lock pins, the data folder in /data. The image
# installs that wheel and never builds the package itself, so a release's image holds the wheel the
# release checked and publishes. See docs/evals-core.md#the-local-edition-in-a-container.
#
# It has no login. Publish its port on 127.0.0.1 only and never expose it to a public network.
#
#   rm -rf dist && uv build --wheel
#   docker build -t martian-evals .
#   docker run -d --name martian-evals -p 127.0.0.1:7600:7600 -v martian-evals:/data martian-evals

ARG PYTHON_IMAGE=python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY dist/*.whl /dist/
RUN set -eu; set -- /dist/*.whl; \
    if [ "$#" -ne 1 ]; then echo "dist/ must hold exactly one wheel; it holds $#: $*" >&2; exit 1; fi; \
    uv export --locked --no-dev --extra evals --no-emit-project --no-header --output-file /tmp/requirements.txt \
 && uv venv --python /usr/local/bin/python3.12 /opt/martian \
 && uv pip install --python /opt/martian/bin/python --no-deps --require-hashes --compile-bytecode \
      --requirements /tmp/requirements.txt \
 && uv pip install --python /opt/martian/bin/python --no-deps --compile-bytecode "$1" \
 && uv pip check --python /opt/martian/bin/python

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="Martian Evals, local edition" \
      org.opencontainers.image.source="https://github.com/MorningHQ/martian-redteam" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN groupadd --gid 10001 martian \
 && useradd --uid 10001 --gid martian --home-dir /home/martian --create-home --shell /usr/sbin/nologin martian \
 && install -d -o martian -g martian /data
COPY --from=build /opt/martian /opt/martian
# MARTIAN_EVALS_BIND puts the server on the container's interfaces so that Docker can publish the
# port; the Host check stays on (see src/martian/evals/local.py).
ENV PATH=/opt/martian/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MARTIAN_EVALS_HOME=/data \
    MARTIAN_EVALS_BIND=0.0.0.0
USER martian
WORKDIR /home/martian
EXPOSE 7600
VOLUME ["/data"]
CMD ["martian", "ui", "--no-open"]
