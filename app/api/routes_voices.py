"""Voice Studio API: upload a reference clip, list/disable/delete voices, run a preview job and fetch its audio.

Admin only, tenant scoped, and only when the tenant's flag `voice_studio` is ON (default OFF; otherwise every endpoint is a 404 `voice_studio_disabled`).
The upload route parses the multipart body ITSELF, after the role and flag checks, so an unauthorised caller never makes the server read a large body.
No audio and no text is logged or audited (docs/VOICE_STUDIO_DESIGN.md)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ApiError, NotFound, Unprocessable
from app.security.auth import Principal, require_admin
from app.voices.studio import job_out

router = APIRouter(prefix="/v1/voices")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreviewIn(Strict):
    text: str = Field(max_length=2000)             # the real limit (300 characters, a setting) is applied by the service with a clear error code
    language: str = Field("auto", max_length=20)
    phone_quality: bool = False


class Gone(ApiError):
    status, code = 410, "gone"


async def studio_admin(request: Request, p: Principal = Depends(require_admin)) -> Principal:
    cfg = await request.app.state.ai.get_config(p.tenant_id)
    if not cfg.get("voice_studio"):
        raise NotFound("Voice Studio is not enabled for this tenant.", code="voice_studio_disabled")
    return p


def studio(request: Request):
    return request.app.state.voices


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@router.post("", status_code=201)
async def upload_voice(request: Request, p: Principal = Depends(studio_admin)):
    """multipart/form-data: file (the clip), name, speaker_name, consent ("true"), consent_scope ("preview_only" | "preview_and_calls")."""
    s = request.app.state.settings
    if not (request.headers.get("content-type") or "").lower().startswith("multipart/form-data"):
        raise Unprocessable("Send the clip as multipart/form-data.", code="multipart_required")
    form = await request.form(max_files=1, max_fields=8, max_part_size=4096)
    try:
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise Unprocessable("The file field is missing.", code="file_required")
        data = await f.read(s.voice_studio_max_upload_bytes + 1)     # never more than the limit (+1 to notice an oversized file) is held
        return await studio(request).upload(p.tenant_id, p.sub, request.state.request_id, data=data, name=str(form.get("name") or ""),
                                            speaker_name=str(form.get("speaker_name") or ""), consent=_truthy(form.get("consent")),
                                            scope=str(form.get("consent_scope") or "preview_only"))
    finally:
        await form.close()


@router.get("")
async def list_voices(request: Request, p: Principal = Depends(studio_admin)):
    return await studio(request).overview(p.tenant_id)


@router.post("/{voice_id}/disable")
async def disable_voice(request: Request, voice_id: str, p: Principal = Depends(studio_admin)):
    return await studio(request).disable(p.tenant_id, p.sub, request.state.request_id, voice_id)


@router.delete("/{voice_id}")
async def delete_voice(request: Request, voice_id: str, p: Principal = Depends(studio_admin)):
    return await studio(request).delete(p.tenant_id, p.sub, request.state.request_id, voice_id)


@router.post("/{voice_id}/preview", status_code=202)
async def preview(request: Request, voice_id: str, body: PreviewIn, p: Principal = Depends(studio_admin)):
    """Answers at once with a job id; poll GET /v1/voices/previews/{job_id}."""
    return await studio(request).start_preview(p.tenant_id, p.sub, request.state.request_id, voice_id, text=body.text, language=body.language,
                                               phone_quality=body.phone_quality)


@router.get("/previews/{job_id}")
async def preview_status(request: Request, job_id: str, p: Principal = Depends(studio_admin)):
    return job_out(studio(request).get_job(p.tenant_id, job_id))


@router.get("/previews/{job_id}/audio")
async def preview_audio(request: Request, job_id: str, p: Principal = Depends(studio_admin)):
    job = studio(request).get_job(p.tenant_id, job_id)
    if job.status != "done":
        raise ApiError("The preview is not ready.", code="preview_not_ready", status=409)
    if job.audio is None:
        raise Gone("The audio of this preview was dropped (previews are kept for a few minutes only). Run it again.", code="preview_expired")
    return Response(content=job.audio, media_type="audio/wav", headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="preview.wav"'})
