from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnauthorizedError
from app.core.security import DUMMY_PASSWORD_HASH, verify_password
from app.models import User
from app.repositories import users as users_repo


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    user = await users_repo.get_by_email(session, email)
    if user is None:
        # Spend the same time hashing so response timing doesn't reveal valid emails.
        verify_password(password, DUMMY_PASSWORD_HASH)
        raise UnauthorizedError("Invalid email or password")
    if not verify_password(password, user.password_hash) or not user.is_active:
        raise UnauthorizedError("Invalid email or password")
    return user
