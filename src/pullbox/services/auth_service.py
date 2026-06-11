"""Authentication service — password hashing, session tokens, and API key management."""

import secrets
from datetime import UTC, datetime

import bcrypt
import structlog
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.api_keys import (
    API_KEY_PREFIX,
    api_key_hash_candidates,
    hash_api_key,
    is_legacy_api_key_hash,
    is_well_formed_api_key,
)
from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import AuthenticationError
from pullbox.core.password_policy import MAX_PASSWORD_BYTES
from pullbox.models.user import APIKey, User

logger = structlog.get_logger(__name__)

SESSION_COOKIE_NAME = "pullbox_session"
BCRYPT_ROUNDS = 12


class AuthService:
    """Static methods for authentication, session management, and API keys."""

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt with 12 rounds."""
        if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            msg = f"Password must be at most {MAX_PASSWORD_BYTES} bytes when encoded as UTF-8."
            raise ValueError(msg)
        salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        """Verify a password against a bcrypt hash."""
        password_bytes = password.encode("utf-8")
        if len(password_bytes) > MAX_PASSWORD_BYTES:
            password_bytes = password_bytes[:MAX_PASSWORD_BYTES]
        return bcrypt.checkpw(password_bytes, password_hash.encode("utf-8"))

    @staticmethod
    def create_session_token(user_id: int, session_version: int = 0) -> str:
        """Create a signed session token containing user_id, CSRF token, and session version."""
        serializer = URLSafeTimedSerializer(get_application_secret())
        csrf = secrets.token_hex(16)
        return serializer.dumps({"user_id": user_id, "csrf": csrf, "sv": session_version})

    @staticmethod
    def validate_session_token(
        token: str,
        max_age_seconds: int = 24 * 3600,
    ) -> dict[str, object] | None:
        """Validate a session token and return its payload, or None if invalid."""
        serializer = URLSafeTimedSerializer(get_application_secret())
        try:
            data: dict[str, object] = serializer.loads(token, max_age=max_age_seconds)
        except (BadSignature, SignatureExpired):
            return None
        return data

    @staticmethod
    async def increment_session_version(session: AsyncSession, user_id: int) -> int:
        """Increment a user's session version, invalidating all existing sessions.

        Returns the new session_version.
        """
        from pullbox.core.exceptions import NotFoundError

        user = await session.get(User, user_id)
        if not user:
            raise NotFoundError("User", user_id)
        user.session_version += 1
        await session.flush()
        logger.info(
            "session_version_incremented", user_id=user_id, new_version=user.session_version
        )
        return user.session_version

    @staticmethod
    async def authenticate(session: AsyncSession, username: str, password: str) -> User:
        """Authenticate a user by username and password.

        Raises AuthenticationError if credentials are invalid.
        """
        result = await session.execute(
            select(User).where(User.username == username, User.is_active.is_(True))
        )
        user = result.scalar_one_or_none()

        if user is None or not AuthService.verify_password(password, user.password_hash):
            logger.warning("authentication_failed", username=username)
            raise AuthenticationError("Invalid username or password.")

        user.last_login_at = datetime.now(UTC)
        logger.info("user_authenticated", user_id=user.id, username=user.username)
        return user

    @staticmethod
    async def generate_api_key(
        session: AsyncSession,
        user_id: int,
        name: str,
        expires_at: datetime | None = None,
    ) -> tuple[str, APIKey]:
        """Generate a new API key. Returns (raw_key, api_key_model)."""
        raw_key = f"{API_KEY_PREFIX}{secrets.token_hex(32)}"
        key_hash = hash_api_key(raw_key)

        api_key = APIKey(
            user_id=user_id,
            key_hash=key_hash,
            name=name,
            expires_at=expires_at,
        )
        session.add(api_key)
        await session.flush()

        logger.info("api_key_generated", user_id=user_id, key_name=name)
        return raw_key, api_key

    @staticmethod
    async def validate_api_key(session: AsyncSession, raw_key: str) -> User | None:
        """Validate an API key and return the associated user, or None."""
        if not is_well_formed_api_key(raw_key):
            return None

        current_key_hash = hash_api_key(raw_key)

        result = await session.execute(
            select(APIKey).where(
                APIKey.key_hash.in_(api_key_hash_candidates(raw_key)),
                APIKey.is_active.is_(True),
            )
        )
        api_key = result.scalar_one_or_none()

        if api_key is None:
            return None

        # Check expiry
        if api_key.expires_at is not None:
            now = datetime.now(UTC)
            if api_key.expires_at < now:
                logger.info("api_key_expired", key_id=api_key.id)
                return None

        api_key.last_used_at = datetime.now(UTC)
        if is_legacy_api_key_hash(api_key.key_hash):
            api_key.key_hash = current_key_hash

        # Eagerly load user
        user_result = await session.execute(
            select(User).where(User.id == api_key.user_id, User.is_active.is_(True))
        )
        return user_result.scalar_one_or_none()

    @staticmethod
    async def create_user(session: AsyncSession, username: str, password: str) -> User:
        """Create a new user with a hashed password.

        Validates the password against the security policy before hashing.
        """
        from pullbox.core.exceptions import ValidationError
        from pullbox.core.password_policy import validate_password, validate_username

        pw_violations = validate_password(password)
        if pw_violations:
            raise ValidationError("; ".join(pw_violations), details={"violations": pw_violations})
        un_violations = validate_username(username)
        if un_violations:
            raise ValidationError("; ".join(un_violations), details={"violations": un_violations})

        password_hash = AuthService.hash_password(password)
        user = User(username=username, password_hash=password_hash)
        session.add(user)
        await session.flush()
        logger.info("user_created", user_id=user.id, username=user.username)
        return user

    @staticmethod
    async def has_users(session: AsyncSession) -> bool:
        """Return True if at least one user exists."""
        result = await session.execute(select(func.count(User.id)))
        count = result.scalar_one()
        return count > 0

    @staticmethod
    def get_csrf_token_from_session(token: str) -> str | None:
        """Extract the CSRF token from a session token."""
        serializer = URLSafeTimedSerializer(get_application_secret())
        try:
            data: dict[str, object] = serializer.loads(token)
        except BadSignature:
            return None
        csrf = data.get("csrf")
        if isinstance(csrf, str):
            return csrf
        return None

    @staticmethod
    def validate_csrf_token(csrf_token: str, session_token: str) -> bool:
        """Validate a CSRF token against the one embedded in the session."""
        expected = AuthService.get_csrf_token_from_session(session_token)
        if expected is None:
            return False
        return secrets.compare_digest(csrf_token, expected)
