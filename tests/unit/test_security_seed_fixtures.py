"""Seed hashing may be shared; users, databases and real auth may not be."""

from pathlib import Path

import pytest

from pullbox.services.auth_service import AuthService

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("workers", [0, 2])
def test_seed_hash_is_once_per_worker_with_fresh_users(
    pytester: pytest.Pytester, workers: int
) -> None:
    root = Path(__file__).resolve().parents[2]
    pytester.makeini("[pytest]\nasyncio_mode = auto\nmarkers = slow: integration tests\n")
    pytester.makeconftest(
        f"""
import sys
sys.path.insert(0, {str(root)!r})
import pytest
from pullbox.services.auth_service import AuthService
from tests.conftest_security import sec_db, sec_user

pytest_plugins = ["tests.conftest"]

@pytest.fixture(scope="session", autouse=True)
def count_seed_hashes():
    original = AuthService.hash_password
    calls = []
    def counted(password):
        calls.append(password)
        return original(password)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AuthService, "hash_password", staticmethod(counted))
        yield
    assert calls == ["Test@1234"], "Seed password must be hashed once per worker"
"""
    )
    pytester.makepyfile(
        """
import bcrypt
import pytest
from pullbox.models.user import User
from pullbox.services.auth_service import AuthService

@pytest.mark.parametrize("case", range(4))
async def test_fresh_seed_and_real_verification(sec_db, sec_user, case):
    assert sec_user.username == "testuser"
    assert sec_user.session_version == 0
    assert int(sec_user.password_hash.split("$")[2]) >= 12
    assert bcrypt.checkpw(b"Test@1234", sec_user.password_hash.encode())
    assert not AuthService.verify_password("wrong-password", sec_user.password_hash)
    async with sec_db() as session:
        user = await session.get(User, sec_user.id)
        assert user.username == "testuser"
        user.username = "changed"
        user.password_hash = "changed"
        user.session_version = 99
        await session.commit()
    sec_user.username = "changed-object"
"""
    )
    result = pytester.runpytest_subprocess("-q", "-n", str(workers), timeout=60)
    result.assert_outcomes(passed=4)


def test_runtime_hashing_still_generates_fresh_salts() -> None:
    first = AuthService.hash_password("FixtureBoundary@123")
    second = AuthService.hash_password("FixtureBoundary@123")

    assert first != second
    assert AuthService.verify_password("FixtureBoundary@123", first)
    assert AuthService.verify_password("FixtureBoundary@123", second)
