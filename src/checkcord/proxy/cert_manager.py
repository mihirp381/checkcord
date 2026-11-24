import os
from pathlib import Path

import structlog
import trustme

logger = structlog.get_logger(__name__)


def generate_certs(cert_dir: Path, force: bool = False) -> bool:
    """Generate self-signed certificates for localhost and 127.0.0.1 with trustme.

    Writes ca.pem, cert.pem and key.pem to cert_dir. Returns True if
    certificates were generated, False if skipped. Both proxy modes use the
    same certificate; they differ only in how traffic is routed to the proxy.
    """
    cert_dir.mkdir(parents=True, exist_ok=True)

    ca_path = cert_dir / "ca.pem"
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"

    if not force and ca_path.exists() and cert_path.exists() and key_path.exists():
        logger.debug("certificates_already_exist", cert_dir=str(cert_dir))
        return False

    logger.info("generating_local_certificates", cert_dir=str(cert_dir))

    # Create CA
    ca = trustme.CA()

    # Issue leaf cert for localhost & 127.0.0.1
    server_cert = ca.issue_cert("localhost", "127.0.0.1")

    # Write CA cert
    ca.cert_pem.write_to_path(str(ca_path))

    # Write private key
    server_cert.private_key_pem.write_to_path(str(key_path))
    # Restrict permissions of private key to 0600
    try:
        os.chmod(key_path, 0o600)
    except OSError as e:
        logger.warning("failed_to_set_private_key_permissions", error=str(e))

    # Write server cert chain
    # Note: server_cert.cert_chain_pems is a list of trustme.Blob objects.
    # We join them to create a full chain cert file.
    full_chain = b"\n".join([blob.bytes() for blob in server_cert.cert_chain_pems])
    cert_path.write_bytes(full_chain)

    logger.info("certificates_generated_successfully", ca=str(ca_path), cert=str(cert_path))
    return True
