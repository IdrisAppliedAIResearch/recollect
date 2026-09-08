"""Model provisioning is restartable and never extracts outside its target."""

import hashlib
import io
import json
import stat
import zipfile

import pytest

from recollect import voice_setup


def _archive(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return stream.getvalue()


@pytest.fixture
def downloads(monkeypatch):
    archive = _archive(
        [("vosk/am/final.mdl", b"model"), ("vosk/conf/model.conf", b"conf")]
    )
    payloads = {
        "vosk.zip": archive, "kokoro.onnx": b"tts", "voices.bin": b"voices",
        "silero-vad.onnx": b"vad",
    }
    assets = tuple(
        voice_setup.VoiceAsset(
            name,
            "https://models.example/" + name,
            len(payload),
            md5=(
                hashlib.md5(payload, usedforsecurity=False).hexdigest()
                if name != "silero-vad.onnx" else None
            ),
            directory="vosk" if name == "vosk.zip" else None,
            sha256=(
                hashlib.sha256(payload).hexdigest()
                if name == "silero-vad.onnx" else None
            ),
        )
        for name, payload in payloads.items()
    )
    calls = []

    def open_url(request, timeout):
        name = request.full_url.rsplit("/", 1)[-1]
        calls.append(name)
        return io.BytesIO(payloads[name])

    monkeypatch.setattr(voice_setup, "VOICE_ASSETS", assets)
    monkeypatch.setattr(voice_setup.urllib.request, "urlopen", open_url)
    return payloads, calls


def test_setup_reuses_verified_assets_and_repairs_only_corruption(tmp_path, downloads):
    _, calls = downloads
    paths = voice_setup.setup_voice(tmp_path)
    assert [path.name for path in paths] == [
        "vosk", "kokoro.onnx", "voices.bin", "silero-vad.onnx",
    ]
    assert (paths[0] / "am/final.mdl").read_bytes() == b"model"
    assert voice_setup.setup_voice(tmp_path) == paths
    assert calls == ["vosk.zip", "kokoro.onnx", "voices.bin", "silero-vad.onnx"]
    (paths[0] / "am/final.mdl").write_bytes(b"broken")
    paths[1].write_bytes(b"bad")
    voice_setup.setup_voice(tmp_path)
    assert calls[4:] == ["vosk.zip", "kokoro.onnx"]
    assert paths[1].read_bytes() == b"tts"
    assert (paths[0] / "am/final.mdl").read_bytes() == b"model"
    assert not list(tmp_path.glob(".voice-setup-*"))


def test_setup_adds_vad_without_redownloading_existing_models(
    tmp_path, downloads, monkeypatch,
):
    _, calls = downloads
    assets = voice_setup.VOICE_ASSETS
    monkeypatch.setattr(voice_setup, "VOICE_ASSETS", assets[:-1])
    voice_setup.setup_voice(tmp_path)
    calls.clear()
    monkeypatch.setattr(voice_setup, "VOICE_ASSETS", assets)
    voice_setup.setup_voice(tmp_path)
    assert calls == ["silero-vad.onnx"]
    assert (tmp_path / "silero-vad.onnx").read_bytes() == b"vad"


def test_pinned_sha256_rejects_same_size_download(tmp_path, downloads):
    payloads, _ = downloads
    voice_setup.setup_voice(tmp_path)
    target = tmp_path / "silero-vad.onnx"
    target.write_bytes(b"old installation")
    payloads["silero-vad.onnx"] = b"bad"
    with pytest.raises(ValueError, match="checksum"):
        voice_setup.setup_voice(tmp_path)
    assert target.read_bytes() == b"old installation"
    assert not list(tmp_path.glob(".voice-setup-*"))


@pytest.mark.parametrize("change_archive_digest", [False, True])
def test_receipt_cannot_override_pinned_sha256(
    tmp_path, downloads, change_archive_digest,
):
    _, calls = downloads
    voice_setup.setup_voice(tmp_path)
    target = tmp_path / "silero-vad.onnx"
    target.write_bytes(b"bad")
    digest = hashlib.sha256(b"bad").hexdigest()
    manifest = tmp_path / ".voice-models.json"
    receipts = json.loads(manifest.read_text())
    receipt = receipts["silero-vad.onnx"]
    receipt["files"]["silero-vad.onnx"] = digest
    if change_archive_digest:
        receipt["sha256"] = digest
    manifest.write_text(json.dumps(receipts))
    calls.clear()
    voice_setup.setup_voice(tmp_path)
    assert calls == ["silero-vad.onnx"]
    assert target.read_bytes() == b"vad"


@pytest.mark.parametrize("replacement", [b"t", b"ttss", b"bad"])
def test_bad_download_preserves_existing_file(tmp_path, downloads, replacement):
    payloads, _ = downloads
    voice_setup.setup_voice(tmp_path)
    target = tmp_path / "kokoro.onnx"
    target.write_bytes(b"old installation")
    payloads["kokoro.onnx"] = replacement
    with pytest.raises(ValueError, match="size|Incomplete|checksum"):
        voice_setup.setup_voice(tmp_path)
    assert target.read_bytes() == b"old installation"
    assert not list(tmp_path.glob(".voice-setup-*"))


@pytest.mark.parametrize(
    "name",
    [
        "../escape", "vosk/../../escape", "/escape", "vosk/..\\escape", "C:/escape",
        "vosk/CON.txt", "vosk/alias.", "vosk/alias ", ".",
    ],
)
def test_archive_traversal_is_rejected(tmp_path, name):
    archive = tmp_path / "model.zip"
    archive.write_bytes(_archive([(name, b"bad")]))
    with pytest.raises(ValueError, match="Unsafe|Unexpected"):
        voice_setup._extract(archive, tmp_path / "output", "vosk")
    assert not (tmp_path.parent / "escape").exists()


def test_archive_links_and_oversized_output_are_rejected(tmp_path, monkeypatch):
    link = zipfile.ZipInfo("vosk/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = tmp_path / "model.zip"
    archive.write_bytes(_archive([(link, "../elsewhere")]))
    with pytest.raises(ValueError, match="symbolic link"):
        voice_setup._extract(archive, tmp_path / "output", "vosk")
    archive.write_bytes(_archive([("vosk/model", b"too large")]))
    monkeypatch.setattr(voice_setup, "_MAX_EXTRACTED_BYTES", 2)
    with pytest.raises(ValueError, match="extraction size"):
        voice_setup._extract(archive, tmp_path / "output", "vosk")


def test_receipt_cannot_verify_files_outside_model_directory(tmp_path, downloads):
    voice_setup.setup_voice(tmp_path)
    manifest = tmp_path / ".voice-models.json"
    receipts = json.loads(manifest.read_text())
    receipts["voices.bin"]["files"] = {"../outside": "ignored"}
    manifest.write_text(json.dumps(receipts))
    voice_setup.setup_voice(tmp_path)
    assert downloads[1][-1] == "voices.bin"
