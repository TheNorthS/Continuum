"""Renewable conversation ownership; SQL remains the final commit authority."""

import asyncio
import contextlib
import logging

from app.common.errors import AppError

logger = logging.getLogger(__name__)


async def settle(task):
    # to_thread 被取消不会停止底层线程；清理必须等待已发起操作，不遗留晚到的 claim。
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class ConversationLease:
    def __init__(self, repository, settings, conversation_id, user_id):
        self.repository = repository
        self.interval = settings.conversation_lease_renew_interval
        self.conversation_id, self.user_id = conversation_id, user_id
        self.token = None
        self.stop = asyncio.Event()
        self.heartbeat = None
        self.sealed = False

    async def renew(self):
        try:
            # 续租请求本身也有期限；数据库不可用时停止生成，不假定自己仍持有锁。
            async with asyncio.timeout(self.interval):
                await asyncio.to_thread(self.repository.renew, self.conversation_id, self.token)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("conversation_lease_unavailable", "会话续租失败，本轮已停止，请重试", 503) from exc

    async def watch(self):
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.interval)
            except TimeoutError:
                await self.renew()

    async def stop_renewing(self):
        self.stop.set()
        if self.heartbeat is not None:
            # 等待正在执行的续租完成；发生过的续租错误不能被停止操作掩盖。
            await self.heartbeat

    async def finish(self, question, result):
        # 提交前停止心跳，避免提交消费 token 后被心跳误报失租；随后再刷新一次有效期。
        await self.stop_renewing()
        await self.renew()
        await asyncio.to_thread(self.repository.finish, self.conversation_id, self.token, question, result)
        self.sealed = True

    async def cleanup(self, work):
        self.stop.set()
        if work is not None and not work.done():
            work.cancel()
        if self.heartbeat is not None and not self.heartbeat.done():
            self.heartbeat.cancel()
        await asyncio.gather(*(t for t in (work, self.heartbeat) if t is not None), return_exceptions=True)
        try:
            # release 只匹配自己的 token；延迟完成的续租也无法复活已释放或已接管的锁。
            await asyncio.to_thread(self.repository.release, self.conversation_id, self.token)
        except Exception:
            # 不覆盖原始失败或已经提交的结果；清理失败后由数据库租约自然到期。
            logger.warning("Conversation lease cleanup failed; expiry will reclaim ownership")

    async def run(self, operation):
        acquisition = asyncio.create_task(
            asyncio.to_thread(self.repository.claim, self.conversation_id, self.user_id)
        )
        try:
            self.token = await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            # 即使客户端在 claim 返回前断开，也要等待结果并释放可能刚获得的租约。
            with contextlib.suppress(Exception):
                self.token = await settle(acquisition)
                await settle(asyncio.create_task(self.cleanup(None)))
            raise
        work = None
        try:
            self.heartbeat = asyncio.create_task(self.watch(), name="conversation-lease-heartbeat")
            work = asyncio.create_task(operation(self), name="conversation-answer")
            done, _ = await asyncio.wait((work, self.heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if self.heartbeat in done:
                # 续租失败优先抛出，finally 会取消模型任务；正常停止则等待最终提交。
                self.heartbeat.result()
            result = await work
            await self.stop_renewing()
            if not self.sealed:
                # 拦截或手动压缩等不消费提交 token 的结果，返回前也须确认仍持有租约。
                await self.renew()
            return result
        finally:
            await settle(asyncio.create_task(self.cleanup(work)))
