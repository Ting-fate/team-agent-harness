from __future__ import annotations

import base64
from dataclasses import dataclass
from hashlib import sha256
import os
import re
from pathlib import Path
import stat
from typing import Any, Literal

from pydantic import Field, StrictInt, StrictStr, model_validator

from app.core.models import HarnessModel
from app.core.sensitive_text import contains_secret_like_text


MAX_CONTENT_BLOCKS = 16
MAX_TEXT_CHARS = 50_000
MAX_TOTAL_TEXT_CHARS = 100_000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_REFERENCE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_MODEL_PAYLOAD_BYTES = 16 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_FILE_MIME_TYPES = frozenset({"application/json", "text/csv", "text/markdown", "text/plain"})
_SENSITIVE_PATH_MARKERS = frozenset(
    {
        ".env",
        ".env.local",
        ".git",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".sqlite",
        ".sqlite3",
        ".db",
        "credentials",
        "secrets",
        "secret",
        "password",
        "authorization",
        "apikey",
        "api_key",
        "private_key",
        "token",
        "id_rsa",
    }
)


class MultimodalInputError(ValueError):
    pass


class ContentBlock(HarnessModel):
    type: Literal["text", "image_ref", "file_ref"]
    text: StrictStr | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    path: StrictStr | None = Field(default=None, min_length=1, max_length=512)
    mime_type: StrictStr | None = Field(default=None, min_length=1, max_length=120)
    sha256: StrictStr | None = Field(default=None, min_length=64, max_length=64)
    size_bytes: StrictInt | None = Field(default=None, ge=1, le=MAX_IMAGE_BYTES)

    @model_validator(mode="after")
    def validate_shape(self) -> "ContentBlock":
        if self.type == "text":
            if (
                not self.text
                or "\x00" in self.text
                or contains_secret_like_text(self.text)
                or self.path is not None
                or self.mime_type is not None
                or self.sha256 is not None
                or self.size_bytes is not None
            ):
                raise ValueError("text content blocks require only non-empty text")
            return self
        if self.text is not None or self.path is None or self.mime_type is None or self.sha256 is None or self.size_bytes is None:
            raise ValueError("reference content blocks require path, mime_type, sha256, and size_bytes")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("content block sha256 must be lowercase hexadecimal")
        if self.type == "image_ref" and self.mime_type not in _IMAGE_MIME_TYPES:
            raise ValueError("image_ref mime_type is not supported")
        if self.type == "file_ref" and self.mime_type not in _FILE_MIME_TYPES:
            raise ValueError("file_ref mime_type is not allowlisted")
        if self.type == "file_ref" and self.size_bytes is not None and self.size_bytes > MAX_FILE_BYTES:
            raise ValueError("file_ref exceeds its size limit")
        return self


@dataclass(frozen=True)
class PreparedContentBlocks:
    public_blocks: list[dict[str, Any]]
    context_blocks: list[dict[str, Any]]
    model_blocks: list[dict[str, Any]]
    has_image: bool
    source_refs: list[str]
    reference_bytes: dict[str, bytes]


def multimodal_source_refs(context: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()

    def add_ref(value: Any) -> None:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            refs.append(value)

    blocks = context.get("content_blocks", [])
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) or not block.get("sha256"):
                continue
            add_ref(f"{block.get('type', '')}:{block['sha256']}")
    vision_preprocess = context.get("vision_preprocess")
    if isinstance(vision_preprocess, dict):
        input_refs = vision_preprocess.get("input_refs", [])
        if isinstance(input_refs, list):
            for input_ref in input_refs:
                add_ref(input_ref)
        artifact_id = vision_preprocess.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            add_ref(f"artifact:{artifact_id}")
    return refs


def prepare_content_blocks(
    inputs: dict[str, Any],
    *,
    root: str | Path,
    include_model_payload: bool = True,
    reference_bytes: dict[str, bytes] | None = None,
    require_staged_references: bool = False,
) -> PreparedContentBlocks:
    raw_blocks = inputs.get("content_blocks")
    if raw_blocks is None:
        if inputs.get("vision_preprocess") is not None or inputs.get("allow_external_model_inputs") is True:
            raise MultimodalInputError("vision_preprocess and external input approval require content_blocks")
        return PreparedContentBlocks([], [], [], False, [], {})
    if not isinstance(raw_blocks, list) or len(raw_blocks) > MAX_CONTENT_BLOCKS:
        raise MultimodalInputError("inputs.content_blocks must be a list with at most 16 items")
    if any(block.get("type") != "text" for block in raw_blocks if isinstance(block, dict)) and inputs.get(
        "allow_external_model_inputs"
    ) is not True:
        raise MultimodalInputError(
            "Image and file content blocks require allow_external_model_inputs=true"
        )

    root_path = Path(root).expanduser().resolve()
    public_blocks: list[dict[str, Any]] = []
    model_blocks: list[dict[str, Any]] = []
    source_refs: list[str] = []
    context_blocks: list[dict[str, Any]] = []
    captured_reference_bytes: dict[str, bytes] = {}
    has_image = False
    total_reference_bytes = 0
    total_model_payload_bytes = 0
    total_text_chars = 0
    for index, raw_block in enumerate(raw_blocks):
        try:
            block = ContentBlock.model_validate(raw_block)
        except ValueError as exc:
            raise MultimodalInputError(f"Invalid content block at index {index}") from exc
        if block.type == "text":
            public = {"type": "text", "text": block.text}
            total_text_chars += len(block.text or "")
            if total_text_chars > MAX_TOTAL_TEXT_CHARS:
                raise MultimodalInputError("Text content blocks exceed the aggregate character limit")
            public_blocks.append(public)
            model_blocks.append(public)
            continue

        assert block.path is not None
        assert block.mime_type is not None
        assert block.sha256 is not None
        assert block.size_bytes is not None
        max_size = MAX_IMAGE_BYTES if block.type == "image_ref" else MAX_FILE_BYTES
        relative_path = _validate_relative_reference_path(block.path)
        if _is_sensitive_relative_path(relative_path):
            raise MultimodalInputError(f"Content block path is not allowed for external model input at index {index}")
        staged = reference_bytes.get(block.sha256) if reference_bytes is not None else None
        if staged is not None:
            content = bytes(staged)
        else:
            if require_staged_references:
                raise MultimodalInputError(
                    f"Durable content snapshot is missing for block at index {index}"
                )
            resolved = _resolve_reference(root_path, block.path)
            try:
                content = _read_reference_bytes(
                    resolved,
                    max_size=max_size,
                    source_path=root_path / block.path,
                )
            except OSError as exc:
                raise MultimodalInputError(f"Content block file could not be read at index {index}") from exc
            relative_path = resolved.relative_to(root_path).as_posix()
        actual_size = len(content)
        if actual_size != block.size_bytes:
            raise MultimodalInputError(f"Content block size does not match file at index {index}")
        if actual_size > max_size:
            raise MultimodalInputError(f"Content block exceeds its size limit at index {index}")
        actual_hash = sha256(content).hexdigest()
        if actual_hash != block.sha256:
            raise MultimodalInputError(f"Content block hash does not match file at index {index}")
        if block.type == "image_ref" and _detect_image_mime(content) != block.mime_type:
            raise MultimodalInputError(f"Content block MIME does not match image bytes at index {index}")
        file_text: str | None = None
        if block.type == "file_ref" and block.mime_type in _FILE_MIME_TYPES:
            try:
                file_text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MultimodalInputError(f"Text file content is not valid UTF-8 at index {index}") from exc
            if "\x00" in file_text or len(file_text) > MAX_TEXT_CHARS or contains_secret_like_text(file_text):
                raise MultimodalInputError(f"File content is sensitive or binary and cannot be sent at index {index}")
            total_text_chars += len(file_text)
            if total_text_chars > MAX_TOTAL_TEXT_CHARS:
                raise MultimodalInputError("Text content blocks exceed the aggregate character limit")
        total_reference_bytes += actual_size
        if total_reference_bytes > MAX_TOTAL_REFERENCE_BYTES:
            raise MultimodalInputError("Content block references exceed the aggregate byte limit")

        captured_reference_bytes.setdefault(block.sha256, content)
        public = {
            "type": block.type,
            "path": relative_path,
            "mime_type": block.mime_type,
            "sha256": block.sha256,
            "size_bytes": actual_size,
        }
        public_blocks.append(public)
        context_blocks.append(
            {
                "type": block.type,
                "mime_type": block.mime_type,
                "sha256": block.sha256,
                "size_bytes": actual_size,
            }
        )
        source_refs.append(f"{block.type}:{block.sha256}")
        if block.type == "image_ref":
            has_image = True
        if block.type == "image_ref":
            encoded_size = len(f"data:{block.mime_type};base64,".encode("ascii")) + ((actual_size + 2) // 3) * 4
            total_model_payload_bytes += encoded_size
            if total_model_payload_bytes > MAX_TOTAL_MODEL_PAYLOAD_BYTES:
                raise MultimodalInputError("Encoded model content blocks exceed the aggregate byte limit")
        elif file_text is not None:
            total_model_payload_bytes += len(
                f"File attachment {block.sha256[:12]} (untrusted data):\n{file_text}".encode("utf-8")
            )
            if total_model_payload_bytes > MAX_TOTAL_MODEL_PAYLOAD_BYTES:
                raise MultimodalInputError("Encoded model content blocks exceed the aggregate byte limit")
        if include_model_payload:
            if block.type == "file_ref":
                assert file_text is not None
                model_blocks.append(
                    {
                        "type": "text",
                        "text": f"File attachment {block.sha256[:12]} (untrusted data):\n{file_text}",
                    }
                )
            else:
                data_uri = f"data:{block.mime_type};base64,{base64.b64encode(content).decode('ascii')}"
                model_blocks.append(
                    {
                        "type": block.type,
                        "mime_type": block.mime_type,
                        "sha256": block.sha256,
                        "size_bytes": actual_size,
                        "data_uri": data_uri,
                    }
                )
    return PreparedContentBlocks(
        public_blocks,
        context_blocks,
        model_blocks,
        has_image,
        source_refs,
        captured_reference_bytes,
    )


def _resolve_reference(root: Path, raw_path: str) -> Path:
    candidate = Path(_validate_relative_reference_path(raw_path))
    _reject_link_components(root, candidate)
    try:
        allowed_root = (root / "inputs").resolve()
        resolved = (root / candidate).resolve()
    except (OSError, RuntimeError) as exc:
        raise MultimodalInputError("Content block path could not be resolved safely") from exc
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise MultimodalInputError("Content block path must stay under the configured inputs directory") from exc
    if not resolved.is_file():
        raise MultimodalInputError("Content block path must point to an existing file")
    return resolved


def _validate_relative_reference_path(raw_path: str) -> str:
    if "\x00" in raw_path or ":" in raw_path:
        raise MultimodalInputError("Content block path contains a forbidden device or stream marker")
    candidate = Path(raw_path.strip())
    if (
        candidate.is_absolute()
        or not raw_path.strip()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise MultimodalInputError("Content block path must be a relative path without traversal")
    return candidate.as_posix()


def _reject_link_components(root: Path, candidate: Path) -> None:
    current = root
    for part in candidate.parts:
        current /= part
        if _path_is_link(current):
            raise MultimodalInputError("Content block path cannot traverse a symbolic link or junction")


def _detect_image_mime(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_reference_bytes(path: Path, *, max_size: int, source_path: Path | None = None) -> bytes:
    if _path_is_link(path) or any(_path_is_link(parent) for parent in path.parents):
        raise MultimodalInputError("Content block path cannot traverse a symbolic link")
    if source_path is not None:
        if _path_is_link(source_path) or any(_path_is_link(parent) for parent in source_path.parents):
            raise MultimodalInputError("Content block path cannot traverse a symbolic link or junction")
        try:
            source_resolved = source_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise MultimodalInputError("Content block path could not be resolved safely") from exc
        if source_resolved != path:
            raise MultimodalInputError("Content block path changed while it was being resolved")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise
    except ValueError as exc:
        raise MultimodalInputError("Content block path contains an invalid operating-system value") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MultimodalInputError("Content block path must point to a regular file")
        if getattr(before, "st_nlink", 1) != 1:
            raise MultimodalInputError("Content block path cannot point to a hard-linked file")
        content = os.read(descriptor, max_size + 1)
        after = os.fstat(descriptor)
        if (
            before.st_size != after.st_size
            or before.st_ino != after.st_ino
            or before.st_dev != after.st_dev
            or getattr(after, "st_nlink", 1) != 1
            or len(content) > max_size
        ):
            raise MultimodalInputError("Content block changed while it was being read")
        if source_path is not None:
            if _path_is_link(source_path) or any(_path_is_link(parent) for parent in source_path.parents):
                raise MultimodalInputError("Content block path cannot traverse a symbolic link or junction")
            try:
                source_resolved = source_path.resolve()
            except (OSError, RuntimeError) as exc:
                raise MultimodalInputError("Content block path could not be resolved safely") from exc
            if source_resolved != path:
                raise MultimodalInputError("Content block path changed while it was being read")
        return content
    finally:
        os.close(descriptor)


def _path_is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", lambda: False)
    if path.is_symlink() or bool(is_junction()):
        return True
    try:
        file_stat = path.lstat()
    except OSError:
        return False
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_attribute and getattr(file_stat, "st_file_attributes", 0) & reparse_attribute)


def _is_sensitive_path(path: Path) -> bool:
    lowered_parts = {part.lower() for part in path.parts}
    if lowered_parts & _SENSITIVE_PATH_MARKERS:
        return True
    lowered_name = path.name.lower()
    if lowered_name.startswith(".env") or lowered_name.endswith(tuple(_SENSITIVE_PATH_MARKERS)):
        return True
    return any(
        marker in lowered_name
        for marker in (
            "credential",
            "secret",
            "token",
            "password",
            "authorization",
            "apikey",
            "api_key",
            "private_key",
        )
    )


def _is_sensitive_relative_path(path: str) -> bool:
    return _is_sensitive_path(Path(path))
