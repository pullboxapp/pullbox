"""Remount tolerance requires saved content, never a filename or stat-only guess."""

import os
from pathlib import Path

import pytest

from pullbox.core.exceptions import ConfigurationError
from pullbox.services import direct_metadata_file_proof as evidence


@pytest.fixture
def retained(tmp_path):
    path = tmp_path / "retained.cbz"
    path.write_bytes(b"original comic bytes")
    proof = evidence.capture_file_proof(path)
    return path, evidence.proof_identity(proof), proof["sha256"]


def test_content_proof_allows_only_the_device_number_to_differ(retained):
    path, saved, digest = retained
    saved["device"] += 1
    verified = evidence.verify_file_proof(path, saved, digest)
    assert verified["device"] == path.stat().st_dev
    assert verified["sha256"] == digest


@pytest.mark.parametrize("key", ["schema_version", "resolved_path", "size", "mtime_ns", "inode"])
def test_other_identity_changes_are_not_remounts(retained, key):
    path, saved, digest = retained
    saved[key] = "different" if isinstance(saved[key], str) else saved[key] + 1
    with pytest.raises(ValueError):
        evidence.verify_file_proof(path, saved, digest)


@pytest.mark.parametrize("digest", [None, "", "0" * 64, True, "a" * 65])
def test_remount_requires_valid_original_digest(retained, digest):
    path, saved, _ = retained
    saved["device"] += 1
    with pytest.raises(ValueError):
        evidence.verify_file_proof(path, saved, digest)


def test_legacy_record_still_requires_exact_stat_identity(retained):
    path, saved, _ = retained
    assert evidence.verify_file_proof(path, saved, None) is None
    saved["device"] += 1
    with pytest.raises(ValueError):
        evidence.verify_file_proof(path, saved, None)


def test_verified_writer_control_checks_do_not_rehash_unchanged_archive(retained, monkeypatch):
    path, saved, digest = retained
    verified = evidence.verify_file_proof(path, saved, digest)
    with monkeypatch.context() as patch:
        patch.setattr(
            evidence, "capture_file_proof", lambda _: pytest.fail("Repeated archive hash")
        )
        assert evidence.verify_file_proof(path, saved, digest, verified) == verified
    stat = path.stat()
    path.write_bytes(b"changed! comic bytes")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(ValueError):
        evidence.verify_file_proof(path, saved, digest, verified)


def test_hardlink_count_change_requires_rehash_not_new_content_approval(retained, tmp_path):
    path, saved, digest = retained
    verified = evidence.verify_file_proof(path, saved, digest)
    (tmp_path / "copy.cbz").hardlink_to(path)
    assert evidence.verify_file_proof(path, saved, digest, verified)["sha256"] == digest


def test_existing_verified_proof_survives_another_device_renumber(retained):
    path, saved, digest = retained
    verified = evidence.verify_file_proof(path, saved, digest)
    verified["device"] += 1
    assert evidence.verify_file_proof(path, saved, digest, verified)["device"] == path.stat().st_dev


def test_changed_file_during_hash_fails_closed(retained, monkeypatch):
    path, _, _ = retained

    def changed(_):
        raise ConfigurationError("Managed placement changed while its ownership proof was built.")

    monkeypatch.setattr(evidence, "build_managed_placement_signature", changed)
    with pytest.raises(ValueError, match="changed"):
        evidence.capture_file_proof(path)


def test_same_stat_byte_change_during_hash_is_rejected(retained, monkeypatch):
    path, _, _ = retained
    original = evidence.build_managed_placement_signature

    def changed(target: Path):
        result = original(target)
        stat = target.stat()
        target.write_bytes(b"changed! comic bytes")
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return result

    monkeypatch.setattr(evidence, "build_managed_placement_signature", changed)
    with pytest.raises(ValueError, match="changed"):
        evidence.capture_file_proof(path)


def test_parent_symlink_redirect_is_rejected(retained, tmp_path):
    path, saved, digest = retained
    other = tmp_path / "other"
    other.mkdir()
    moved = other / path.name
    path.rename(moved)
    link = tmp_path / "redirect"
    link.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError):
        evidence.verify_file_proof(link / path.name, saved, digest)
