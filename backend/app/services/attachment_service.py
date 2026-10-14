"""Private attachment space. Parsing and storage never call an embedder."""

import asyncio  # 异步并发门控，以及把同步解析/数据库操作卸载到线程。
import base64  # 将原文件和图片字节编码为可放入加密 JSON 的字符串。
import io  # 使用内存字节流，避免写出明文临时文件。
import json  # 估算 JSON payload 的存储大小。
import threading  # 同步线程间互斥，保护非线程安全的 PDFium。
import warnings  # 将 Pillow 的解压炸弹警告提升为解析失败。
import zipfile  # DOCX 本质是 ZIP 包，按内部路径读取正文和图片。
from contextlib import closing  # 为只有 close() 的 PDFium 对象提供自动释放。
from pathlib import PurePosixPath  # 纯路径处理，不访问文件系统。
from xml.etree import ElementTree  # 解析 DOCX 主文档的 XML。

from PIL import Image, ImageOps  # 解码图片、修正 EXIF 方向、缩放与重新编码。
from sqlalchemy import func, select, update  # 构造聚合查询、归属过滤和事务锁操作。

from app.common.errors import AppError  # 对外暴露稳定的错误码及可读提示。
from app.models.entities import Attachment, User  # 附件模型及用于同用户操作串行化的用户行。

PDF_LOCK = threading.Lock()  # PDFium 即使处理不同文档也非线程安全，同一进程的调用需串行。
# 扩展名白名单及下载响应所用 MIME；解析器仍会实际解码文件，不能只相信扩展名。
TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def filename(value):
    # 统一 Windows/Unix 分隔符后只取末级文件名，丢弃客户端携带的目录路径。
    name = PurePosixPath((value or "").replace("\\", "/")).name.strip()
    # 拒绝空名称、超长名称和控制字符，避免显示及响应头出现异常。
    if not name or len(name) > 180 or any(ord(c) < 32 for c in name):
        raise AppError("attachment_name", "附件名称须为 1–180 字符", 422)
    # 返回规范化名称；函数不读取或创建任何磁盘文件。
    return name


def image_data(content):
    # 将警告处理限定在当前上下文中，离开后恢复原过滤规则。
    with warnings.catch_warnings():
        # 高压缩比图片可能占用大量内存，遇到 Pillow 像素警告时直接失败。
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        # 先从内存打开图片头，退出 with 时关闭原图对象。
        with Image.open(io.BytesIO(content)) as original:
            # 使用解码器识别的真实格式，同时拒绝动画/多帧图片。
            if original.format not in {"PNG", "JPEG", "WEBP"} or getattr(original, "n_frames", 1) != 1:
                raise ValueError("unsupported image")
            # 在完整解码前检查像素总量，而不是只限制压缩文件的字节数。
            if original.width * original.height > 20_000_000:
                raise AppError("attachment_image_limit", "图片不得超过 2000 万像素", 413)
            original.load()  # 真正解码像素，损坏文件也会在这里失败。
            # 根据拍摄方向旋转/翻转，再转成视觉请求使用的统一 RGB 格式。
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((1600, 1600))  # 保持比例缩小，不把小图放大。
            out = io.BytesIO()  # 准备用于存储归一化图片的内存缓冲区。
            image.save(out, format="JPEG", quality=85)  # 控制视觉输入大小；原文件另行完整保存。
            # 仅返回 Base64 数据，不带 data: 前缀，前缀由 VisionService 组装。
            return base64.b64encode(out.getvalue()).decode()


def parse_attachment(name, content, settings):
    # 根据规范化文件名选择对应解析路径。
    suffix = PurePosixPath(name).suffix.lower()
    # 白名单之外的文件不进入解析器。
    if suffix not in TYPES:
        raise AppError("attachment_type", "支持 PNG/JPEG/WebP、PDF、DOCX、UTF-8 TXT/Markdown", 415)
    # 同时拒绝空文件与超过上传预算的文件。
    if not content or len(content) > settings.max_upload_bytes:
        raise AppError("attachment_size", "附件为空或超过单文件大小限制", 413)
    text, images = "", []  # 解析结果分为正文和按顺序保存的 JPEG 图片。
    try:
        if suffix in {".txt", ".md", ".markdown"}:
            text = content.decode("utf-8-sig")  # 支持 UTF-8，可自动去除文件开头的 BOM。
            # NUL 通常意味着并非正常文本文件，拒绝当作正文使用。
            if "\x00" in text:
                raise ValueError("binary text")
        elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            # 单张图片不在上传时调用模型，只生成可供后续视觉请求使用的图像。
            images = [image_data(content)]
        elif suffix == ".docx":
            # 只在内存中读取 ZIP 项，不解压到客户端提供的磁盘路径。
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()  # 先读取目录信息，随后检查总解压规模。
                # 同时限制 ZIP 项数和声明的解压总字节，降低压缩炸弹风险。
                if len(entries) > 1500 or sum(i.file_size for i in entries) > 30 * 1024 * 1024:
                    raise AppError("attachment_archive_limit", "Word 解压规模超出限制", 413)
                xml = archive.read("word/document.xml")  # 主文档包括正文段落及表格中的段落。
                # 拒绝可见的 DTD/实体声明；这里并不执行 Office 宏或外部链接。
                if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                    raise ValueError("unsafe XML")
                root = ElementTree.fromstring(xml)  # 构造 XML 树，格式损坏时抛出解析异常。
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"  # Word 正文命名空间。
                # 段落内部拼接 w:t 文本，段落之间换行；此处不完整保留原来的排版结构。
                text = "\n".join("".join(n.text or "" for n in p.iter(ns + "t")) for p in root.iter(ns + "p"))
                # 遍历包中的媒体文件，图片顺序采用 ZIP 目录顺序，不是页面布局定位结果。
                for entry in entries:
                    if entry.filename.startswith("word/media/") and not entry.is_dir():
                        # 非支持的图片格式不静默忽略，提示用户转为 PDF。
                        if PurePosixPath(entry.filename).suffix.lower() not in {
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".webp",
                        }:
                            raise AppError(
                                "attachment_embedded_image", "Word 包含不支持的图片，请转为 PDF 后上传", 422
                            )
                        if len(images) >= settings.attachment_max_images:  # 解码下一张图之前检查数量。
                            raise AppError("attachment_image_limit", "Word 图片数量超出限制", 413)
                        # 将内嵌图片统一归一化为 JPEG Base64。
                        images.append(image_data(archive.read(entry)))
        else:
            import pypdfium2 as pdfium  # 白名单中剩余类型是 PDF，按需加载解析依赖。

            # 持锁执行整个 PDF 解析过程；文档上下文结束时释放底层资源。
            with PDF_LOCK, pdfium.PdfDocument(content) as document:
                # 每一页都会渲染成图片，因此页数使用同一图片数量预算。
                if not 0 < len(document) <= settings.attachment_max_images:
                    raise AppError("attachment_page_limit", "PDF 页数超过附件图片数量上限，请拆分上传", 413)
                parts = []  # 按页积累可直接提取的文字。
                for index in range(len(document)):
                    # PdfPage 只提供 close()，用 closing 确保正常/异常退出都会释放。
                    with closing(document[index]) as page:
                        with closing(page.get_textpage()) as textpage:
                            # 保留页码；扫描页可能没有可提取文字，后续由视觉模型读取页面图。
                            parts.append(f"第 {index + 1} 页\n" + textpage.get_text_range())
                        width, height = page.get_size()  # PDF 页面原始尺寸，用于计算渲染缩放率。
                        if min(width, height) <= 0:
                            raise ValueError("invalid page")
                        # 缩放率最多 2，同时把渲染后的页面长边控制在约 1600 像素。
                        with closing(page.render(scale=min(2, 1600 / max(width, height)))) as bitmap:
                            out = io.BytesIO()  # 页面图片保存在内存，不落明文文件。
                            # 将位图转为 RGB JPEG，便于主流视觉接口直接接收。
                            bitmap.to_pil().convert("RGB").save(out, format="JPEG", quality=85)
                            # 列表顺序与 PDF 页码一致。
                            images.append(base64.b64encode(out.getvalue()).decode())
                text = "\n\n".join(parts)  # 合并各页文字，仍保留页间分隔。
        text = text.strip()  # 去除整份正文前后的空白。
        # 既没有文字又没有图像时，该附件不能提供问答证据。
        if not text and not images:
            raise AppError("attachment_empty", "附件未包含可读取内容", 422)
        # 超出正文预算就拒绝，不悄悄截掉后半部分后仍声称已读取完整文件。
        if len(text) > settings.attachment_max_chars:
            raise AppError("attachment_text_limit", "附件正文过长，请拆分后上传", 413)
    except AppError:
        raise  # 保留主动校验产生的具体业务错误码。
    except Exception as exc:
        # 统一包装编码、图片、ZIP、XML、PDF 底层错误，不对外暴露原始解析异常。
        raise AppError("attachment_parse", "无法解析附件，请检查格式、编码或是否加密", 422) from exc
    # 返回 MIME 和存储 payload；原文件完整保存，问答文本/图片为解析派生结果。
    return TYPES[suffix], {"original": base64.b64encode(content).decode(), "text": text, "images": images}


def summary(row):
    # 只选择允许公开的元数据，不访问 deferred payload，列表请求无需解密大文件内容。
    return {
        key: getattr(row, key)  # 根据白名单字段从 ORM 对象取值。
        for key in (
            "id",  # 附件稳定标识，后续选择/下载/删除均使用此值。
            "filename",  # ORM 字段类型负责解密后的显示名称。
            "media_type",  # 下载响应的内容类型。
            "size",  # 原文件字节数。
            "storage_size",  # 用于空间配额的估算加密存储大小。
            "text_chars",  # 已解析正文字符数。
            "image_count",  # 归一化图片或 PDF 页面数量。
            "created_at",  # 上传时间戳。
        )
    }


class AttachmentService:  # 管理私有附件；不依赖 Embedder、Milvus 或知识图谱服务。
    def __init__(self, settings, database, vision, moderation):
        # 所有服务由 Container 注入，数据库和 HTTP 连接不按上传请求重复创建。
        self.settings, self.db, self.vision, self.moderation = settings, database, vision, moderation
        self.parse_gate = asyncio.Semaphore(2)  # 每个服务实例最多同时进入两个解析/保存流程。

    def _owned(self, session, user_id, attachment_id):
        # 将附件 ID 和 owner_id 一起放入查询条件，不能先查到文件再依赖前端判断归属。
        row = session.scalar(
            select(Attachment).where(Attachment.id == attachment_id, Attachment.owner_id == user_id)
        )
        if row is None:
            # 不区分“文件不存在”和“文件属于别人”，避免泄露其他用户的文件存在性。
            raise AppError("attachment_not_found", "附件不存在或无权访问", 404)
        return row  # 供详情、下载、重命名、删除和问答读取复用。

    def list(self, user_id):
        # 只读会话结束即关闭；调用者从异步路由通过 to_thread 调用。
        with self.db.session() as session:
            rows = session.scalars(
                select(Attachment)  # payload 字段是延迟加载，不在这里读取原文件。
                .where(Attachment.owner_id == user_id)  # 即使管理员也只返回自己的附件。
                .order_by(Attachment.created_at.desc())  # 最近上传的文件排在前面。
            ).all()
            # 同时返回实际占用与服务端限制，供空间页面展示容量和上传提示。
            return {
                "items": [summary(r) for r in rows],  # 附件元数据列表。
                "used_bytes": sum(r.storage_size for r in rows),  # 包括预览及编码开销的估算占用。
                "quota_bytes": self.settings.attachment_space_bytes,  # 每用户空间上限。
                "max_files": self.settings.attachment_max_files,  # 每用户附件数量上限。
                "max_upload_bytes": self.settings.max_upload_bytes,  # 单个原文件大小上限。
                "max_per_message": self.settings.attachment_max_per_message,  # 每轮附件数量上限。
                "max_chars": self.settings.attachment_max_chars,  # 每轮解析正文总量上限。
                "max_images": self.settings.attachment_max_images,  # 每轮图片总量上限。
                "vision_configured": self.vision.configured,  # 仅表示配置是否齐全，不代表连通性通过。
            }

    async def upload(self, user_id, name, content):
        name = filename(name)  # 先规范化名称，再交给解析器。
        # 限制同时进行的文件处理，防止大量解析任务立即占满工作线程。
        async with self.parse_gate:
            # 图片/PDF/XML 解码是同步工作，放到线程中避免阻塞事件循环。
            media, payload = await asyncio.to_thread(parse_attachment, name, content, self.settings)
            # 解析成功后再进行配额检查和事务保存；上传过程不会调用视觉模型或 Embedding。
            return await asyncio.to_thread(self._save, user_id, name, media, content, payload)

    def _save(self, user_id, name, media, content, payload):
        # JSON 内已经含原文/图片 Base64，再估计密文 Base64 的 4/3 扩展及固定额外开销。
        storage_size = len(json.dumps(payload, ensure_ascii=False).encode()) * 4 // 3 + 256
        # 退出事务上下文时统一提交，任何异常都会回滚。
        with self.db.session.begin() as session:
            # 以不改变 token_version 值的 UPDATE 获取写锁，让同用户跨 worker 的配额检查串行。
            # PostgreSQL 锁用户行，SQLite 获取数据库写锁；不是用进程内锁代替数据库一致性。
            session.execute(update(User).where(User.id == user_id).values(token_version=User.token_version))
            # 拿到锁以后再统计，coalesce 把没有任何附件时的 SUM(NULL) 转为 0。
            count, size = session.execute(
                select(func.count(Attachment.id), func.coalesce(func.sum(Attachment.storage_size), 0)).where(
                    Attachment.owner_id == user_id
                )
            ).one()
            # 数量或总存储任一超限均拒绝，避免先落库再发现空间不足。
            if (
                count >= self.settings.attachment_max_files
                or size + storage_size > self.settings.attachment_space_bytes
            ):
                raise AppError("attachment_quota", "空间容量或附件数量已满，请先删除不需要的附件", 413)
            # 构造 ORM 记录；filename/payload 的字段类型会在写 SQL 时自动加密。
            row = Attachment(
                owner_id=user_id,  # 归属来自已认证用户。
                filename=name,  # 规范化后的显示文件名。
                media_type=media,  # 解析分支选出的 MIME。
                size=len(content),  # 原文件大小。
                storage_size=storage_size,  # 用于空间容量统计。
                text_chars=len(payload["text"]),  # 后续选附件时不必解密即可检查正文预算。
                image_count=len(payload["images"]),  # 后续选附件时检查视觉输入预算。
                payload=payload,  # 原文件、解析文本和图片一起进入加密字段。
            )
            session.add(row)  # 将新记录纳入当前事务。
            session.flush()  # 执行 INSERT 并取得默认 ID/创建时间，但此时尚未离开事务。
            return summary(row)  # 只返回摘要；离开 with 后提交成功，调用者才收到结果。

    def detail(self, user_id, attachment_id):
        with self.db.session() as session:
            row = self._owned(session, user_id, attachment_id)  # 先核验归属，不能通过详情接口越权。
            # 访问 payload 时延迟加载并解密，只返回预览文本，不把原文件 Base64 发给列表页面。
            return {**summary(row), "text": row.payload["text"]}

    def download(self, user_id, attachment_id):
        with self.db.session() as session:
            row = self._owned(session, user_id, attachment_id)  # 下载与预览采用相同的本人校验。
            # 解密 payload 后还原原文件字节；API 层再设置下载文件名和 no-store 响应头。
            return summary(row), base64.b64decode(row.payload["original"])

    def rename(self, user_id, attachment_id, name):
        name = filename(name)  # 重命名也必须遵循长度、路径和控制字符限制。
        with self.db.session.begin() as session:
            row = self._owned(session, user_id, attachment_id)  # 只允许修改自己的附件。
            # 文件正文没有重新解析，因此不能通过改扩展名伪装文件格式。
            if PurePosixPath(name).suffix.lower() != PurePosixPath(row.filename).suffix.lower():
                raise AppError("attachment_extension", "重命名时请保留原文件扩展名", 422)
            row.filename = name  # 修改 ORM 字段，提交时重新加密该名称。
            # 其他元数据和原文件内容均不改变。
            return summary(row)

    def delete(self, user_id, attachment_id):
        with self.db.session.begin() as session:
            # 与上传配额检查、问答提交使用相同的用户写锁，协调并发删除与使用。
            session.execute(update(User).where(User.id == user_id).values(token_version=User.token_version))
            # 删除数据库记录和其加密 payload；历史消息的隐藏由 Repository.history 负责。
            session.delete(self._owned(session, user_id, attachment_id))

    def selection(self, session, user_id, ids):
        # 保存会话选择和读取正文共用校验，仅访问摘要列，不加载大 payload。
        # 重复 ID 不默默去重，明确拒绝，避免前端计数与实际模型输入不一致。
        if len(ids) != len(set(ids)) or len(ids) > self.settings.attachment_max_per_message:
            raise AppError("attachment_count", "附件数量超过限制或包含重复附件", 422)
        # 按选择顺序逐个检查本人归属；任意失效附件都会停止本次操作。
        rows = [self._owned(session, user_id, aid) for aid in ids]
        if (
            sum(r.text_chars for r in rows) > self.settings.attachment_max_chars
            or sum(r.image_count for r in rows) > self.settings.attachment_max_images
        ):
            raise AppError("attachment_context_limit", "本轮附件总文本或图片数量超出限制，请减少附件", 413)
        return rows

    def selection_status(self, session, user_id, ids):
        # 恢复界面时不因文件已删除而让整个会话打不开，也不泄露其他用户的文件信息。
        rows = {
            r.id: r
            for r in session.scalars(
                select(Attachment).where(Attachment.owner_id == user_id, Attachment.id.in_(ids))
            )
        }
        return {
            "attachments": [summary(rows[aid]) for aid in ids if aid in rows],
            "unavailable_attachment_ids": [aid for aid in ids if aid not in rows],
        }

    def load(self, user_id, ids):
        with self.db.session() as session:
            # 即使选择此前已保存，每轮也要重新检查存在性、归属和最新预算。
            rows = self.selection(session, user_id, ids)
            # 在 session 关闭前完成 payload 延迟加载/解密，返回普通字典供异步代码使用。
            return [{**summary(r), "payload": r.payload} for r in rows]

    async def sources(self, user_id, ids, query):
        # 数据库读取及解密在线程中完成，避免事件循环处理大 payload。
        rows = await asyncio.to_thread(self.load, user_id, ids)
        sources = []  # 最终采用与知识库/Web 相同的来源结构。
        for row in rows:
            body = row["payload"]["text"]  # 上传时解析得到的正文。
            if body:
                # 文本先过审，再决定是否调用后续视觉或生成模型。
                await self.moderation.guard(body, "source")
            if row["payload"]["images"]:
                # 视觉识别是带不确定性的证据；图片里的指令不能改变系统规则。
                visual = await self.vision.describe(row["payload"]["images"], query)
                # 视觉服务已检查图片风险，识别出的文字仍通过现有文本审查。
                await self.moderation.guard(visual, "source")
                # 将视觉证据追加到正文，同时明确提醒后续模型存在识别误差。
                body += "\n\n视觉识别（可能存在识别误差）：\n" + visual
            # 每个附件形成一条来源，不拆分、不向量化、不写知识库索引。
            sources.append(
                {
                    "id": "attachment-" + row["id"],  # 与 chunk 和 web 来源使用不同前缀。
                    "text": body,  # 正文及审查后的视觉事实。
                    "score": 1.0,  # 用户主动选择的附件占位分数，不是相似度或置信度。
                    "metadata": {
                        "attachment_id": row["id"],  # 提交复核和受保护下载使用真实附件 ID。
                        "filename": row["filename"],  # 引用面板显示名称。
                        "title": row["filename"],  # 与现有来源标题协议兼容。
                        "category": "attachment",  # 标识来自私人附件空间。
                    },
                }
            )
        if sources:
            # 再检查组合来源及文件名等元数据，防止跨片段风险或元数据绕过审查。
            await self.moderation.guard_sources(sources)
        # 一并返回附件摘要供历史显示，但不携带原始文件或图片 Base64。
        return sources, [{k: v for k, v in row.items() if k != "payload"} for row in rows]
