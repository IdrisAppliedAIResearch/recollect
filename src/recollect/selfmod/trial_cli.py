"""``recollect selfmod-freeze`` and ``recollect selfmod-trial`` entry points.

Freeze is the user's preregistration step: it writes a manifest and nothing else.
The trial loads that manifest, opens a primary-mode controller and runs the
unattended orchestrator through CP6. A failure to start the live environment is
itself recorded and accounted; it never leaves an unaccounted attempt.
"""

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from . import acceptance, subagent_tree, trial_manifest
from .candidate_evaluator import EVALUATION_CHECKS
from .checkpoints import materialize
from .contracts import File, Snapshot
from .controller import Controller, ControllerConfig
from .journal import encode


def pinned_docker(share_root):
    """A private-config Docker CLI bound to the current context's endpoint."""
    from .native_runtime import NativeDocker

    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("Docker executable was not found")
    environment = {k: v for k, v in os.environ.items()
                   if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH",
                                    "PATHEXT"}}
    endpoint = subprocess.run(
        [executable, "context", "inspect", "--format",
         "{{.Endpoints.docker.Host}}"], env=environment, capture_output=True,
        check=True, text=True, timeout=120).stdout.strip()
    config_dir = Path(share_root) / ("selfmod-trial-cli-" + uuid.uuid4().hex)
    config_dir.parent.mkdir(parents=True, exist_ok=True)
    materialize(config_dir, Snapshot((File("config.json", b'{"auths":{}}\n'),)))
    return NativeDocker((executable, "--config", str(config_dir), "--host", endpoint),
                        tuple(environment.items()))


def freeze_main(inputs_path, output_path, repository):
    from ..config import RecollectConfig

    inputs = json.loads(Path(inputs_path).read_text(encoding="utf-8"))
    config = RecollectConfig.from_env()
    identities = asyncio.run(trial_manifest.collect_identities(
        repository, base_url=config.generator_base_url,
        model=config.generator_model, weight_path=inputs["weight_path"],
        base_image_id=inputs["base_image_id"], calendar=inputs["calendar"],
        credential_store=Path(inputs["credential_store"])))
    output = Path(output_path)
    if output.exists():
        raise FileExistsError("A frozen manifest is never overwritten")
    output.write_bytes(trial_manifest.freeze(identities, inputs))
    print(f"frozen manifest {output} sha256 "
          f"{trial_manifest.file_sha256(output)}")
    return 0


def controller_config(manifest, repository):
    value = manifest.value
    tree = subagent_tree.baseline(repository)
    policy = subagent_tree.change_policy(tree)
    return ControllerConfig(
        value["attempt_id"],
        acceptance.task_contract(value["experiment"]["request"], policy),
        manifest.registrations(), tree.sha256, value["evaluator"]["sha256"],
        EVALUATION_CHECKS)


async def run_trial(manifest_path, attempt_root, repository, credential_store,
                    *, serve_port=None):
    from ..config import RecollectConfig
    from .trial import TrialOrchestrator
    from .trial_live import LiveSettings, LiveTrialEnvironment

    manifest = trial_manifest.RuntimeManifest.load(manifest_path)
    attempt_root = Path(attempt_root)
    attempt_root.mkdir(parents=True, exist_ok=False)
    base = RecollectConfig.from_env()
    controller = Controller.create(attempt_root / "controller",
                                   controller_config(manifest, repository),
                                   mode="primary")
    environment = LiveTrialEnvironment(LiveSettings(
        repository=Path(repository), manifest=manifest, attempt_root=attempt_root,
        docker=pinned_docker(base.sandbox_root), credential_store=credential_store,
        base_config=base))
    server = None
    try:
        try:
            await environment.start()
        except Exception as error:  # noqa: BLE001 - start failure is accounted
            controller.fail("environment_start_failed:" + type(error).__name__)
            receipts = await environment.finish(error)
            evidence = Snapshot((*receipts.files, File(
                "trial/start-failure.json", encode({"error_type": type(error).__name__,
                                                    "error": str(error)[:2048]}))))
            return await asyncio.to_thread(controller.account, evidence), controller
        if serve_port is not None:
            server = await _serve(environment, serve_port)
        cp6 = await TrialOrchestrator(controller, environment).run()
        return cp6, controller
    finally:
        if server is not None:
            server.should_exit = True
        controller.close()


async def _serve(environment, port):
    """Observe the attempt in the UI; the served app shares the trial's state."""
    import uvicorn

    from ..api import create_app

    app = create_app(environment.config, state_factory=lambda _: environment.state)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="off"))
    app.state.recollect = environment.state
    asyncio.create_task(server.serve())
    return server


def trial_main(manifest_path, attempt_root, repository, credential_store, serve_port):
    cp6, controller = asyncio.run(run_trial(
        manifest_path, attempt_root, repository, credential_store,
        serve_port=serve_port))
    result = controller._checkpoints[-1].value["observations"]["result"]
    print(f"CP6 {cp6} result {result}")
    return 0 if result == "primary_complete" else 1
