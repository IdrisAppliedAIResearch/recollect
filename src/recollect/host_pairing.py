"""Provision one private host identity and a portable public pairing bundle."""

from __future__ import annotations

import argparse
import json
import secrets
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .pairing import TLS_SERVER_NAME, PairingCredential, parse_pairing
from .private_files import read_private, replace_private, write_private


def _prepare_parent(path: Path) -> None:
    for parent in path.parents:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise ValueError("Pairing files cannot use linked parent directories")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def _read_existing(path: Path) -> bytes | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return read_private(path)


def ensure_host_pairing(bundle_path: str | Path) -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    bundle = Path(bundle_path).absolute()
    certificate_path = bundle.with_name(bundle.name + ".tls.crt")
    private_key_path = bundle.with_name(bundle.name + ".tls.key")
    _prepare_parent(bundle)
    # Validate every existing path before generating any new secret material.
    bundle_bytes = _read_existing(bundle)
    certificate_bytes = _read_existing(certificate_path)
    private_key_bytes = _read_existing(private_key_path)
    credential = parse_pairing(bundle_bytes) if bundle_bytes is not None else None
    certificate_exists = certificate_bytes is not None
    private_key_exists = private_key_bytes is not None
    if certificate_exists != private_key_exists:
        raise ValueError(
            "Incomplete host TLS identity; restore its certificate/key pair"
        )
    if credential and credential.certificate_pem and not certificate_exists:
        raise ValueError("The pairing bundle's host TLS identity is missing")

    if certificate_exists:
        certificate_pem = certificate_bytes.decode("ascii")
        key = serialization.load_pem_private_key(private_key_bytes, None)
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
        public_format = (serialization.Encoding.DER,
                         serialization.PublicFormat.SubjectPublicKeyInfo)
        if certificate.public_key().public_bytes(*public_format) != (
            key.public_key().public_bytes(*public_format)
        ):
            raise ValueError("Host TLS certificate and private key do not match")
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName,
        ).value.get_values_for_type(x509.DNSName)
        if TLS_SERVER_NAME not in names:
            raise ValueError("Host certificate does not identify Recollect")
        if not certificate.not_valid_before_utc <= datetime.now(UTC) < (
            certificate.not_valid_after_utc
        ):
            raise ValueError(
                "Host certificate expired; renew and pair the devices again"
            )
        if (credential and credential.certificate_pem
                and x509.load_pem_x509_certificate(
                credential.certificate_pem.encode("ascii"),
                ).fingerprint(hashes.SHA256()) != certificate.fingerprint(
                    hashes.SHA256(),
                )):
            raise ValueError("Pairing bundle and host TLS identity differ")
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, TLS_SERVER_NAME)])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
            .not_valid_after(datetime.now(UTC) + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName(TLS_SERVER_NAME),
            ]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                key_cert_sign=True, crl_sign=True, encipher_only=False,
                decipher_only=False,
            ), critical=True)
            .add_extension(x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
            ]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                key.public_key(),
            ), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                key.public_key(),
            ), critical=False)
            .sign(key, hashes.SHA256())
        )
        certificate_pem = certificate.public_bytes(
            serialization.Encoding.PEM,
        ).decode("ascii")
        write_private(private_key_path, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        write_private(certificate_path, certificate_pem.encode("ascii"))

    paired = PairingCredential(
        credential.token if credential else secrets.token_urlsafe(32), certificate_pem,
    )
    if credential is None:
        write_private(bundle, paired.to_bytes())
    elif credential.certificate_pem is None:
        # Preserve the shared secret when upgrading an existing token file.
        replace_private(bundle, paired.to_bytes())
    return {
        "bundle_path": str(bundle), "certificate_path": str(certificate_path),
        "private_key_path": str(private_key_path), "server_name": TLS_SERVER_NAME,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(ensure_host_pairing(args.bundle)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
