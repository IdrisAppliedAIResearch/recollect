"""Read portable pairing credentials without importing a model or crypto runtime."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field
from pathlib import Path

PAIRING_FORMAT = "recollect-pairing-v1"
TLS_SERVER_NAME = "recollect-desktop"
MAX_PAIRING_BYTES = 65_536


def validate_token(token: str | None) -> None:
    if (not isinstance(token, str) or not 32 <= len(token) <= 4096
            or not token.isascii()
            or any(not 33 <= ord(char) <= 126 for char in token)):
        raise ValueError(
            "RECOLLECT_DEPLOYMENT_TOKEN must contain 32 to 4096 printable "
            "ASCII characters without whitespace"
        )


@dataclass(frozen=True)
class PairingCredential:
    token: str = field(repr=False)
    certificate_pem: str | None = None
    server_name: str = TLS_SERVER_NAME

    def __post_init__(self) -> None:
        validate_token(self.token)
        if self.server_name != TLS_SERVER_NAME:
            raise ValueError("Unsupported pairing TLS server name")
        if self.certificate_pem is not None:
            if (not isinstance(self.certificate_pem, str)
                    or len(self.certificate_pem) > 32_768
                    or self.certificate_pem.count("-----BEGIN CERTIFICATE-----") != 1
                    or "PRIVATE KEY" in self.certificate_pem):
                raise ValueError("Pairing requires exactly one public TLS certificate")
            try:
                self.ssl_context()
            except (ssl.SSLError, ValueError) as error:
                raise ValueError(
                    "Pairing contains an invalid TLS certificate"
                ) from error

    def ssl_context(self) -> ssl.SSLContext:
        if self.certificate_pem is None:
            return ssl.create_default_context()
        # Trust only the public identity copied from the desktop, not other CAs.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=self.certificate_pem)
        return context

    def request_extensions(self) -> dict:
        return {"sni_hostname": self.server_name} if self.certificate_pem else {}

    def websocket_options(self) -> dict:
        options = {"ssl": self.ssl_context()}
        if self.certificate_pem:
            options["server_hostname"] = self.server_name
        return options

    def to_bytes(self) -> bytes:
        if self.certificate_pem is None:
            return (self.token + "\n").encode("ascii")
        return (json.dumps({
            "format": PAIRING_FORMAT, "token": self.token,
            "tls": {"server_name": self.server_name,
                    "certificate_pem": self.certificate_pem},
        }, indent=2) + "\n").encode("utf-8")


def parse_pairing(contents: bytes) -> PairingCredential:
    if len(contents) > MAX_PAIRING_BYTES:
        raise ValueError("Pairing file is too large")
    try:
        text = contents.decode("utf-8-sig").strip()
        if not text.startswith("{"):
            return PairingCredential(text)
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            # Older custom tokens may begin with a brace; the token contract
            # still rejects whitespace and malformed multiline bundle contents.
            return PairingCredential(text)
        if (not isinstance(document, dict)
                or set(document) != {"format", "token", "tls"}
                or document["format"] != PAIRING_FORMAT
                or not isinstance(document["tls"], dict)
                or set(document["tls"]) != {"server_name", "certificate_pem"}
                or not isinstance(document["tls"]["certificate_pem"], str)):
            raise ValueError("Unsupported pairing file format")
        return PairingCredential(document["token"], **document["tls"])
    except UnicodeError as error:
        raise ValueError("Pairing file is not valid UTF-8 credentials") from error


def load_pairing(path: str | Path) -> PairingCredential:
    from .private_files import read_private

    return parse_pairing(read_private(Path(path)))
