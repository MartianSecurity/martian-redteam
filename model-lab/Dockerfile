FROM python:3.12-slim AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /usr/local/bin/uv
ARG MODEL_DEVICE=cpu
ENV UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /lab
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra "$MODEL_DEVICE"

FROM dependencies AS weights
COPY src/action_security_lab/model_spec.py ./model_spec.py
COPY download_model.py ./
RUN uv run --no-sync python download_model.py

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /lab
COPY --from=dependencies /lab/.venv /lab/.venv
COPY --from=weights /opt/model /opt/model
COPY src ./src
RUN useradd --system --uid 10001 --no-create-home modelrunner
COPY licenses /usr/share/licenses/martian-model-lab
USER 10001
ENV PATH="/lab/.venv/bin:$PATH" PYTHONPATH=/lab/src HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
ENTRYPOINT ["python", "-m", "action_security_lab.benchmark"]
