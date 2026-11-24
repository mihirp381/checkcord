"""Tests for proxy.cert_manager -- TLS cert generation for the local MITM interception proxy."""

import stat
from pathlib import Path

from checkcord.proxy.cert_manager import generate_certs


def test_generate_certs_creates_all_three_files(tmp_path: Path) -> None:
    cert_dir = tmp_path / "certs"

    result = generate_certs(cert_dir)

    assert result is True
    assert (cert_dir / "ca.pem").exists()
    assert (cert_dir / "cert.pem").exists()
    assert (cert_dir / "key.pem").exists()


def test_generate_certs_skips_regeneration_when_certs_exist(tmp_path: Path) -> None:
    cert_dir = tmp_path / "certs"
    assert generate_certs(cert_dir) is True

    ca_content_before = (cert_dir / "ca.pem").read_bytes()
    key_mtime_before = (cert_dir / "key.pem").stat().st_mtime_ns

    result = generate_certs(cert_dir)

    assert result is False
    assert (cert_dir / "ca.pem").read_bytes() == ca_content_before
    assert (cert_dir / "key.pem").stat().st_mtime_ns == key_mtime_before


def test_generate_certs_force_regenerates_even_when_certs_exist(tmp_path: Path) -> None:
    cert_dir = tmp_path / "certs"
    assert generate_certs(cert_dir) is True
    ca_content_before = (cert_dir / "ca.pem").read_bytes()

    result = generate_certs(cert_dir, force=True)

    assert result is True
    # A freshly-generated CA has a different key/cert than the original.
    assert (cert_dir / "ca.pem").read_bytes() != ca_content_before


def test_private_key_file_permissions_are_0600(tmp_path: Path) -> None:
    cert_dir = tmp_path / "certs"
    generate_certs(cert_dir)

    key_path = cert_dir / "key.pem"
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600


def test_generate_certs_creates_cert_dir_if_missing(tmp_path: Path) -> None:
    cert_dir = tmp_path / "nested" / "certs" / "dir"
    assert not cert_dir.exists()

    result = generate_certs(cert_dir)

    assert result is True
    assert cert_dir.is_dir()


def test_generate_certs_regenerates_when_only_some_files_present(tmp_path: Path) -> None:
    """A partial/corrupted cert_dir (e.g. only ca.pem present) must be treated as absent."""
    cert_dir = tmp_path / "certs"
    cert_dir.mkdir(parents=True)
    (cert_dir / "ca.pem").write_bytes(b"not a real cert")

    result = generate_certs(cert_dir)

    assert result is True
    assert (cert_dir / "cert.pem").exists()
    assert (cert_dir / "key.pem").exists()
