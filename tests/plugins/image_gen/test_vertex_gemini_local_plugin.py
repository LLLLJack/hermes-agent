"""Behavior tests for the VPS-local Vertex image provider."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[3]


def _load_plugin():
    path = ROOT / "plugins/image_gen/vertex-gemini/__init__.py"
    spec = importlib.util.spec_from_file_location("vps_vertex_gemini_image", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provider_declares_image_edit_capabilities(tmp_path: Path):
    module = _load_plugin()
    provider = module.VertexGeminiImageGenProvider()

    assert provider.capabilities() == {
        "modalities": ["text", "image"],
        "max_reference_images": 14,
    }

    source = tmp_path / "source.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nlocal-test")
    with httpx.Client() as client:
        part = module._image_part(str(source), client)

    body = module._request_body("edit this", "portrait", {}, [part])
    assert part["inlineData"]["mimeType"] == "image/png"
    assert body["contents"]["parts"] == [{"text": "edit this"}, part]
    assert body["generationConfig"]["imageConfig"]["aspectRatio"] == "9:16"


def test_runtime_reuses_official_vertex_credentials(monkeypatch):
    module = _load_plugin()
    monkeypatch.setattr(
        module,
        "get_vertex_credentials",
        lambda: ("short-lived-token", "credential-project"),
    )
    monkeypatch.setattr(
        module,
        "_load_config",
        lambda: {"vertex": {"project_id": "configured-project", "region": "global"}},
    )
    monkeypatch.setattr(module, "_scoped_env", lambda _name: "")

    runtime = module._resolve_runtime()

    assert runtime == {
        "token": "short-lived-token",
        "project": "configured-project",
        "region": "global",
        "base_url": (
            "https://aiplatform.googleapis.com/v1/projects/configured-project/"
            "locations/global/publishers/google"
        ),
    }


def test_scoped_vertex_model_config_wins_over_top_level(monkeypatch):
    module = _load_plugin()
    monkeypatch.setattr(
        module,
        "_load_config",
        lambda: {
            "image_gen": {
                "model": "gpt-image-2",
                "vertex-gemini": {"model": "gemini-2.5-flash-image"},
            }
        },
    )
    monkeypatch.setattr(module, "_scoped_env", lambda _name: "")

    assert module._resolve_model()[0] == "gemini-2.5-flash-image"
