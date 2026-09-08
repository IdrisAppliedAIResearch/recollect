"""Whisper provisioning is pinned and reuses verified local assets."""

import hashlib
import io

import pytest

from recollect import voice_setup


def test_whisper_setup_reuses_files_and_repairs_only_corruption(tmp_path, monkeypatch):
    payloads = {"config.json": b"{}", "model.bin": b"weights"}
    assets = tuple(
        voice_setup.VoiceAsset(
            name, "https://models.example/" + name, len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        for name, payload in payloads.items()
    )
    calls = []

    def download(request, timeout):
        name = request.full_url.rsplit("/", 1)[-1]
        calls.append(name)
        return io.BytesIO(payloads[name])

    monkeypatch.setattr(voice_setup, "WHISPER_ASSETS", assets)
    monkeypatch.setattr(voice_setup.urllib.request, "urlopen", download)
    paths = voice_setup.setup_whisper(tmp_path)
    assert [path.name for path in paths] == list(payloads)
    assert voice_setup.setup_whisper(tmp_path) == paths
    assert calls == list(payloads)
    (tmp_path / "model.bin").write_bytes(b"damaged")
    voice_setup.setup_whisper(tmp_path)
    assert calls == ["config.json", "model.bin", "model.bin"]
    assert (tmp_path / "model.bin").read_bytes() == b"weights"

    payloads["model.bin"] = b"corrupt"
    (tmp_path / "model.bin").write_bytes(b"old weights")
    with pytest.raises(ValueError, match="checksum"):
        voice_setup.setup_whisper(tmp_path)
    assert (tmp_path / "model.bin").read_bytes() == b"old weights"
    assert not list(tmp_path.glob(".voice-setup-*"))


def test_whisper_assets_use_one_immutable_revision_and_pinned_weight_digest():
    assert {asset.filename for asset in voice_setup.WHISPER_ASSETS} == {
        "config.json", "model.bin", "preprocessor_config.json",
        "tokenizer.json", "vocabulary.json",
    }
    for asset in voice_setup.WHISPER_ASSETS:
        assert f"/resolve/{voice_setup.WHISPER_REVISION}/" in asset.url
    weights = next(a for a in voice_setup.WHISPER_ASSETS if a.filename == "model.bin")
    assert weights.sha256 and len(weights.sha256) == 64
