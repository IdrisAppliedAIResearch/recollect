"""Explicit, restartable provisioning of the local speech models."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class VoiceAsset:
    filename: str
    url: str
    size: int
    md5: str | None = None
    directory: str | None = None
    sha256: str | None = None


# Vosk publishes an MD5; the older Kokoro assets have no published digest.
# Silero's size and SHA-256 were measured from its pinned upstream commit.
# Receipts also detect corruption when setup reuses an installed model.
_KOKORO_RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
_SILERO_REVISION = "7e30209a3e901f9842f81b225f3e93d8199902b1"
VOICE_ASSETS = (
    VoiceAsset(
        "vosk-model-small-en-us-0.15.zip",
        "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
        41_205_931,
        md5="09ab50ccd62b674cbaa231b825f9c1cb",
        directory="vosk-model-small-en-us-0.15",
    ),
    VoiceAsset("kokoro-v1.0.onnx", f"{_KOKORO_RELEASE}/kokoro-v1.0.onnx", 325_532_387),
    VoiceAsset("voices-v1.0.bin", f"{_KOKORO_RELEASE}/voices-v1.0.bin", 28_214_398),
    VoiceAsset(
        "silero-vad.onnx",
        "https://raw.githubusercontent.com/snakers4/silero-vad/"
        f"{_SILERO_REVISION}/src/silero_vad/data/silero_vad.onnx",
        2_327_524,
        sha256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
    ),
)
WHISPER_REVISION = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
_WHISPER_URL = (
    "https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo/resolve/"
    + WHISPER_REVISION
)
WHISPER_ASSETS = tuple(
    VoiceAsset(name, f"{_WHISPER_URL}/{name}", size, sha256=digest)
    for name, size, digest in (
        ("config.json", 2263, None),
        ("model.bin", 1_617_884_929,
         "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"),
        ("preprocessor_config.json", 340, None),
        ("tokenizer.json", 2_710_337, None),
        ("vocabulary.json", 1_068_114, None),
    )
)
_MAX_ARCHIVE_FILES = 256
_MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{suffix}" for prefix in ("COM", "LPT") for suffix in "123456789¹²³"
}


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _relative_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or not path.parts
        or "\\" in name
        or ":" in name
        or path.is_absolute()
        or ".." in path.parts
        or any(ord(character) < 32 for character in name)
        or any(
            part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED
            for part in path.parts
        )
    ):
        raise ValueError(f"Unsafe voice model archive path: {name!r}")
    return path


def _download(asset: VoiceAsset, destination: Path) -> str:
    request = urllib.request.Request(asset.url, headers={"User-Agent": "Recollect/0.1"})
    digest = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with (
        urllib.request.urlopen(request, timeout=60) as response,
        destination.open("wb") as output,
    ):
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > asset.size:
                raise ValueError(f"Download exceeds expected size: {asset.filename}")
            digest.update(chunk)
            md5.update(chunk)
            output.write(chunk)
    if size != asset.size:
        raise ValueError(f"Incomplete download: {asset.filename} ({size}/{asset.size})")
    if asset.md5 and md5.hexdigest() != asset.md5:
        raise ValueError(f"Download checksum mismatch: {asset.filename}")
    if asset.sha256 and digest.hexdigest() != asset.sha256:
        raise ValueError(f"Download checksum mismatch: {asset.filename}")
    return digest.hexdigest()


def _extract(archive: Path, destination: Path, directory: str) -> dict[str, str]:
    root = destination.resolve()
    files = {}
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > _MAX_ARCHIVE_FILES:
            raise ValueError("Voice model archive contains too many files")
        if sum(member.file_size for member in members) > _MAX_EXTRACTED_BYTES:
            raise ValueError("Voice model archive exceeds extraction size limit")
        seen = set()
        for member in members:
            relative = _relative_path(member.filename)
            if not relative.parts or relative.parts[0] != directory:
                raise ValueError("Unexpected directory in voice model archive")
            normalized = str(relative).casefold()
            if normalized in seen or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(
                    "Duplicate path or symbolic link in voice model archive"
                )
            seen.add(normalized)
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Voice model archive path escapes destination")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as entry, target.open("xb") as output:
                shutil.copyfileobj(entry, output)
            files[str(relative)] = _sha256(target)
    if not files:
        raise ValueError("Voice model archive contains no model files")
    return files


def _complete(root: Path, asset: VoiceAsset, receipt: object) -> bool:
    if not isinstance(receipt, dict) or receipt.get("url") != asset.url:
        return False
    if receipt.get("size") != asset.size:
        return False
    if asset.sha256 and receipt.get("sha256") != asset.sha256:
        return False
    files = receipt.get("files")
    if not isinstance(files, dict) or not files:
        return False
    try:
        for name, digest in files.items():
            relative = _relative_path(name)
            if asset.directory:
                if relative.parts[0] != asset.directory:
                    return False
            elif str(relative) != asset.filename:
                return False
            if not asset.directory and asset.sha256 and digest != asset.sha256:
                return False
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or _sha256(target) != digest:
                return False
    except (OSError, ValueError, TypeError):
        return False
    return True


def setup_voice(model_dir: Path) -> list[Path]:
    """Download pinned assets, verify complete files, and return runtime paths.

    This is the only network operation for speech. It needs no optional speech
    packages; run it explicitly before starting voice mode. Existing files are
    replaced only after their replacements have downloaded and verified.
    """
    return _setup_assets(model_dir, VOICE_ASSETS)


def setup_whisper(model_dir: Path) -> list[Path]:
    """Provision the pinned Turbo conversion; recognition never downloads files."""
    return _setup_assets(model_dir, WHISPER_ASSETS)


def _setup_assets(model_dir: Path, assets: tuple[VoiceAsset, ...]) -> list[Path]:
    root = model_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / ".voice-models.json"
    try:
        receipts = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(receipts, dict):
            receipts = {}
    except (OSError, ValueError):
        receipts = {}
    paths = []
    for asset in assets:
        target = root / (asset.directory or asset.filename)
        paths.append(target)
        if _complete(root, asset, receipts.get(asset.filename)):
            continue
        if target.is_symlink() or target.is_junction():
            raise ValueError(f"Voice model target must not be a link: {target}")
        with tempfile.TemporaryDirectory(prefix=".voice-setup-", dir=root) as temporary:
            staging = Path(temporary).resolve()
            if not staging.is_relative_to(root):
                raise ValueError("Voice model staging directory escapes destination")
            download = staging / asset.filename
            digest = _download(asset, download)
            if asset.directory:
                files = _extract(download, staging, asset.directory)
                source = staging / asset.directory
            else:
                files = {asset.filename: digest}
                source = download
            # Preserve an existing installation until the new one is complete.
            previous = staging / "previous"
            if target.exists():
                target.replace(previous)
            try:
                source.replace(target)
            except OSError:
                if previous.exists():
                    previous.replace(target)
                raise
            receipts[asset.filename] = {
                "url": asset.url,
                "size": asset.size,
                "sha256": digest,
                "files": files,
            }
            pending = staging / "receipt.json"
            pending.write_text(json.dumps(receipts, indent=2) + "\n", encoding="utf-8")
            pending.replace(manifest)
    return paths
