import io

import pytest

from recollect.selfmod import native_supervisor as supervisor


def manifest():
    payload = {
        "version": 1, "run_id": "a" * 32, "capture_spec_sha256": "b" * 64,
        "native_binary_sha256": "c" * 64, "image_id": "sha256:" + "d" * 64,
        "image_environment": [],
        "isolation": {},
        "helpers": {name: "e" * 64 for name in supervisor.HELPERS},
    }
    return {"payload": payload, "sha256": supervisor.digest(
        supervisor.canonical(payload))}


def test_manifest_exact_roundtrip():
    value = manifest()
    assert supervisor.load_manifest(supervisor.canonical(value)) == value


@pytest.mark.parametrize("fault", ["hash", "version", "run", "extra", "helper",
                                  "binary", "missing"])
def test_invalid_runtime_manifest_rejected(fault):
    value = manifest()
    payload = value["payload"]
    if fault == "hash":
        value["sha256"] = "0" * 64
    else:
        if fault == "version":
            payload["version"] = True
        elif fault == "run":
            payload["run_id"] = "../other"
        elif fault == "extra":
            payload["extra"] = True
        elif fault == "helper":
            payload["helpers"]["worker.py"] = "0" * 64
        elif fault == "binary":
            payload["native_binary_sha256"] = "no"
        else:
            del payload["capture_spec_sha256"]
        value["sha256"] = supervisor.digest(supervisor.canonical(payload))
    with pytest.raises(ValueError):
        supervisor.load_manifest(supervisor.canonical(value))


@pytest.mark.parametrize("raw", [b"{}", b"[]\n", b'{"a":1,"a":2}\n',
                                b'{ "a":1}\n', b"{broken\n"])
def test_noncanonical_control_frame_rejected(raw):
    with pytest.raises(ValueError):
        supervisor.read_frame(io.BytesIO(raw))


def test_control_eof_and_exact_frame():
    assert supervisor.read_frame(io.BytesIO()) is None
    value = {"kind": "fence", "runtime_sha256": "x"}
    assert supervisor.read_frame(io.BytesIO(supervisor.canonical(value))) == value


def test_supervisor_refuses_host_execution():
    with pytest.raises(ValueError, match="namespace PID 1"):
        supervisor.supervisor_only()
