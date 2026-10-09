"""API fixtures reuse immutable setup without sharing database or auth state."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_API_FIXTURES = (
    ("test_import_api", "_db_factory", "_api_key_header", "importuser"),
    ("test_issue_import_file", "_db_factory", "_api_key_header", "importfileuser"),
    ("test_import_file_api", "_db_factory", "_api_key_header", "fileapiuser"),
    ("test_clear_download_history", "db_factory", "api_key", "clearuser"),
    ("test_blocklist_api", "_db_factory", "_api_key_header", "bluser"),
)


@pytest.mark.parametrize("workers", [0, 2])
def test_api_fixtures_reuse_setup_and_isolate_committed_state(
    pytester: pytest.Pytester, workers: int
) -> None:
    root = Path(__file__).resolve().parents[2]
    pytester.makeini("[pytest]\nasyncio_mode = auto\nmarkers = slow: integration tests\n")
    pytester.makeconftest(
        f"""
import sys
sys.path.insert(0, {str(root)!r})
import pytest
from pullbox.models import Base
from pullbox.services.auth_service import AuthService

pytest_plugins = ["tests.conftest"]

@pytest.fixture(scope="session", autouse=True)
def setup_counts():
    counts = {{"schema": 0, "hash": 0}}
    create_all = Base.metadata.create_all
    hash_password = AuthService.hash_password
    def counted_schema(*args, **kwargs):
        counts["schema"] += 1
        return create_all(*args, **kwargs)
    def counted_hash(password):
        counts["hash"] += 1
        return hash_password(password)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Base.metadata, "create_all", counted_schema)
        patch.setattr(AuthService, "hash_password", staticmethod(counted_hash))
        yield counts
"""
    )
    for module, database, api_key, username in _API_FIXTURES:
        directory = pytester.path / module
        directory.mkdir()
        (directory / "conftest.py").write_text(
            f"from tests.api.{module} import {database}, {api_key}\n"
        )
        (directory / f"test_{module}_isolation.py").write_text(
            f"""
import pytest
from sqlalchemy import select, text
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import AuthService, BCRYPT_ROUNDS

@pytest.mark.parametrize("case", range(2))
async def test_fresh_seed({database}, {api_key}, setup_counts, case):
    assert setup_counts == {{"schema": 1, "hash": 1}}, (
        "API fixtures must reuse schema construction and seed hashing per worker",
        setup_counts,
    )
    async with {database}() as session:
        user = (await session.execute(select(User))).scalar_one()
        key = (await session.execute(select(APIKey))).scalar_one()
        assert user.id == key.id == 1
        assert user.username == {username!r}
        assert user.session_version == 0
        assert key.is_active
        assert key.user_id == user.id
        assert int(user.password_hash.split("$")[2]) == BCRYPT_ROUNDS
        assert AuthService.verify_password("Test@1234", user.password_hash)
        assert not AuthService.verify_password("wrong-password", user.password_hash)
        assert await AuthService.validate_api_key(session, {api_key}) is user
        assert await AuthService.validate_api_key(session, "pb_k1_" + "f" * 64) is None
        assert not (await session.execute(text(
            "SELECT name FROM sqlite_master WHERE name = 'previous_test_only'"
        ))).all()
        # Commit both data and DDL changes; the next case must still start fresh.
        user.username = "changed"
        user.password_hash = "changed"
        user.session_version = 99
        key.is_active = False
        await session.execute(text("CREATE TABLE previous_test_only (value TEXT)"))
        await session.commit()
        assert await AuthService.validate_api_key(session, {api_key}) is None
"""
        )
    result = pytester.runpytest_subprocess("-q", "-n", str(workers), timeout=90)
    result.assert_outcomes(passed=10)
