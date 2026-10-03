#!/usr/bin/env python3
"""
XLI Memory v4 — SQLite conversations, semantic search by history
"""

import sqlite3
import hashlib
from datetime import datetime

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.memory")

MEMORY_DB = xli_path("memory.db")


class ConversationMemory:
    """Persistent conversation memory with search"""

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
        logger.log_structured("INFO", "memory", "ConversationMemory initialized")

    def _init_db(self):
        """Initialize memory database"""
        conn = sqlite3.connect(str(MEMORY_DB))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                task_hash TEXT UNIQUE,
                task TEXT,
                result TEXT,
                agent TEXT,
                keywords TEXT,
                success BOOLEAN
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp ON conversations(timestamp)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_keywords ON conversations(keywords)
        """)

        conn.commit()
        conn.close()

    def _extract_keywords(self, text: str) -> str:
        """Extract keywords from text"""
        # Simple keyword extraction
        words = text.lower().split()
        # Filter common words and take meaningful ones
        stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                     "being", "have", "has", "had", "do", "does", "did", "will",
                     "would", "could", "should", "may", "might", "must", "shall",
                     "can", "need", "dare", "ought", "used", "to", "of", "in",
                     "for", "on", "with", "at", "by", "from", "as", "into",
                     "through", "during", "before", "after", "above", "below",
                     "between", "under", "and", "but", "or", "yet", "so", "if",
                     "because", "although", "though", "while", "where", "when",
                     "that", "which", "who", "whom", "whose", "what", "this",
                     "these", "those", "i", "me", "my", "mine", "myself", "you",
                     "your", "yours", "yourself", "he", "him", "his", "himself",
                     "she", "her", "hers", "herself", "it", "its", "itself",
                     "we", "us", "our", "ours", "ourselves", "they", "them",
                     "their", "theirs", "themselves", "one", "ones", "anyone",
                     "someone", "everyone", "noone", "nobody", "nothing",
                     "anything", "something", "everything", "each", "every",
                     "all", "some", "any", "no", "none", "both", "either",
                     "neither", "many", "much", "few", "little", "more",
                     "most", "other", "another", "such", "only", "own",
                     "same", "than", "too", "very", "just", "now",
                     "then", "here", "there", "once", "again", "also",
                     "back", "still", "even", "why", "how", "nor", "not"}

        keywords = [w for w in words if len(w) > 3 and w not in stopwords]
        return " ".join(keywords[:20])  # Limit keywords

    def save_conversation(self, task: str, result: str, agent: str, success: bool = True):
        """Save conversation to memory"""
        task_hash = hashlib.sha256(task.encode()).hexdigest()[:16]
        keywords = self._extract_keywords(task + " " + result)

        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cursor = conn.cursor()

            cursor.execute("""
                INSERT OR REPLACE INTO conversations 
                (task_hash, task, result, agent, keywords, success)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (task_hash, task, result, agent, keywords, success))

            conn.commit()
            conn.close()

            logger.log_structured("DEBUG", "memory", "Conversation saved",
                                 {"hash": task_hash, "agent": agent})

        except Exception as e:
            logger.log_error("memory", "Failed to save conversation", exc=e)

    def search_memory(self, query: str, limit: int = 5) -> list[dict]:
        """Search memory by keywords"""
        keywords = self._extract_keywords(query)
        query_terms = keywords.split()

        if not query_terms:
            return []

        # Build LIKE query
        conditions = " OR ".join(["keywords LIKE ?"] * len(query_terms))
        params = [f"%{term}%" for term in query_terms]

        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cursor = conn.cursor()

            cursor.execute(f"""
                SELECT task, result, agent, timestamp, success
                FROM conversations
                WHERE {conditions}
                ORDER BY timestamp DESC
                LIMIT ?
            """, (*params, limit))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "task": row[0],
                    "result": row[1],
                    "agent": row[2],
                    "timestamp": row[3],
                    "success": row[4]
                })

            conn.close()

            logger.log_structured("DEBUG", "memory",
                                 f"Search '{query[:30]}': {len(results)} results")
            return results

        except Exception as e:
            logger.log_error("memory", "Search failed", exc=e)
            return []

    def get_recent(self, limit: int = 10) -> list[dict]:
        """Get recent conversations"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cursor = conn.cursor()

            cursor.execute("""
                SELECT task, result, agent, timestamp, success
                FROM conversations
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))

            results = []
            for row in cursor.fetchall():
                results.append({
                    "task": row[0],
                    "result": row[1],
                    "agent": row[2],
                    "timestamp": row[3],
                    "success": row[4]
                })

            conn.close()
            return results

        except Exception as e:
            logger.log_error("memory", "Get recent failed", exc=e)
            return []

    def get_context_for_task(self, task: str, max_results: int = 3) -> str:
        """Get relevant context from memory for a task"""
        results = self.search_memory(task, limit=max_results)

        if not results:
            return ""

        context = "\n\n📜 **Relevant History:**\n"
        for r in results:
            status = "✅" if r["success"] else "❌"
            context += f"\n{status} [{r['agent']}] {r['task'][:100]}...\n"
            context += f"   → {r['result'][:150]}...\n"

        return context

    def clear_old(self, days: int = 30):
        """Clear conversations older than N days"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cursor = conn.cursor()

            cursor.execute(f"""
                DELETE FROM conversations
                WHERE timestamp < datetime('now', '-{days} days')
            """)

            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            logger.log_structured("INFO", "memory", f"Cleared {deleted} old conversations")

        except Exception as e:
            logger.log_error("memory", "Clear old failed", exc=e)


    def add_turn(self, role: str, content: str):
        """Add a conversation turn to memory"""
        try:
            conn = sqlite3.connect(str(MEMORY_DB))
            cursor = conn.cursor()

            task_hash = hashlib.sha256(f"turn_{role}_{content[:50]}_{datetime.now().isoformat()}".encode()).hexdigest()[:16]
            keywords = self._extract_keywords(content)

            cursor.execute("""
                INSERT OR REPLACE INTO conversations 
                (task_hash, task, result, agent, keywords, success)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (task_hash, f"[{role}] {content[:200]}", content, role, keywords, True))

            conn.commit()
            conn.close()

            logger.log_structured("DEBUG", "memory", "Turn added", {"role": role, "len": len(content)})

        except Exception as e:
            logger.log_error("memory", "Failed to add turn", exc=e)

def get_memory() -> ConversationMemory:
    """Get singleton ConversationMemory"""
    return ConversationMemory()

