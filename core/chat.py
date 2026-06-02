import time
import uuid
import asyncio
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: float = Field(default_factory=time.time)
    session_id: Optional[str] = None


class Conversation(BaseModel):
    id: str
    title: str = "New chat"
    agent_id: str = "default"
    messages: List[ChatMessage] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class ChatStore:
    """In-memory conversation history for ChatGPT-style UI."""

    def __init__(self):
        self._conversations: Dict[str, Conversation] = {}
        self._lock = asyncio.Lock()

    async def create_conversation(self, agent_id: str = "default", title: str = "New chat") -> Conversation:
        conv_id = f"conv_{uuid.uuid4().hex[:10]}"
        conv = Conversation(id=conv_id, title=title, agent_id=agent_id)
        async with self._lock:
            self._conversations[conv_id] = conv
        return conv.model_copy(deep=True)

    async def list_conversations(self) -> List[Conversation]:
        async with self._lock:
            convs = sorted(
                self._conversations.values(),
                key=lambda c: c.updated_at,
                reverse=True,
            )
            return [c.model_copy(deep=True) for c in convs]

    async def get_conversation(self, conv_id: str) -> Optional[Conversation]:
        async with self._lock:
            conv = self._conversations.get(conv_id)
            return conv.model_copy(deep=True) if conv else None

    async def add_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        session_id: Optional[str] = None,
    ) -> Optional[Conversation]:
        async with self._lock:
            conv = self._conversations.get(conv_id)
            if not conv:
                return None
            conv.messages.append(ChatMessage(role=role, content=content, session_id=session_id))
            conv.updated_at = time.time()
            if role == "user" and conv.title == "New chat":
                conv.title = content[:48] + ("…" if len(content) > 48 else "")
            return conv.model_copy(deep=True)

    async def delete_conversation(self, conv_id: str) -> bool:
        async with self._lock:
            if conv_id in self._conversations:
                del self._conversations[conv_id]
                return True
            return False
