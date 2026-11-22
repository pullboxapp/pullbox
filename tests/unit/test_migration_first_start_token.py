import os

import pytest

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
