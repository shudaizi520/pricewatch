"""Password hashing and bounded login throttling."""

import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password using Argon2id."""

    return _password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    """Return whether a password matches without leaking mismatch details."""

    try:
        return _password_hasher.verify(encoded, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


class LoginThrottle:
    """In-memory rolling-window throttle for a single-process service."""

    def __init__(self, maximum_failures: int = 5, window_seconds: int = 300) -> None:
        self.maximum_failures = maximum_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _active_failures(self, key: str, now: float) -> deque[float]:
        failures = self._failures[key]
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        return failures

    def blocked(self, key: str) -> bool:
        return len(self._active_failures(key, time.monotonic())) >= self.maximum_failures

    def record_failure(self, key: str) -> None:
        self._active_failures(key, time.monotonic()).append(time.monotonic())

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)
