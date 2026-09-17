"""Opt-in: build and verify a non-target bundle image; no pull, rebuild or serving."""

import os
import shutil
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.deployment import BundleImages, SubagentBundle
from recollect.selfmod.files import materialize
from recollect.selfmod.journal import IntegrityError
from recollect.selfmod.native_runtime import NativeDocker

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOLLECT_RUN_SELFMOD_DOCKER_TESTS") != "1",
        reason="opt in to real selfmod Docker qualification",
    ),
]


@pytest.fixture
def images():
    executable = shutil.which("docker")
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT"}}
    root = (Path(os.environ["LOCALAPPDATA"]) / "recollect" / "sandboxes"
            / ("selfmod-bundle-cli-" + uuid.uuid4().hex))
    materialize(root, Snapshot((File("config.json", b'{"auths":{}}\n'),)))
    endpoint = subprocess.run(
        [executable, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        env=env, capture_output=True, check=True, text=True).stdout.strip()
    argv = (executable, "--config", str(root), "--host", endpoint)
    base = subprocess.run([*argv, "image", "inspect", "--format", "{{.Id}}",
                           "recollect-opencode-sandbox:1.18.18"], env=env,
                          capture_output=True, check=True, text=True).stdout.strip()
    built = []
    try:
        yield NativeDocker(argv, tuple(env.items())), base, built
    finally:
        for image in built:
            subprocess.run([*argv, "image", "rm", image], env=env,
                           capture_output=True, check=False)
        shutil.rmtree(root)


async def test_bundle_image_serves_exact_accepted_bytes_only(images):
    docker, base, built = images
    candidate = Snapshot((
        File("dependencies.lock", b"httpx==0.28.1\n"),
        File("tools/generic_research.py", b"def run():\n    return 'non-target'\n"),
    ))
    bundle = SubagentBundle(candidate, base, (("entrypoint", "generic"),))
    store = BundleImages(docker)
    image = await store.build(bundle)
    built.append(image)
    assert await store.verify(bundle, image)
    # Identical bytes reuse the verified image instead of piling up a new one.
    assert await store.build(bundle) == image
    tampered = replace(bundle, candidate=Snapshot((
        *candidate.files[:1], File("tools/generic_research.py", b"changed\n"))))
    with pytest.raises(IntegrityError):
        await store.verify(tampered, image)
    with pytest.raises(IntegrityError):
        await store.verify(bundle, base)
