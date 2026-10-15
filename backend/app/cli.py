import argparse
import asyncio
import getpass
import json
from pathlib import Path

from app.api.schemas import Credentials
from app.common.encryption import FIELDS, Cipher, encrypt_database
from app.config import ROOT, Settings
from app.infrastructure.container import Container


async def execute(args):
    settings = Settings()
    if args.command == "memory-worker":
        settings.memory_worker_enabled = (
            False  # Dedicated worker owns its loop; do not also spawn an embedded one.
        )
    if args.command == "keys-init":
        cipher = Cipher(settings.encryption_key_file, create=True)
        print(json.dumps({"key_file": str(cipher.path), "active_key": cipher.active}))
        return
    services = Container(settings)
    try:
        await services.start()
        if args.command == "init":
            print("知恒 · Continuum 数据库及管理员已就绪（已有管理员密码不会被覆盖）。")
        elif args.command == "memory-worker":
            if args.once:
                print(json.dumps({"processed": await services.memory.run_once()}))
            else:
                while True:
                    await services.memory.run_once()
                    await asyncio.sleep(settings.memory_worker_interval)
        elif args.command == "ingest":
            directory = Path(args.directory).resolve()
            files = sorted(
                p
                for p in directory.rglob("*")
                if p.is_file() and p.suffix.lower() in {".md", ".markdown", ".txt"}
            )
            if not files:
                raise SystemExit("目录中没有 Markdown/TXT 文件")
            failures = 0
            # Bounded producer/consumer queue gives back-pressure without import-time subprocesses.
            queue = asyncio.Queue(maxsize=4)

            async def worker():
                nonlocal failures
                while True:
                    path = await queue.get()
                    try:
                        if path is None:
                            return
                        if path.stat().st_size > settings.max_upload_bytes:
                            raise ValueError("文件超出大小限制")
                        result = await services.knowledge.ingest(
                            path.name, await asyncio.to_thread(path.read_bytes)
                        )
                        print(
                            json.dumps(
                                {
                                    "file": path.name,
                                    "status": result["status"],
                                    "chunks": result["chunk_count"],
                                    "duplicate": result["duplicate"],
                                },
                                ensure_ascii=False,
                            )
                        )
                        if result["status"] != "ready":
                            failures += 1
                    except Exception as exc:
                        failures += 1
                        print(
                            json.dumps(
                                {"file": path.name, "error_type": type(exc).__name__}, ensure_ascii=False
                            )
                        )
                    finally:
                        queue.task_done()

            tasks = [asyncio.create_task(worker()) for _ in range(2)]
            for path in files:
                await queue.put(path)
            for _ in tasks:
                await queue.put(None)
            await queue.join()
            await asyncio.gather(*tasks)
            if failures:
                raise SystemExit(f"{failures} 个文件未完成导入，请检查文档状态")
        elif args.command == "create-user":
            password = getpass.getpass("新用户密码（至少 8 字符）: ")
            if len(password) < 8:
                raise SystemExit("密码过短")
            Credentials(username=args.username, password=password, challenge="operator-validation-only")
            result = await asyncio.to_thread(
                services.security.create_user,
                args.username,
                password,
                args.role,
            )
            print(f"已创建 {result['username']} ({result['role']})")
        elif args.command == "chat":
            # Local CLI is an operator tool, using the configured admin identity.
            user = await asyncio.to_thread(services.repository.user, username=settings.admin_username)
            conversation = await asyncio.to_thread(
                services.repository.create_conversation, user["id"], "CLI 对话"
            )
            print("输入 q / exit 结束。")
            while True:
                question = await asyncio.to_thread(input, "你: ")
                if question.lower() in {"q", "quit", "exit"}:
                    break
                if question.strip():
                    result = await services.chat.answer(conversation["id"], user["id"], question, args.mode)
                    print(result["answer"])
        elif args.command == "keys-rotate":
            if not args.offline:
                raise SystemExit("轮换必须停止全部应用 worker 后，使用 --offline 明确执行维护")
            kid = await asyncio.to_thread(services.cipher.rotate)
            changed = await asyncio.to_thread(encrypt_database, services.db)
            if services.graph.enabled:
                await services.graph.close()
                await services.graph.start()
            if settings.vector_backend == "milvus":
                chunks = await asyncio.to_thread(services.repository.ready_chunks)
                await asyncio.to_thread(services.store.upsert, chunks)
            if services.cache.redis:
                async for key in services.cache.redis.scan_iter(match="rag-e:*", count=100):
                    await services.cache.redis.delete(key)
            await asyncio.to_thread(services.security.audit, "keys.rotate", kid, details={"rows": changed})
            print(json.dumps({"active_key": kid, "rewritten_rows": changed, "old_keys_retained": True}))
        elif args.command == "keys-status":
            from sqlalchemy import text

            counts = {}
            with services.db.engine.connect() as connection:
                for table, columns in FIELDS.items():
                    for column in columns:
                        for value in connection.execute(
                            text(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")
                        ).scalars():
                            services.cipher.decrypt(value, table + "." + column)
                            key = value.split(":")[1]
                            counts[key] = counts.get(key, 0) + 1
            print(
                json.dumps(
                    {
                        "active_key": services.cipher.active,
                        "retained_keys": list(services.cipher.keys),
                        "sql_ciphertext_counts": counts,
                    }
                )
            )
        elif args.command == "audit-verify":
            print(json.dumps(await asyncio.to_thread(services.security.verify_audit)))
        elif args.command == "graph-rebuild":
            services.graph.require_enabled()
            documents = await asyncio.to_thread(services.repository.documents)
            failures = 0
            for document in documents:
                if document["status"] != "ready" or (args.document_id and document["id"] != args.document_id):
                    continue
                try:
                    print(json.dumps(await services.graph.index(document["id"]), ensure_ascii=False))
                except Exception as exc:
                    failures += 1
                    print(json.dumps({"document_id": document["id"], "error": type(exc).__name__}))
            if failures:
                raise SystemExit(f"{failures} 份文档图谱重建失败")
        elif args.command == "verify":
            await asyncio.to_thread(services.db.healthy)
            await asyncio.to_thread(services.store.healthy)
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "mode": settings.mode,
                        "storage": settings.vector_backend,
                        "stats": await asyncio.to_thread(services.repository.stats),
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        await services.close()


def main():
    parser = argparse.ArgumentParser(description="知恒 · Continuum 运维与数据导入工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("verify")
    worker = sub.add_parser("memory-worker")
    worker.add_argument("--once", action="store_true")
    sub.add_parser("keys-init")
    sub.add_parser("keys-status")
    sub.add_parser("audit-verify")
    rotation = sub.add_parser("keys-rotate")
    rotation.add_argument("--offline", action="store_true")
    rebuild = sub.add_parser("graph-rebuild")
    rebuild.add_argument("--document-id")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("directory", nargs="?", default=str(ROOT / "data" / "documents"))
    user = sub.add_parser("create-user")
    user.add_argument("username")
    user.add_argument("--role", default="user")
    chat = sub.add_parser("chat")
    chat.add_argument("--mode", choices=["adaptive", "agentic"], default="adaptive")
    asyncio.run(execute(parser.parse_args()))


if __name__ == "__main__":
    main()
