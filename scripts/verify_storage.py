"""Integration probe for an isolated PostgreSQL + Redis deployment.

Run inside the validation image. It cleans up only the rows it creates.
"""

import asyncio
import json

from app.config import Settings
from app.infrastructure.container import Container


async def main():
    services = Container(Settings())
    document_id = conversation_id = None
    try:
        await services.start()
        assert services.db.engine.dialect.name == "postgresql"
        assert services.cache.redis and await services.cache.redis.ping()
        document = await services.knowledge.ingest(
            "validation.md", b"# EUV\nEUV lithography uses 13.5 nm light."
        )
        assert not document["duplicate"] and document["status"] == "ready"
        document_id = document["id"]
        first = await services.knowledge.search("EUV", 4)
        second = await services.knowledge.search("EUV", 4)
        assert first == second and first
        assert await services.cache.redis.dbsize() > 0
        user = services.repository.user(username=services.settings.admin_username)
        conversation = services.repository.create_conversation(
            user["id"], "storage validation"
        )
        conversation_id = conversation["id"]
        answer = await services.chat.answer(
            conversation_id, user["id"], "EUV", "adaptive"
        )
        assert (
            answer["sources"]
            and len(services.repository.history(conversation_id, user["id"])) == 2
        )
        await services.knowledge.delete(document_id)
        document_id = None
        assert await services.knowledge.search("EUV", 4) == []
        print(
            json.dumps(
                {
                    "postgresql": "passed",
                    "redis_cache": "passed",
                    "chat_transaction": "passed",
                    "delete_invalidation": "passed",
                }
            )
        )
    finally:
        if document_id:
            await services.knowledge.delete(document_id)
        if conversation_id:
            services.repository.delete_conversation(conversation_id, user["id"])
        await services.close()


if __name__ == "__main__":
    asyncio.run(main())
