"""Vertex AI Gemini image generation and editing backend.

This local plugin deliberately reuses Hermes' official Vertex credential
loader.  It needs no inline service-account JSON support and does not modify
the main chat-provider routing.  Select it explicitly with
``image_gen.provider: vertex-gemini``.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import httpx

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)
from agent.vertex_adapter import get_vertex_credentials, has_vertex_credentials

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-image"
MAX_REFERENCE_IMAGES = 14
MAX_SOURCE_BYTES = 25 * 1024 * 1024

_MODELS: Dict[str, Dict[str, Any]] = {
    "gemini-3-pro-image-preview": {
        "display": "Gemini 3 Pro Image Preview",
        "speed": "~8s",
        "strengths": "Highest Gemini image quality, text rendering, and editing",
        "image_size": "2K",
    },
    "gemini-3.1-flash-image": {
        "display": "Gemini 3.1 Flash Image",
        "speed": "~4s",
        "strengths": "Fast generation and multi-reference image editing",
        "image_size": "2K",
    },
    "gemini-3.1-flash-image-preview": {
        "display": "Gemini 3.1 Flash Image Preview",
        "speed": "~4s",
        "strengths": "Fast preview generation and multi-reference editing",
        "image_size": "2K",
    },
    "gemini-2.5-flash-image": {
        "display": "Gemini 2.5 Flash Image",
        "speed": "~2s",
        "strengths": "Generally available generation and editing",
        "image_size": "",
    },
}

_ASPECT_TO_GEMINI = {"landscape": "16:9", "square": "1:1", "portrait": "9:16"}


def _scoped_env(name: str) -> str:
    try:
        from agent.secret_scope import get_secret

        return str(get_secret(name, "") or "").strip()
    except ImportError:
        return ""


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        return config if isinstance(config, dict) else {}
    except Exception as exc:  # noqa: BLE001 - config failures become setup errors
        logger.debug("Could not load Vertex image configuration: %s", exc)
        return {}


def _resolve_model(explicit: Any = None) -> Tuple[str, Dict[str, Any]]:
    config = _load_config()
    image_config = config.get("image_gen") if isinstance(config.get("image_gen"), dict) else {}
    scoped = (
        image_config.get("vertex-gemini")
        if isinstance(image_config.get("vertex-gemini"), dict)
        else {}
    )
    candidate = str(
        explicit
        or _scoped_env("VERTEXAI_IMAGE_MODEL")
        or scoped.get("model")
        or image_config.get("model")
        or DEFAULT_MODEL
    ).strip()
    return candidate, _MODELS.get(candidate, {"display": candidate, "image_size": ""})


def _resolve_runtime() -> Dict[str, str]:
    token, credential_project = get_vertex_credentials()
    if not token or not credential_project:
        raise RuntimeError(
            "Vertex AI credentials could not be resolved. Set "
            "VERTEX_CREDENTIALS_PATH to the mounted service-account JSON."
        )

    config = _load_config()
    vertex = config.get("vertex") if isinstance(config.get("vertex"), dict) else {}
    project = str(
        _scoped_env("VERTEX_PROJECT_ID")
        or vertex.get("project_id")
        or credential_project
    ).strip()
    region = str(_scoped_env("VERTEX_REGION") or vertex.get("region") or "global").strip()
    if not project:
        raise RuntimeError("Vertex AI project ID is not configured")
    if not region:
        region = "global"

    image_config = config.get("image_gen") if isinstance(config.get("image_gen"), dict) else {}
    scoped = (
        image_config.get("vertex-gemini")
        if isinstance(image_config.get("vertex-gemini"), dict)
        else {}
    )
    override = str(scoped.get("base_url") or "").strip().rstrip("/")
    if override:
        base_url = override
    else:
        host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
        base_url = (
            f"https://{host}/v1/projects/{project}/locations/{region}"
            "/publishers/google"
        )
    return {
        "token": token,
        "project": project,
        "region": region,
        "base_url": base_url,
    }


def _local_path(reference: str) -> Path:
    if reference.lower().startswith("file://"):
        return Path(unquote(urlparse(reference).path))
    return Path(reference)


def _image_part(reference: str, client: httpx.Client) -> Dict[str, Any]:
    reference = reference.strip()
    lower = reference.lower()
    if lower.startswith("data:"):
        header, separator, payload = reference.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ValueError("source image data URI must be base64 encoded")
        media_type = header[5:].split(";", 1)[0] or "image/png"
        raw = base64.b64decode(payload, validate=True)
    elif lower.startswith(("http://", "https://")):
        response = client.get(reference, timeout=60.0)
        response.raise_for_status()
        raw = response.content
        media_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip()
        if not media_type.startswith("image/"):
            media_type = mimetypes.guess_type(reference.split("?", 1)[0])[0] or "image/png"
    else:
        path = _local_path(reference)
        raw = path.read_bytes()
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"

    if not raw:
        raise ValueError(f"source image is empty: {reference}")
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(
            f"source image exceeds {MAX_SOURCE_BYTES // (1024 * 1024)}MB: {reference}"
        )
    return {
        "inlineData": {
            "mimeType": media_type,
            "data": base64.b64encode(raw).decode("ascii"),
        }
    }


def _request_body(
    prompt: str,
    aspect: str,
    model_meta: Dict[str, Any],
    source_parts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    image_config: Dict[str, Any] = {"aspectRatio": _ASPECT_TO_GEMINI[aspect]}
    image_size = str(model_meta.get("image_size") or "").strip()
    if image_size:
        image_config["imageSize"] = image_size
    return {
        "contents": {"role": "USER", "parts": [{"text": prompt}, *source_parts]},
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": image_config,
        },
    }


def _extract_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    except Exception:  # noqa: BLE001 - fall back to bounded response text
        pass
    text = (response.text or "").strip()
    return text[:500] if text else f"HTTP {response.status_code}"


def _parse_image(payload: Dict[str, Any]) -> Tuple[str, str]:
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline, dict):
                continue
            data = str(inline.get("data") or "").strip()
            media_type = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
            if data:
                extension = "webp" if "webp" in media_type.lower() else "jpg" if "jpeg" in media_type.lower() else "png"
                return data, extension
    raise ValueError("Vertex Gemini returned no image data")


class VertexGeminiImageGenProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "vertex-gemini"

    @property
    def display_name(self) -> str:
        return "Vertex AI Gemini"

    def is_available(self) -> bool:
        return has_vertex_credentials()

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": "Google Cloud pricing",
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": MAX_REFERENCE_IMAGES}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Vertex AI Gemini",
            "badge": "paid",
            "tag": "Gemini image generation via a mounted Vertex service-account file",
            "env_vars": [
                {
                    "key": "VERTEX_CREDENTIALS_PATH",
                    "prompt": "Service-account JSON path",
                    "url": "https://cloud.google.com/docs/authentication/set-up-adc-on-premises",
                }
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=self.name,
                aspect_ratio=aspect,
            )

        sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            sources.append(image_url.strip())
        sources.extend(normalize_reference_images(reference_image_urls) or [])
        sources = sources[:MAX_REFERENCE_IMAGES]
        modality = "image" if sources else "text"
        model, model_meta = _resolve_model(kwargs.get("model"))

        try:
            runtime = _resolve_runtime()
            with httpx.Client(
                headers={"Authorization": f"Bearer {runtime['token']}"},
                follow_redirects=True,
                timeout=300.0,
            ) as client:
                source_parts = [_image_part(reference, client) for reference in sources]
                response = client.post(
                    f"{runtime['base_url']}/models/{model}:generateContent",
                    json=_request_body(prompt, aspect, model_meta, source_parts),
                )
                if response.is_error:
                    return error_response(
                        error=_extract_error(response),
                        error_type="api_error",
                        provider=self.name,
                        model=model,
                        prompt=prompt,
                        aspect_ratio=aspect,
                    )
                encoded, extension = _parse_image(response.json())
                base64.b64decode(encoded, validate=True)
                path = save_b64_image(
                    encoded,
                    prefix=f"vertex_gemini_{model}",
                    extension=extension,
                )
        except Exception as exc:  # noqa: BLE001 - provider returns structured errors
            logger.debug("Vertex Gemini image generation failed", exc_info=True)
            return error_response(
                error=f"Vertex Gemini image generation failed: {exc}",
                error_type="api_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=str(path),
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
            modality=modality,
            extra={
                "source_images": len(sources),
                "project": runtime["project"],
                "location": runtime["region"],
            },
        )


def register(ctx) -> None:
    ctx.register_image_gen_provider(VertexGeminiImageGenProvider())

