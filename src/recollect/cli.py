"""Command line entry points.

``doctor`` exists because this system has four things that can be wrong
before a single word is exchanged - the embedder artifact, the ASPECT
parser, the store's call-shape gate, and the generator - and each fails in
a way that is obvious once named and baffling otherwise. It checks all of
them and says which one is broken.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        from .launch import standalone_main

        return standalone_main([])
    parser = argparse.ArgumentParser(
        prog="recollect",
        description=(
            "A harness for episodic conversational memory, instrumented so "
            "every retrieval decision is visible. On Windows, recollect with "
            "no arguments launches the complete standalone application."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the API and UI server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--mode", choices=("standalone", "host", "client"))
    serve.add_argument("--desktop-url", help="desktop Recollect origin for client mode")
    serve.add_argument("--token-file", type=Path,
                       help="private host/client pairing bundle or legacy token file")
    serve.add_argument("--ssl-certfile", type=Path, help="host TLS certificate file")
    serve.add_argument("--ssl-keyfile", type=Path, help="host TLS private key file")
    serve.add_argument(
        "--allow-http-loopback", action="store_true", default=None,
        help="allow a legacy token over an explicitly configured loopback tunnel",
    )
    serve.add_argument("--env-file", type=Path, default=Path(".env"))

    subparsers.add_parser("doctor", help="check the embedder, store and generator")

    chat = subparsers.add_parser("chat", help="a terminal conversation")
    chat.add_argument("--session", default=None, help="session id to continue")

    voice_setup = subparsers.add_parser(
        "voice-setup", help="download the local speech models"
    )
    voice_setup.add_argument("--model-dir", type=Path, default=None)
    voice_setup.add_argument("--whisper", action="store_true",
                             help="also provision GPU Whisper Turbo transcription")
    voice_setup.add_argument("--asr-model-dir", type=Path, default=None)
    subparsers.add_parser("voice-doctor", help="load and check local speech models")

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    if args.command == "doctor":
        return asyncio.run(_doctor())
    if args.command == "chat":
        return asyncio.run(_chat(args.session))
    if args.command == "voice-setup":
        from .voice_setup import setup_voice, setup_whisper

        config = _load_config()
        try:
            for path in setup_voice(args.model_dir or config.voice_model_dir):
                print(f"  ready: {path}")
            if args.whisper:
                for path in setup_whisper(
                    args.asr_model_dir or config.voice_asr_model_dir,
                ):
                    print(f"  ready: {path}")
        except Exception as error:
            print(f"Speech setup failed: {error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "voice-doctor":
        from .engine.voice import VoiceService

        config = _load_config()
        try:
            voice = VoiceService(config)
            voice.warm_up()
        except Exception as error:
            print(f"Speech check failed: {error}", file=sys.stderr)
            return 1
        print(f"Local speech ready. Wake phrase: {config.voice_wake_phrase}")
        print(f"Transcription: {config.voice_asr_backend}")
        print(f"Kokoro execution: {voice.status()['provider']}")
        return 0
    return 1


def _serve(args) -> int:
    import uvicorn
    from dotenv import load_dotenv

    from .deployment import DeploymentConfig

    os.environ["RECOLLECT_ENV_FILE"] = str(args.env_file.resolve())
    if args.env_file.is_file():
        load_dotenv(args.env_file)
    # Uvicorn's factory and reload child must see the same explicit choices.
    for field, variable in (
        ("mode", "RECOLLECT_DEPLOYMENT_MODE"),
        ("host", "RECOLLECT_HOST"),
        ("port", "RECOLLECT_PORT"),
        ("desktop_url", "RECOLLECT_DESKTOP_URL"),
        ("ssl_certfile", "RECOLLECT_SSL_CERTFILE"),
        ("ssl_keyfile", "RECOLLECT_SSL_KEYFILE"),
        ("allow_http_loopback", "RECOLLECT_ALLOW_HTTP_LOOPBACK"),
    ):
        value = getattr(args, field)
        if value is not None:
            os.environ[variable] = str(value.absolute() if isinstance(value, Path)
                                       else value)
    try:
        if args.token_file is not None:
            from .pairing import load_pairing

            credential = load_pairing(args.token_file)
            os.environ["RECOLLECT_DEPLOYMENT_TOKEN"] = credential.token
            if credential.certificate_pem:
                os.environ["RECOLLECT_TRUSTED_CERTIFICATE_PEM"] = (
                    credential.certificate_pem
                )
                if os.environ.get("RECOLLECT_DEPLOYMENT_MODE") == "host":
                    for variable, suffix in (
                        ("RECOLLECT_SSL_CERTFILE", ".tls.crt"),
                        ("RECOLLECT_SSL_KEYFILE", ".tls.key"),
                    ):
                        os.environ.setdefault(variable, str(args.token_file.with_name(
                            args.token_file.name + suffix,
                        ).absolute()))
            else:
                os.environ.pop("RECOLLECT_TRUSTED_CERTIFICATE_PEM", None)
        config = DeploymentConfig.from_env(env_file=None)
    except (ValueError, OSError) as error:
        print(f"Deployment configuration failed: {error}", file=sys.stderr)
        return 1
    scheme = "https" if config.ssl_certfile else "http"
    print(f"recollect {config.mode} serving on {scheme}://{config.host}:{config.port}")
    if config.mode != "host":
        print(f"  inspector : {scheme}://{config.host}:{config.port}/")
    if config.mode == "client":
        print(f"  desktop   : {config.desktop_url}")
    tls_options = {}
    if config.ssl_certfile:
        tls_options = {
            "ssl_certfile": str(config.ssl_certfile),
            "ssl_keyfile": str(config.ssl_keyfile),
        }
    uvicorn.run(
        "recollect.deployment:create_app",
        factory=True,
        host=config.host,
        port=config.port,
        reload=args.reload,
        proxy_headers=False,
        ws_max_size=65_536,
        ws_max_queue=1,
        ws_per_message_deflate=False,
        **tls_options,
    )
    return 0


def _load_config():
    from .config import RecollectConfig

    return RecollectConfig.from_env()


async def _doctor() -> int:
    from .engine._internals import LIBRARY_VERSION, load_aspect_model
    from .engine.embedder import EXPECTED_SENTINEL_SHA256, HarnessEmbedder
    from .engine.generator import Generator, GeneratorSettings

    config = _load_config()
    ok = True

    print("recollect doctor\n")
    print(f"  episodic library : {LIBRARY_VERSION}")
    print(f"  budget           : {config.budget_chars} chars")
    print(f"  model file       : {config.embedding_model_path}")

    if not config.embedding_model_path.is_file():
        print("  FAIL: the embedding model file does not exist.")
        return 1

    print("\nembedder (in-process, pinned)")
    try:
        embedder = HarnessEmbedder(
            config.embedding_model_path, n_threads=config.embedding_threads
        )
        health = await asyncio.to_thread(embedder.warm_up)
        print(f"  threads          : {health['n_threads']}")
        print(f"  cold load        : {health['cold_load_ms']:.0f} ms")
        print(f"  sentinel sha256  : {health['sentinel_sha256']}")
        if health["sentinel_matches_research"]:
            print("  OK: reproduces the vector the research committed.")
        else:
            ok = False
            print("  FAIL: sentinel drifted.")
            print(f"        expected {EXPECTED_SENTINEL_SHA256}")
            print(
                "        Stored vectors and fresh query vectors are no longer\n"
                "        in the same space. Every cosine would be unreliable."
            )
    except Exception as error:  # noqa: BLE001 - doctor reports, never raises
        ok = False
        print(f"  FAIL: {error}")

    print("\naspect (protected spread, frozen parser)")
    if not config.aspect_enabled:
        print("  disabled by deployment (RECOLLECT_ASPECT_ENABLED=0); no model needed.")
    else:
        try:
            model = await asyncio.to_thread(
                load_aspect_model, config.episodic.aspect_model
            )
            version = str(model.meta.get("version"))
            print(f"  parser model     : {config.episodic.aspect_model} {version}")
            print("  OK: the frozen ASPECT parser loads and is version-checked.")
        except Exception as error:  # noqa: BLE001 - doctor reports, never raises
            ok = False
            print(f"  FAIL: {error}")

    print("\ngenerator (HTTP, OpenAI-compatible)")
    generator = Generator(
        GeneratorSettings(
            base_url=config.generator_base_url,
            model=config.generator_model,
            api_key=config.generator_api_key,
        )
    )
    try:
        health = await generator.health()
        if health["reachable"]:
            print(f"  OK: {health['base_url']}")
            print(f"  models           : {health.get('available_models')}")
        else:
            ok = False
            print(f"  FAIL: {health['base_url']} unreachable")
            print(f"        {health.get('error')}")
            print(
                "        Start a server, e.g. llama-server --host 127.0.0.1 "
                "--port 8000"
            )
    finally:
        await generator.aclose()

    print("\n" + ("all checks passed" if ok else "some checks failed"))
    return 0 if ok else 1


async def _chat(session_id: str | None) -> int:
    """A minimal terminal client, so the harness is usable with no UI."""
    import httpx

    config = _load_config()
    base = f"http://{config.host}:{config.port}"

    async with httpx.AsyncClient(base_url=base, timeout=300.0) as client:
        try:
            await client.get("/api/health", timeout=5.0)
        except httpx.HTTPError:
            print(f"No server at {base}. Start one with: recollect serve")
            return 1

        if session_id is None:
            response = await client.post("/api/sessions", json={})
            session_id = response.json()["session_id"]
            print(f"new session {session_id}\n")

        while True:
            try:
                message = input("you: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not message or message in {"exit", "quit"}:
                return 0

            async with client.stream(
                "POST",
                "/api/chat",
                json={"session_id": session_id, "message": message},
            ) as response:
                event = None
                printed_prefix = False
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        payload = line[5:].strip()
                        if event == "token":
                            if not printed_prefix:
                                print("bot: ", end="", flush=True)
                                printed_prefix = True
                            print(json.loads(payload)["text"], end="", flush=True)
                        elif event == "retrieval":
                            trace = json.loads(payload)
                            report = trace["report"]
                            # Starved means proposals the budget never
                            # admitted - not merely proposals an earlier
                            # tier had already claimed.
                            starved = [
                                t["name"]
                                for t in trace["tiers"]
                                if t["skipped_ids"] and not t["delivered_ids"]
                            ]
                            note = (
                                f" · starved: {','.join(starved)}"
                                if starved
                                else ""
                            )
                            allowance = report.get(
                                "retrieval_budget_chars"
                            ) or report["budget_chars"]
                            print(
                                f"  [memory] {report['episodes_delivered']} episodes"
                                f" · {report['chars_delivered']}/"
                                f"{allowance} chars"
                                f" + {report['recency_count']} recent (additive)"
                                f" · S={report['semantic_count']}"
                                f" A={report['aspect_count']}"
                                f" (+{report['returned_semantic_count']} returned)"
                                + note
                            )
                        elif event == "error":
                            print(f"  [error] {json.loads(payload)['message']}")
                print("\n")


if __name__ == "__main__":
    sys.exit(main())
