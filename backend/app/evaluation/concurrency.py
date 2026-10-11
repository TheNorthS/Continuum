"""Bounded question scheduling with isolated workflows and shared provider limits."""

import asyncio

from app.agents.orchestrator import Orchestrator
from app.services.chat_service import ChatService


class DependencyFailureGuard:
    """Stop new questions after repeated infrastructure failures, not refusals."""

    errors = {
        "upstream_unavailable",
        "moderation_unavailable",
        "generation_timeout",
        "MilvusException",
        "DescribeCollectionException",
        "ConnectError",
        "ReadTimeout",
    }

    def __init__(self, limit=3):
        self.limit, self.consecutive = limit, 0

    def observe(self, row):
        failed = all(
            row.get(variant, {}).get("retrieval_error") in self.errors
            or (
                row.get(variant, {}).get("answer", {}).get("status") == "error"
                and row[variant]["answer"].get("error") in self.errors
            )
            for variant in ("rrf", "rerank")
        )
        self.consecutive = self.consecutive + 1 if failed else 0

    def check(self):
        if self.consecutive >= self.limit:
            raise RuntimeError("evaluation_dependency_unavailable")


def worker_chats(services, baseline):
    """Bind each arm once; never mutate the container's live orchestrator."""
    chats = {}
    for variant, knowledge in (("rrf", baseline), ("rerank", services.knowledge)):
        flow = Orchestrator(
            services.settings, services.llm, knowledge, services.web, services.tracing, services.moderation
        )
        chats[variant] = ChatService(services.settings, services.repository, flow, services.moderation)
    return chats


async def run_question_workers(queries, results, concurrency, factory, before_start, commit):
    if not 1 <= concurrency <= 4:
        raise ValueError("Question concurrency must be between 1 and 4")
    pending = iter((index, query) for index, query in enumerate(queries) if query["id"] not in results)
    stop_requested = False
    stop_reason = "evaluation_stop_requested"

    async def worker(worker_id):
        nonlocal stop_requested, stop_reason
        process = factory(worker_id)
        while not stop_requested:
            try:
                before_start()
            except RuntimeError as exc:
                if str(exc) not in {"evaluation_stop_requested", "evaluation_dependency_unavailable"}:
                    raise
                stop_requested = True
                stop_reason = str(exc)
                return
            item = next(pending, None)
            if item is None:
                return
            index, query = item
            row = await process(index, query)
            # No await: SQLite checkpoint/progress writes stay on the event-loop
            # thread and cannot interleave, including out-of-order completions.
            commit(query, row)

    async with asyncio.TaskGroup() as group:
        for worker_id in range(concurrency):
            group.create_task(worker(worker_id))
    if stop_requested:
        # STOP stops new work but lets all in-flight question pairs commit first.
        raise RuntimeError(stop_reason)
