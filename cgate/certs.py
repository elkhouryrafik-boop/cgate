"""
cgate.certs — a certificate for localhost, and nothing else.

Office refuses to load a task pane over plain HTTP, including from localhost.
That leaves two options: host the add-in on a server the firm's documents would
have to reach, or terminate TLS on the machine the document is already on. The
second is the only one compatible with the answer this project gives to
the question of whether documents may leave the machine, so it is the one implemented here.

What this generates
-------------------
A self-signed certificate whose subject alternative names are exactly
`localhost`, `127.0.0.1` and `::1`. It cannot authenticate any other host, so
trusting it does not weaken the machine's trust in anything on the network. It
is written to `%LOCALAPPDATA%\cgate\certs` with the private key readable only
by the current user, and it expires in 825 days because that is the maximum a
browser will accept.

Trust is a separate, explicit step (`--trust`), because installing a root is a
change to the machine and should be a thing a person did on purpose. Without it
Word shows a certificate warning and Edge WebView will refuse to load the pane.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import subprocess
import sys

CERT_DIR_ENV = "CGATE_CERT_DIR"
_VALID_DAYS = 825


def cert_dir() -> str:
    override = os.environ.get(CERT_DIR_ENV)
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "cgate", "certs")


def cert_paths() -> tuple[str, str]:
    d = cert_dir()
    return os.path.join(d, "localhost.pem"), os.path.join(d, "localhost-key.pem")


def _days_remaining(cert_path: str) -> int | None:
    from cryptography import x509

    try:
        with open(cert_path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
    except (OSError, ValueError):
        return None
    expiry = cert.not_valid_after_utc
    return (expiry - datetime.datetime.now(datetime.timezone.utc)).days


def ensure(force: bool = False) -> tuple[str, str]:
    """Return (cert_path, key_path), generating the pair if needed."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_path, key_path = cert_paths()
    if not force and os.path.exists(cert_path) and os.path.exists(key_path):
        left = _days_remaining(cert_path)
        if left is not None and left > 14:
            return cert_path, key_path

    os.makedirs(cert_dir(), exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "cgate compliance support system"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "cgate local host certificate"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_VALID_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.IPAddress(ipaddress.ip_address("::1")),
            ]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True, key_cert_sign=True,
                content_commitment=False, data_encipherment=False, key_agreement=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))

    # The key is written through a descriptor opened 0600 so it is never briefly
    # world-readable between creation and chmod.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    return cert_path, key_path


def trust(cert_path: str) -> int:
    """Install the certificate into the current user's trusted roots.

    Per-user, never machine-wide: this needs no elevation and its blast radius
    is one account. On anything other than Windows the command is printed for
    the operator to run, rather than guessing at the platform's trust store.
    """
    if sys.platform != "win32":
        print("Trust this certificate with your platform's trust store:")
        print(f"  {cert_path}")
        return 1
    proc = subprocess.run(
        ["certutil", "-user", "-addstore", "Root", cert_path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        return proc.returncode
    print(f"Trusted for this user: {cert_path}")
    print("Word and Edge WebView will now load the task pane without a warning.")
    return 0


def untrust() -> int:
    """Remove it again. Named in the README so the change is reversible."""
    if sys.platform != "win32":
        print("Remove the certificate from your platform's trust store manually.")
        return 1
    proc = subprocess.run(
        ["certutil", "-user", "-delstore", "Root", "localhost"],
        capture_output=True, text=True,
    )
    sys.stdout.write(proc.stdout)
    return proc.returncode
