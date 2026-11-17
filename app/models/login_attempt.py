"""Login attempts on /login, kept so the owner can review who tried to get in.

Wrong passwords are stored in plain text on purpose (to guess who the intruder
is), except when the input is close to the real password: that is most likely
the owner's own typo, and storing it would leak the real password if the DB
ever leaked. Successful logins never store the password.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    user_agent: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    # failed | locked | success
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    # None for success, or when withheld because it resembles the real password.
    attempted_password: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # e.g. "similar_to_real" when the password was withheld.
    password_note: Mapped[str | None] = mapped_column(String(40), nullable=True)
    device_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
