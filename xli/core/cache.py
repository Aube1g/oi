#!/usr/bin/env python3
"""
XLI Cache v4 — Prompt hash → response, TTL, size limit
"""

import sqlite3
import hashlib
import json
import time

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.cache")

CACHE_DB = xli_path("cache.db")
DEFAULT_TTL = 3600  # 1 hour
MAX_SIZE = 1000  # Max entries


class LLMCache:
    """Cache for LLM responses with TTL"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._init_db()
        logger.log_structured("INFO", "cache", "LLMCache initialized")

    def _init_db(self):
        """Initialize cache database"""
        conn = sqlite3.connect(str(CACHE_DB))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                prompt_hash TEXT PRIMARY KEY,
                response TEXT,
                metadata TEXT,
                created_at REAL,
                expires_at REAL
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires_at)
        """)

        conn.commit()
        conn.close()

    def _hash_prompt(self, messages: list, provider: str, model: str, temperature: float) -> str:
        """Create hash from prompt parameters"""
        data = {
            "messages": messages,
            "provider": provider,
            "model": model,
            "temperature": temperature
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def get(self, messages: list, provider: str, model: str, temperature: float) -> str | None:
        """Get cached response if valid"""
        prompt_hash = self._hash_prompt(messages, provider, model, temperature)
        now = time.time()

        try:
            conn = sqlite3.connect(str(CACHE_DB))
            cursor = conn.cursor()

            cursor.execute("""
                SELECT response, expires_at FROM cache
                WHERE prompt_hash = ? AND expires_at > ?
            """, (prompt_hash, now))

            row = cursor.fetchone()
            conn.close()

            if row:
                logger.log_structured("DEBUG", "cache", "Cache HIT", {"hash": prompt_hash[:16]})
                return row[0]

            logger.log_structured("DEBUG", "cache", "Cache MISS", {"hash": prompt_hash[:16]})
            return None

        except Exception as e:
            logger.log_error("cache", "Get failed", exc=e)
            return None

    def set(self, messages: list, provider: str, model: str, temperature: float,
            response: str, ttl: int = DEFAULT_TTL):
        """Cache response with TTL"""
        prompt_hash = self._hash_prompt(messages, provider, model, temperature)
        now = time.time()
        expires = now + ttl

        try:
            conn = sqlite3.connect(str(CACHE_DB))
            cursor = conn.cursor()

            # Check size limit
            cursor.execute("SELECT COUNT(*) FROM cache")
            count = cursor.fetchone()[0]

            if count >= MAX_SIZE:
                # Evict oldest entries
                cursor.execute("""
                    DELETE FROM cache WHERE prompt_hash IN (
                        SELECT prompt_hash FROM cache
                        ORDER BY created_at ASC
                        LIMIT ?
                    )
                """, (count - MAX_SIZE + 1,))

            cursor.execute("""
                INSERT OR REPLACE INTO cache 
                (prompt_hash, response, metadata, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (prompt_hash, response, json.dumps({"provider": provider, "model": model}), now, expires))

            conn.commit()
            conn.close()

            logger.log_structured("DEBUG", "cache", "Cache SET",
                                 {"hash": prompt_hash[:16], "ttl": ttl})

        except Exception as e:
            logger.log_error("cache", "Set failed", exc=e)

    def invalidate(self, pattern: str = None):
        """Invalidate cache entries"""
        try:
            conn = sqlite3.connect(str(CACHE_DB))
            cursor = conn.cursor()

            if pattern:
                cursor.execute("DELETE FROM cache WHERE prompt_hash LIKE ?", (f"%{pattern}%",))
            else:
                cursor.execute("DELETE FROM cache")

            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            logger.log_structured("INFO", "cache", f"Invalidated {deleted} entries")

        except Exception as e:
            logger.log_error("cache", "Invalidate failed", exc=e)

    def cleanup_expired(self):
        """Remove expired entries"""
        try:
            conn = sqlite3.connect(str(CACHE_DB))
            cursor = conn.cursor()

            now = time.time()
            cursor.execute("DELETE FROM cache WHERE expires_at < ?", (now,))

            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            logger.log_structured("DEBUG", "cache", f"Cleaned {deleted} expired entries")

        except Exception as e:
            logger.log_error("cache", "Cleanup failed", exc=e)


def get_cache() -> LLMCache:
    """Get singleton LLMCache"""
    return LLMCache()

