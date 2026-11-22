import json
import os

import pytest

from pullbox.services import migration_approval as approval
from pullbox.services.migration_first_start import BindingError, read_token


def test_only_accept_private_regular_secret(tmp_path):
    token = tmp_path / "token"
    value = "ab" * 32
    token.write_text(value + "\n")
    token.chmod(0o600)
    assert read_token(token) == bytes.fromhex(value)
    link = tmp_path / "link"
    link.symlink_to(token)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    for path in (link, fifo, tmp_path, tmp_path / "missing"):
        with pytest.raises(BindingError):
            read_token(path)
    token.chmod(0o644)
    with pytest.raises(BindingError):
        read_token(token)
    token.chmod(0o600)
    for malformed in (value.upper(), value + "\n\n", value[:-1], "not-a-token"):
        token.write_text(malformed)
        with pytest.raises(BindingError) as error:
            read_token(token)
        assert malformed not in str(error.value)


def test_approval_is_expiring_private_and_cannot_be_retargeted_or_overwritten(
    tmp_path, monkeypatch
):
    now = 10000
    monkeypatch.setattr(approval.time, "time", lambda: now)
    label = "pullbox:offline-stage:bound-v2:fixture"
    media, key, secret = "b" * 64, bytes(range(32)), "test-secret"
    file = tmp_path / "approval.json"
    approval.write_approval(file, label, media, key, secret, all_files=True)
    assert approval._read(file, label, key, secret) == (media, True, now + approval.WINDOW)
    assert file.stat().st_mode & 0o777 == 0o600
    original = file.read_bytes()
    with pytest.raises(FileExistsError):
        approval.write_approval(file, label, media, key, secret, all_files=False)
    assert file.read_bytes() == original
    for wrong_label, wrong_key in ((label + "x", key), (label, b"a" * 32)):
        with pytest.raises(BindingError):
            approval._read(file, wrong_label, wrong_key, secret)
    edited = json.loads(original)
    edited["approval"]["all_files"] = False
    file.write_text(json.dumps(edited))
    with pytest.raises(BindingError):
        approval._read(file, label, key, secret)
    file.write_bytes(original)
    now += approval.WINDOW
    with pytest.raises(BindingError):
        approval._read(file, label, key, secret)
