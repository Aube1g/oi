#!/usr/bin/env python3
"""
XLI Vector Store v4 — FAISS, semantic search, project indexing
"""

import json
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.vector")

# numpy and faiss are both part of the optional `embeddings` extra. faiss was
# already guarded; numpy was not, so importing this module without the extra
# installed raised ModuleNotFoundError before the HAS_FAISS check could ever
# run — the graceful degradation was unreachable.
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore[assignment]
    HAS_NUMPY = False

try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

if not HAS_NUMPY or not HAS_FAISS:
    missing = [n for n, ok in (("numpy", HAS_NUMPY), ("faiss-cpu", HAS_FAISS)) if not ok]
    logger.log_structured(
        "WARN",
        "vector",
        f"semantic search unavailable, install with: pip install 'xli[embeddings]' "
        f"(missing: {', '.join(missing)})",
    )


class CodeVectorStore:
    """Semantic code search with FAISS"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = False  # Will be set to True after init

        if not HAS_FAISS or not HAS_NUMPY:
            logger.log_structured("ERROR", "vector", "FAISS not available")
            return

        self.index = None
        self.documents: dict[int, dict] = {}
        self.dimension = 384  # MiniLM dimension
        self.index_path = xli_path("vector.index")
        self.docs_path = xli_path("vector.docs.json")

        self._load()
        self._initialized = True
        logger.log_structured("INFO", "vector", "VectorStore initialized")

    def _get_embedding(self, text: str) -> "np.ndarray":
        """Get embedding for text (simplified — would use sentence-transformers)"""
        # Placeholder: random embedding for now
        # In production: use sentence-transformers/all-MiniLM-L6-v2
        np.random.seed(hash(text) % 2**32)
        return np.random.randn(self.dimension).astype("float32")

    def _load(self):
        """Load existing index"""
        if self.index_path.exists() and self.docs_path.exists():
            try:
                self.index = faiss.read_index(str(self.index_path))
                with open(self.docs_path) as f:
                    self.documents = {int(k): v for k, v in json.load(f).items()}
                logger.log_structured("INFO", "vector",
                                     f"Loaded index with {len(self.documents)} docs")
            except Exception as e:
                logger.log_error("vector", "Load failed", exc=e)
                self._create_new_index()
        else:
            self._create_new_index()

    def _create_new_index(self):
        """Create new FAISS index"""
        self.index = faiss.IndexFlatIP(self.dimension)  # Inner product = cosine for normalized
        self.documents = {}

    def _save(self):
        """Save index and documents"""
        try:
            faiss.write_index(self.index, str(self.index_path))
            with open(self.docs_path, "w") as f:
                json.dump(self.documents, f)
            logger.log_structured("DEBUG", "vector",
                                 f"Saved index with {len(self.documents)} docs")
        except Exception as e:
            logger.log_error("vector", "Save failed", exc=e)

    def add_document(self, path: str, content: str, doc_type: str = "code"):
        """Add document to index"""
        if not self._initialized or not HAS_FAISS or not HAS_NUMPY:
            return

        doc_id = len(self.documents)
        embedding = self._get_embedding(content)
        embedding = embedding / np.linalg.norm(embedding)  # Normalize

        self.index.add(embedding.reshape(1, -1))
        self.documents[doc_id] = {
            "path": path,
            "content": content[:1000],  # Store preview
            "type": doc_type,
            "hash": hashlib.sha256(content.encode()).hexdigest()[:16]
        }

        self._save()
        logger.log_structured("DEBUG", "vector", f"Added: {path}")

    def index_project(self, directory: str, pattern: str = "*.py"):
        """Index all files in project"""
        if not self._initialized or not HAS_FAISS or not HAS_NUMPY:
            logger.log_structured("WARN", "vector", "Cannot index — FAISS unavailable")
            return 0

        count = 0
        for file_path in Path(directory).rglob(pattern):
            if any(x in str(file_path) for x in ["venv", "__pycache__", ".git", "node_modules"]):
                continue

            try:
                content = file_path.read_text(errors="ignore")
                self.add_document(str(file_path), content)
                count += 1
            except Exception as e:
                logger.log_error("vector", f"Failed to index {file_path}", exc=e)

        logger.log_structured("INFO", "vector", f"Indexed {count} files from {directory}")
        return count

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, str, float]]:
        """Semantic search"""
        if not self._initialized or not HAS_FAISS or not HAS_NUMPY or len(self.documents) == 0:
            return []

        embedding = self._get_embedding(query)
        embedding = embedding / np.linalg.norm(embedding)

        scores, indices = self.index.search(embedding.reshape(1, -1), top_k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx >= 0 and idx in self.documents:
                doc = self.documents[idx]
                results.append((doc["path"], doc["content"][:200], float(score)))

        logger.log_structured("DEBUG", "vector",
                             f"Search '{query[:30]}': {len(results)} results")
        return results


def get_vector_store() -> CodeVectorStore:
    """Get singleton CodeVectorStore"""
    return CodeVectorStore()

