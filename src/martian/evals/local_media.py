"""Downloads for verified media carried by a local Promptfoo import."""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response

from martian.evals._promptfoo_artifacts import MEDIA_HASH, MEDIA_PREFIX
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep
from martian.evals.server.runs import find_run

router = APIRouter(prefix="/v1/local/evals", tags=["local eval media"])
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "video/mp4": "mp4",
    "video/webm": "webm",
}


@router.get("/runs/{run_id:uuid}/promptfoo-media/{sha256}")
async def download_promptfoo_media(
    run_id: uuid.UUID,
    sha256: Annotated[str, Path(pattern=MEDIA_HASH, min_length=64, max_length=64)],
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
) -> Response:
    run = await find_run(store, records, principal, run_id)
    source = run.provenance.get("promptfoo")
    media = source.get("media") if isinstance(source, dict) else None
    assets = media.get("assets") if isinstance(media, dict) else None
    asset = assets.get(sha256) if isinstance(assets, dict) else None
    if (
        run.kind != "eval"
        or run.state != "completed"
        or not isinstance(asset, dict)
        or asset.get("available") is not True
    ):
        raise HTTPException(404, "Imported media is not available for this run")
    data = await store.get_artifact(principal, run.project_id, run.id, MEDIA_PREFIX + sha256)
    if data is None:
        raise HTTPException(404, "Imported media is not available for this run")
    if asset.get("size_bytes") != len(data) or hashlib.sha256(data).hexdigest() != sha256:
        raise HTTPException(409, "Stored media does not match the imported archive")
    mime_type = asset.get("mime_type")
    extension = _EXTENSIONS.get(mime_type, "bin") if isinstance(mime_type, str) else "bin"
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="promptfoo-{sha256}.{extension}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
