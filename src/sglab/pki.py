"""Small certificate authority for node (device) and core-service MQTT client certificates.

    <pki_dir>/ca.key, ca.crt            "NBE Device CA" (EC P-256, 10 years)
    <pki_dir>/nodes/<ID>.key/.crt       one client cert per ESP32 node, CN = node ID
    <pki_dir>/core.key/.crt             the sglab service's own MQTT client cert, CN = nbe-core

mosquitto trusts ca.crt for client certificates and uses the certificate CN as
the MQTT username (use_identity_as_username), so the ACL can pin each node to
its own topics.  The broker's *server* certificate is separate (e.g. your
existing Let's Encrypt cert for the domain).

Keep <pki_dir> out of the web root and back it up: losing ca.key means
re-flashing every node with new credentials.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _write(path: Path, data: bytes, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    else:
        path.write_bytes(data)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


class PKI:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    @property
    def ca_crt_path(self) -> Path:
        return self.dir / "ca.crt"

    def exists(self) -> bool:
        return (self.dir / "ca.key").exists() and self.ca_crt_path.exists()

    def init_ca(self, name: str = "NBE Device CA", years: int = 10) -> Path:
        if self.exists():
            raise FileExistsError(f"CA already exists in {self.dir}")
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name),
                             x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NBE smart glasses")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365 * years))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                             content_commitment=False, key_encipherment=False, data_encipherment=False,
                                             key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        _write(self.dir / "ca.key", _key_pem(key), private=True)
        _write(self.ca_crt_path, cert.public_bytes(serialization.Encoding.PEM))
        return self.ca_crt_path

    def _ca(self):
        key = serialization.load_pem_private_key((self.dir / "ca.key").read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(self.ca_crt_path.read_bytes())
        return key, cert

    def issue(self, common_name: str, days: int = 3 * 365) -> tuple[bytes, bytes, x509.Certificate]:
        """New EC P-256 key + client certificate (CN = common_name). Returns (key_pem, cert_pem, cert)."""
        if not self.exists():
            raise FileNotFoundError(f"no CA in {self.dir} - run `sglab pki init` first")
        ca_key, ca_cert = self._ca()
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
                .issuer_name(ca_cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=days))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                             content_commitment=False, key_encipherment=False, data_encipherment=False,
                                             key_agreement=True, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        return _key_pem(key), cert.public_bytes(serialization.Encoding.PEM), cert

    def issue_to(self, common_name: str, key_path: Path, crt_path: Path, days: int = 3 * 365) -> x509.Certificate:
        key_pem, cert_pem, cert = self.issue(common_name, days)
        _write(Path(key_path), key_pem, private=True)
        _write(Path(crt_path), cert_pem)
        return cert
