"""
lib/system/atomic_writer.py
===========================
Atomic file persistence operations for JSON and text records.
Guarantees:
  1. Writes to temporary file on the same filesystem.
  2. Fsyncs buffer to durable media before rename.
  3. Uses os.replace for atomic replacement on POSIX filesystems.
  4. Zero half-written JSON, zero truncated files, zero corrupted state.
  5. Transactional rollback support for multi-file operations.
"""

import os
import json
import uuid
import shutil
import logging
from typing import Any, List, Optional
from contextlib import contextmanager

logger = logging.getLogger("AtomicWriter")


class AtomicWriteError(IOError):
    """Raised when an atomic write or transactional operation fails."""
    pass


def atomic_write_text(filepath: str, content: str, encoding: str = "utf-8") -> str:
    """
    Atomically writes text content to target file path.
    """
    target_dir = os.path.dirname(filepath)
    if target_dir and not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    temp_path = f"{filepath}.tmp.{uuid.uuid4().hex}"
    try:
        with open(temp_path, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
        return filepath
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        logger.error(f"Atomic text write failed for {filepath}: {e}")
        raise


def atomic_write_json(
    filepath: str,
    data: Any,
    indent: int = 2,
    default: Optional[Any] = None,
    encoding: str = "utf-8",
) -> str:
    """
    Atomically writes Python data as formatted JSON.
    """
    serialized = json.dumps(data, indent=indent, default=default, ensure_ascii=False)
    return atomic_write_text(filepath, serialized, encoding=encoding)


@contextmanager
def atomic_transaction(filepaths: List[str]):
    """
    Context manager for multi-file bulk operations.
    Snapshots existing files to temporary backup copies before the block runs.
    If an unhandled exception occurs, rolls back all files to their pre-transaction state.
    On clean exit, cleans up backup copies.
    """
    backups = {}
    tx_id = uuid.uuid4().hex[:8]

    # Phase 1: Create backup copies
    for fp in filepaths:
        if os.path.exists(fp):
            bak_path = f"{fp}.txbak.{tx_id}"
            shutil.copy2(fp, bak_path)
            backups[fp] = bak_path

    try:
        yield
    except Exception as e:
        logger.warning(f"Transaction failed ({e}). Initiating automatic rollback of {len(backups)} files...")
        for fp, bak_path in backups.items():
            if os.path.exists(bak_path):
                try:
                    os.replace(bak_path, fp)
                    logger.info(f"Successfully rolled back {fp}")
                except Exception as rollback_err:
                    logger.error(f"Failed rolling back {fp}: {rollback_err}")
        raise
    finally:
        # Phase 2: Cleanup remaining backup files
        for bak_path in backups.values():
            if os.path.exists(bak_path):
                try:
                    os.remove(bak_path)
                except OSError:
                    pass
