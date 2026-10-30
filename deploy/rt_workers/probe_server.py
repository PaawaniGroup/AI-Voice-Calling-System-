"""Gate G1 probe server: the real worker protocol (common.py: hello + signed single-use token, ping, warmup, cancel) with NO model, over TLS with a certificate generated at start.
Run on a throw-away Pod by tools/realtime/launch_probe_pod.py; also usable locally on 127.0.0.1.

  RT_SECRET   the signing secret (an environment variable; never logged)        PORT (default 8001)    RT_BIND (default 127.0.0.1)    STATUS_DIR (default /workspace/out/<RESULT_TOKEN>)
It writes STATUS_DIR/status.json {"state": "serving", "cert_sha256": "<hex>"}: the SHA-256 of the certificate is public (the client PINS it). Nothing else is written.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Limits, serve_connection, serve_forever   # noqa: E402


class NoEngine:
    """The probe never runs a model; a synth request is answered with an engine error."""

    async def synth_stream(self, *a, **k):
        raise RuntimeError("no model on the probe server")
        yield b""


def make_certificate(directory: Path) -> tuple[Path, Path, str]:
    """A self-signed certificate valid for one day (EC P-256). Returns (cert path, key path, sha256 of the DER certificate)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    directory.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mme-rt-probe")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    cp, kp = directory / "cert.pem", directory / "key.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(kp, 0o600)
    return cp, kp, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


async def main() -> None:
    secret = os.environ["RT_SECRET"].encode()
    status_dir = Path(os.environ.get("STATUS_DIR") or f"/workspace/out/{os.environ.get('RESULT_TOKEN', 'probe')}")
    cp, kp, fp = make_certificate(Path(os.environ.get("CERT_DIR", "/workspace/cert")))
    os.environ["RT_TLS_CERT"], os.environ["RT_TLS_KEY"] = str(cp), str(kp)
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "status.json").write_text(json.dumps({"state": "serving", "cert_sha256": fp}), encoding="utf-8")
    limits = Limits(max_global=2)
    sem = asyncio.Semaphore(limits.max_global)

    async def handler(ws):
        await serve_connection(ws, service="tts", engine=NoEngine(), secret=secret, limits=limits, global_sem=sem)
    await serve_forever(handler)


if __name__ == "__main__":
    asyncio.run(main())
