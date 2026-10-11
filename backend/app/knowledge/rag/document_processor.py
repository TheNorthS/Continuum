import asyncio
import logging
import re

import httpx

from app.common.errors import AppError


def embedding_input_too_long(exc):
    """Only a provider's explicit per-input token limit permits length fallback."""
    cause = exc.__cause__
    if exc.code != "upstream_error" or not isinstance(cause, httpx.HTTPStatusError):
        return False
    if cause.response.status_code != 400:
        return False
    try:
        error = cause.response.json().get("error", {})
        message = error.get("message", "") if isinstance(error, dict) else ""
        return isinstance(message, str) and bool(
            re.search(r"maximum input length is \d+ tokens", message, re.I)
        )
    except (ValueError, AttributeError):
        return False


def sections(text: str, filename: str) -> list[tuple[str, int, str]]:
    """Keep heading ancestry; fenced code headings are never interpreted as headings."""
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    if text.startswith("---\n"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            text = parts[1].lstrip("\n")
    headings, body, output = [], [], []
    fence = None

    def flush():
        content = "\n".join(body).strip()
        if content:
            output.append(
                (" > ".join(h[1] for h in headings) or filename, headings[-1][0] if headings else 0, content)
            )
        body.clear()

    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            value = marker[1]
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            body.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if fence is None else None
        if heading:
            flush()
            level = len(heading[1])
            headings = [h for h in headings if h[0] < level] + [(level, heading[2])]
        else:
            body.append(line)
    flush()
    return output


class DocumentProcessor:
    def __init__(self, settings, embedder):
        self.settings = settings
        self.embedder = embedder

    def _bounded(self, text):
        size, overlap = self.settings.chunk_size, self.settings.chunk_overlap
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            if end < len(text):
                boundary = max(
                    text.rfind("\n", start + size // 2, end),
                    text.rfind("。", start + size // 2, end),
                    text.rfind(". ", start + size // 2, end),
                )
                if boundary >= 0:
                    end = boundary + 1
            value = text[start:end].strip()
            if value:
                yield value
            if end == len(text):
                break
            start = max(start + 1, end - overlap)

    async def _semantic(self, text):
        if not self.settings.semantic_chunking or len(text) <= self.settings.chunk_size:
            return await asyncio.to_thread(lambda: list(self._bounded(text)))
        sentences = await asyncio.to_thread(
            lambda: [s.strip() for s in re.split(r"(?<=[。！？.!?])\s*|\n\s*\n", text) if s.strip()]
        )
        # Bound pathological inputs before sending anything to an embedding service.
        if len(sentences) < 2 or len(sentences) > 512:
            return await asyncio.to_thread(lambda: list(self._bounded(text)))
        try:
            vectors = await self.embedder.embed(sentences)
        except AppError as exc:
            if not embedding_input_too_long(exc):
                raise
            # Preserve the entire source using the same configured size/overlap
            # fallback used for pathological sentence counts. Never truncate text
            # or hide authentication, quota, network or unrelated provider errors.
            logging.getLogger(__name__).warning("semantic_input_too_long_using_length_chunks")
            return await asyncio.to_thread(lambda: list(self._bounded(text)))
        return await asyncio.to_thread(self._semantic_groups, sentences, vectors)

    def _semantic_groups(self, sentences, vectors):
        distances = [
            1 - sum(a * b for a, b in zip(vectors[i], vectors[i + 1], strict=True))
            for i in range(len(vectors) - 1)
        ]
        threshold = sorted(distances)[int((len(distances) - 1) * 0.85)]
        groups, current = [], []
        for index, sentence in enumerate(sentences):
            current.append(sentence)
            if index == len(sentences) - 1 or distances[index] >= threshold:
                groups.extend(self._bounded("\n".join(current)))
                current = []
        return groups

    async def parse(self, filename: str, content: bytes) -> list[dict]:
        if not filename.lower().endswith((".md", ".markdown", ".txt")):
            raise AppError("unsupported_file", "仅支持 UTF-8 Markdown 和 TXT 文件", 415)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AppError("invalid_encoding", "文件必须使用 UTF-8 编码", 422) from exc
        if "\x00" in text:
            raise AppError("invalid_file", "文件包含二进制内容", 422)
        chunks = []
        for title, depth, body in await asyncio.to_thread(sections, text, filename):
            for part in await self._semantic(body):
                chunks.append(
                    {
                        "text": part,
                        "meta": {
                            "title": title[:500],
                            "category_depth": depth,
                            "filename": filename,
                            "source": filename,
                            "category": "content",
                            "filetype": filename.rsplit(".", 1)[-1].lower(),
                            "chunk_index": len(chunks),
                        },
                    }
                )
                if len(chunks) > self.settings.max_chunks:
                    raise AppError("too_many_chunks", "文档分块过多，请拆分文件", 413)
        if not chunks:
            raise AppError("empty_document", "文档没有可索引的正文", 422)
        return chunks
