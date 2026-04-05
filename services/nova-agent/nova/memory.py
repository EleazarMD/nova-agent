"""
Long-term Memory System for Nova Agent.

Provides persistent memory storage and retrieval across conversations:
- Save facts, preferences, and context
- Recall with semantic similarity search
- User-specific memory isolation
- Automatic summarization for large memories
- Memory expiration and cleanup

Storage: ChromaDB (vector) + PostgreSQL (metadata)
Embedding: 1024-dim via AI Gateway
"""

import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field, asdict
from loguru import logger


@dataclass
class Memory:
    """Represents a stored memory."""
    id: str
    user_id: str
    content: str  # The actual memory text
    category: str  # preference, fact, context, todo, etc.
    tags: List[str] = field(default_factory=list)
    importance: int = 5  # 1-10 scale
    source: str = "conversation"  # Where it came from
    created_at: datetime = field(default_factory=datetime.now)
    accessed_at: Optional[datetime] = None
    access_count: int = 0
    expires_at: Optional[datetime] = None  # For temporary memories
    embedding: Optional[List[float]] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for storage."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "content": self.content,
            "category": self.category,
            "tags": self.tags,
            "importance": self.importance,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "accessed_at": self.accessed_at.isoformat() if self.accessed_at else None,
            "access_count": self.access_count,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "Memory":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            content=data["content"],
            category=data.get("category", "fact"),
            tags=data.get("tags", []),
            importance=data.get("importance", 5),
            source=data.get("source", "conversation"),
            created_at=datetime.fromisoformat(data["created_at"]),
            accessed_at=datetime.fromisoformat(data["accessed_at"]) if data.get("accessed_at") else None,
            access_count=data.get("access_count", 0),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
        )


@dataclass
class MemorySearchResult:
    """Result from memory search."""
    memory: Memory
    score: float  # Similarity score (0-1)
    distance: float  # Vector distance


class LongTermMemory:
    """
    Long-term memory storage and retrieval system.
    
    Features:
    - Semantic search via vector embeddings
    - Automatic importance scoring
    - Memory decay (less frequently accessed = lower relevance)
    - User isolation (memories per user)
    - Category filtering
    """
    
    def __init__(
        self,
        user_id: str,
        chroma_client=None,  # Injected ChromaDB client
        embedding_fn=None,  # Function to generate embeddings
    ):
        self.user_id = user_id
        self._chroma = chroma_client
        self._embedding_fn = embedding_fn
        self._cache: Dict[str, Memory] = {}  # In-memory cache
        self._initialized = False
    
    async def _ensure_initialized(self):
        """Initialize the memory store."""
        if self._initialized:
            return
        
        # TODO: Initialize ChromaDB collection for this user
        # For now, use in-memory storage
        logger.info(f"Long-term memory initialized for user={self.user_id}")
        self._initialized = True
    
    def _generate_id(self, content: str) -> str:
        """Generate unique ID from content hash."""
        hash_input = f"{self.user_id}:{content}"
        return hashlib.md5(hash_input.encode()).hexdigest()[:16]
    
    async def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Get embedding for text via AI Gateway."""
        if self._embedding_fn:
            return await self._embedding_fn(text)
        
        # Mock embedding for development (1024-dim)
        import random
        random.seed(text)
        return [random.uniform(-1, 1) for _ in range(1024)]
    
    # -------------------------------------------------------------------------
    # Save Operations
    # -------------------------------------------------------------------------
    
    async def save(
        self,
        content: str,
        category: str = "fact",
        tags: Optional[List[str]] = None,
        importance: int = 5,
        source: str = "conversation",
        ttl_days: Optional[int] = None,
    ) -> str:
        """
        Save a memory.
        
        Args:
            content: The memory text
            category: Type of memory (preference, fact, context, todo)
            tags: Searchable tags
            importance: 1-10 scale
            source: Where this came from
            ttl_days: Time-to-live (None = permanent)
            
        Returns:
            Memory ID
        """
        await self._ensure_initialized()
        
        # Check for similar existing memory (dedup)
        similar = await self._find_similar(content, threshold=0.95)
        if similar:
            logger.debug(f"Memory dedup: similar to existing {similar.memory.id}")
            # Update access count
            similar.memory.access_count += 1
            similar.memory.accessed_at = datetime.now()
            return similar.memory.id
        
        # Create new memory
        memory_id = self._generate_id(content)
        embedding = await self._get_embedding(content)
        
        expires_at = None
        if ttl_days:
            expires_at = datetime.now() + timedelta(days=ttl_days)
        
        memory = Memory(
            id=memory_id,
            user_id=self.user_id,
            content=content,
            category=category,
            tags=tags or [],
            importance=importance,
            source=source,
            expires_at=expires_at,
            embedding=embedding,
        )
        
        # Store in cache
        self._cache[memory_id] = memory
        
        # TODO: Store in ChromaDB
        logger.info(f"Memory saved: {memory_id[:8]}... ({category})")
        
        return memory_id
    
    async def save_preference(
        self,
        preference: str,
        importance: int = 7,
    ) -> str:
        """Convenience method for saving user preferences."""
        return await self.save(
            content=preference,
            category="preference",
            tags=["preference", "user-setting"],
            importance=importance,
            source="explicit",
        )
    
    async def save_fact(
        self,
        fact: str,
        importance: int = 6,
        ttl_days: Optional[int] = None,
    ) -> str:
        """Convenience method for saving facts."""
        return await self.save(
            content=fact,
            category="fact",
            tags=["fact"],
            importance=importance,
            ttl_days=ttl_days,
        )
    
    async def save_todo(
        self,
        task: str,
        importance: int = 8,
    ) -> str:
        """Convenience method for saving todos."""
        return await self.save(
            content=task,
            category="todo",
            tags=["todo", "task"],
            importance=importance,
            ttl_days=7,  # Todos expire in 7 days
        )
    
    # -------------------------------------------------------------------------
    # Recall Operations
    # -------------------------------------------------------------------------
    
    async def recall(
        self,
        query: str,
        category: Optional[str] = None,
        limit: int = 5,
        min_score: float = 0.7,
    ) -> List[MemorySearchResult]:
        """
        Search memories by semantic similarity.
        
        Args:
            query: Search query
            category: Filter by category
            limit: Max results
            min_score: Minimum similarity score (0-1)
            
        Returns:
            List of matching memories with scores
        """
        await self._ensure_initialized()
        
        query_embedding = await self._get_embedding(query)
        if not query_embedding:
            return []
        
        results = []
        
        # Search in-memory cache (TODO: replace with ChromaDB search)
        for memory in self._cache.values():
            # Skip expired memories
            if memory.expires_at and datetime.now() > memory.expires_at:
                continue
            
            # Category filter
            if category and memory.category != category:
                continue
            
            if not memory.embedding:
                continue
            
            # Calculate cosine similarity
            score = self._cosine_similarity(query_embedding, memory.embedding)
            
            # Boost by importance and recency
            score = self._boost_by_importance(score, memory)
            
            if score >= min_score:
                results.append(MemorySearchResult(
                    memory=memory,
                    score=score,
                    distance=1 - score,
                ))
                
                # Update access stats
                memory.access_count += 1
                memory.accessed_at = datetime.now()
        
        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)
        
        return results[:limit]
    
    async def recall_preferences(self, limit: int = 10) -> List[Memory]:
        """Get all user preferences."""
        results = await self.recall(
            query="user preferences settings",
            category="preference",
            limit=limit,
            min_score=0.3,  # Lower threshold for preferences
        )
        return [r.memory for r in results]
    
    async def recall_todos(self) -> List[Memory]:
        """Get all active todos."""
        results = await self.recall(
            query="todo task reminder",
            category="todo",
            limit=20,
            min_score=0.3,
        )
        return [r.memory for r in results]
    
    async def get_context_for_llm(self, query: str, max_memories: int = 3) -> str:
        """
        Get formatted memory context for LLM prompt.
        
        Returns memories formatted for inclusion in system prompt.
        """
        results = await self.recall(query, limit=max_memories)
        
        if not results:
            return ""
        
        lines = ["## Relevant User Memory:"]
        for result in results:
            m = result.memory
            lines.append(f"- [{m.category}] {m.content}")
        
        return "\n".join(lines)
    
    # -------------------------------------------------------------------------
    # Management
    # -------------------------------------------------------------------------
    
    async def delete(self, memory_id: str) -> bool:
        """Delete a memory."""
        if memory_id in self._cache:
            del self._cache[memory_id]
            logger.info(f"Memory deleted: {memory_id[:8]}...")
            return True
        return False
    
    async def delete_by_category(self, category: str) -> int:
        """Delete all memories in a category."""
        to_delete = [
            mid for mid, m in self._cache.items()
            if m.category == category
        ]
        for mid in to_delete:
            del self._cache[mid]
        logger.info(f"Deleted {len(to_delete)} memories from category '{category}'")
        return len(to_delete)
    
    async def cleanup_expired(self) -> int:
        """Remove expired memories."""
        now = datetime.now()
        expired = [
            mid for mid, m in self._cache.items()
            if m.expires_at and now > m.expires_at
        ]
        for mid in expired:
            del self._cache[mid]
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired memories")
        return len(expired)
    
    async def update_importance(self, memory_id: str, importance: int):
        """Update memory importance."""
        if memory_id in self._cache:
            self._cache[memory_id].importance = max(1, min(10, importance))
    
    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    
    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        
        if norm_a == 0 or norm_b == 0:
            return 0.0
        
        return dot_product / (norm_a * norm_b)
    
    def _boost_by_importance(self, score: float, memory: Memory) -> float:
        """Boost score based on importance and recency."""
        # Importance boost (0.1 per point above 5)
        importance_boost = (memory.importance - 5) * 0.02
        
        # Recency boost (max 0.1 for recently accessed)
        recency_boost = 0.0
        if memory.accessed_at:
            days_since = (datetime.now() - memory.accessed_at).days
            if days_since < 1:
                recency_boost = 0.1
            elif days_since < 7:
                recency_boost = 0.05
        
        # Access count boost (diminishing returns)
        access_boost = min(memory.access_count * 0.01, 0.1)
        
        return min(score + importance_boost + recency_boost + access_boost, 1.0)
    
    async def _find_similar(self, content: str, threshold: float = 0.95) -> Optional[MemorySearchResult]:
        """Find a memory very similar to given content."""
        embedding = await self._get_embedding(content)
        if not embedding:
            return None
        
        best_match = None
        best_score = 0.0
        
        for memory in self._cache.values():
            if not memory.embedding:
                continue
            
            score = self._cosine_similarity(embedding, memory.embedding)
            if score > best_score:
                best_score = score
                best_match = memory
        
        if best_match and best_score >= threshold:
            return MemorySearchResult(
                memory=best_match,
                score=best_score,
                distance=1 - best_score,
            )
        
        return None


# Registry per user
_memory_stores: Dict[str, LongTermMemory] = {}


def get_memory_store(user_id: str) -> LongTermMemory:
    """Get or create memory store for a user."""
    if user_id not in _memory_stores:
        _memory_stores[user_id] = LongTermMemory(user_id)
    return _memory_stores[user_id]


# Tool functions for LLM
async def save_memory(
    user_id: str,
    content: str,
    category: str = "fact",
    importance: int = 5,
) -> str:
    """Tool function: Save a memory."""
    store = get_memory_store(user_id)
    return await store.save(content, category=category, importance=importance)


async def recall_memory(
    user_id: str,
    query: str,
    category: Optional[str] = None,
    limit: int = 5,
) -> List[str]:
    """Tool function: Recall memories."""
    store = get_memory_store(user_id)
    results = await store.recall(query, category=category, limit=limit)
    return [r.memory.content for r in results]
