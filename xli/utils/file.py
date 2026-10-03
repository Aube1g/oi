#!/usr/bin/env python3
"""
XLI Utils — FileOps: atomic writes
"""

import os
import tempfile
from pathlib import Path

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.utils.file")


class FileOps:
    """Atomic file operations"""

    @staticmethod
    def atomic_write(path: str, content: str, mode: str = "w") -> bool:
        """Write a file atomically via temp file + rename.

        Returns False rather than raising when the write cannot happen: the
        contract is a boolean, so a caller checking the result must not also
        have to catch NotADirectoryError from the mkdir below.
        """
        target = Path(path)

        # Write to a temp file in the same directory, then rename over the
        # target. Same-directory matters: os.replace is only atomic within one
        # filesystem.
        fd = None
        temp_path = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.tmp_")

            with os.fdopen(fd, mode) as f:
                fd = None  # the file object owns the descriptor now
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, str(target))
            temp_path = None  # successfully consumed by the rename
            logger.log_structured("INFO", "file", f"atomic write: {path}")
            return True

        except OSError as exc:
            logger.log_error("file", f"atomic write failed: {path}", exc=exc)
            return False

        finally:
            # Never leave a half-written temp file behind, whichever step failed.
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass


    @staticmethod
    def read_lines(path: str, encoding: str = "utf-8") -> list:
        """Read file as lines"""
        try:
            return Path(path).read_text(encoding=encoding).splitlines()
        except Exception as e:
            logger.log_error("file", f"Read failed: {path}", exc=e)
            return []

    @staticmethod
    def ensure_dir(path: str) -> Path:
        """Ensure directory exists"""
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        return p
