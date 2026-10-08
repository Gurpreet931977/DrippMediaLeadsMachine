"""
lib/system/file_lock.py
=======================
Cross-process file locking mechanism using fcntl.flock on macOS / Linux.
Prevents race conditions, corrupted state, concurrent overwrites, and dual runner invocations.
"""

import os
import time
import fcntl
import logging
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger("FileLock")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


import threading

_THREAD_LOCKS = threading.local()

def _get_thread_locks() -> dict:
    if not hasattr(_THREAD_LOCKS, "held"):
        _THREAD_LOCKS.held = {}
    return _THREAD_LOCKS.held


class LockTimeoutError(TimeoutError):
    """Raised when a cross-process file lock cannot be acquired within the timeout period."""
    pass


class FileLock:
    """
    Cross-process re-entrant or non-blocking advisory file lock.
    """

    def __init__(
        self,
        lock_path_or_name: str,
        timeout: float = 10.0,
        poll_interval: float = 0.05,
        lock_dir: Optional[str] = None,
        reentrant: bool = False,
    ):
        if "/" in lock_path_or_name or "\\" in lock_path_or_name:
            self.lock_path = lock_path_or_name
        else:
            base_dir = lock_dir or DATA_DIR
            self.lock_path = os.path.join(base_dir, f".{lock_path_or_name}.lock")

        self.timeout = timeout
        self.poll_interval = poll_interval
        self.reentrant = reentrant
        self._fd: Optional[int] = None
        self._is_reentrant_holder = False

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    @property
    def is_locked(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """
        Attempts to acquire the lock before timeout expires.
        Raises LockTimeoutError if timeout is exceeded.
        """
        norm_path = os.path.abspath(self.lock_path)
        held = _get_thread_locks()

        if self.reentrant and norm_path in held:
            held[norm_path]["count"] += 1
            self._fd = held[norm_path]["fd"]
            self._is_reentrant_holder = True
            return True

        self._is_reentrant_holder = False
        lock_dir = os.path.dirname(self.lock_path)
        if lock_dir and not os.path.exists(lock_dir):
            os.makedirs(lock_dir, exist_ok=True)

        start_time = time.time()
        self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR)

        delay = self.poll_interval
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if self.reentrant:
                    held[norm_path] = {"count": 1, "fd": self._fd}
                return True
            except (BlockingIOError, OSError):
                elapsed = time.time() - start_time
                if elapsed >= self.timeout:
                    if self._fd is not None:
                        try:
                            os.close(self._fd)
                        except OSError:
                            pass
                        self._fd = None
                    raise LockTimeoutError(
                        f"Failed to acquire lock on '{self.lock_path}' after {elapsed:.2f}s (timeout={self.timeout}s)."
                    )
                time.sleep(delay)
                delay = min(delay * 1.5, 0.5)

    def release(self) -> None:
        """Releases the held lock and closes descriptor."""
        norm_path = os.path.abspath(self.lock_path)
        held = _get_thread_locks()

        if self.reentrant and norm_path in held:
            held[norm_path]["count"] -= 1
            if held[norm_path]["count"] > 0:
                self._fd = None
                return
            del held[norm_path]

        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


# Alias for backward and test compatibility
NamedFileLock = FileLock


# Named Lock Helpers for Authoritative Systems
def crm_write_lock(timeout: float = 10.0, lock_dir: Optional[str] = None) -> FileLock:
    d = lock_dir or DATA_DIR
    return FileLock(os.path.join(d, ".crm_write.lock"), timeout=timeout, reentrant=True)


def commercial_records_lock(timeout: float = 10.0, lock_dir: Optional[str] = None) -> FileLock:
    d = lock_dir or DATA_DIR
    return FileLock(os.path.join(d, ".commercial_records.lock"), timeout=timeout, reentrant=True)


def proposal_records_lock(timeout: float = 10.0, lock_dir: Optional[str] = None) -> FileLock:
    d = lock_dir or DATA_DIR
    return FileLock(os.path.join(d, ".proposal_records.lock"), timeout=timeout, reentrant=True)


def campaign_lock(timeout: float = 10.0, lock_dir: Optional[str] = None) -> FileLock:
    d = lock_dir or DATA_DIR
    return FileLock(os.path.join(d, ".campaign.lock"), timeout=timeout, reentrant=True)


def quota_lock(timeout: float = 10.0, lock_dir: Optional[str] = None) -> FileLock:
    d = lock_dir or DATA_DIR
    return FileLock(os.path.join(d, ".quota_governor.lock"), timeout=timeout, reentrant=True)


def orchestrator_lock(timeout: float = 10.0, lock_dir: Optional[str] = None) -> FileLock:
    d = lock_dir or DATA_DIR
    return FileLock(os.path.join(d, ".technical_orchestrator.lock"), timeout=timeout, reentrant=True)

