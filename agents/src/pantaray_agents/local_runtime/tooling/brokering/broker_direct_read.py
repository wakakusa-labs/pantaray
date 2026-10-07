from __future__ import annotations

import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.security.image_media_types import IMAGE_MIME_TYPES

from ..action_session_temp_paths import SCRATCH_SESSION_TEMP_DIRNAME
from .attachment_reference import build_workspace_file_attachment
from .broker_common import BrokerPolicyError
from .broker_direct_read_document import (
    document_format,
    read_document,
    reject_legacy_document,
)
from .broker_direct_read_page import (
    DEFAULT_READ_LIMIT,
    bound_text_page,
    text_page_output,
)
from .broker_direct_read_text import read_text_descriptor_lines
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_protocol import ValidatedReadRequest
from .read_path_resolver import (
    ReadTarget,
    action_reference_paths,
    resolve_read_target,
)
from .read_scope import ReadScope
from .workspace_descriptor_access import open_workspace_entry_descriptor

SAMPLE_BYTES = 4_096
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
READ_BINARY_FILE_UNSUPPORTED = "READ_BINARY_FILE_UNSUPPORTED"
READ_NOT_A_REGULAR_FILE = "READ_NOT_A_REGULAR_FILE"
READ_ATTACHMENT_TOO_LARGE = "READ_ATTACHMENT_TOO_LARGE"
READ_START_UNIT_UNSUPPORTED = "READ_START_UNIT_UNSUPPORTED"
READ_DIRECTORY_PAGE_LIMIT_RETRY_HINT = "Continue with offset=next_offset."

_BINARY_EXTENSIONS = frozenset(
    {
        ".7z",
        ".a",
        ".bin",
        ".class",
        ".dat",
        ".dll",
        ".exe",
        ".gz",
        ".jar",
        ".lib",
        ".o",
        ".obj",
        ".pyc",
        ".pyo",
        ".so",
        ".tar",
        ".war",
        ".wasm",
        ".zip",
    }
)


@dataclass(frozen=True, slots=True)
class DirectoryReadResult:
    entries: list[dict[str, JSONValue]]
    next_offset: int | None
    truncated: bool
    truncation_reason: str | None
    retry_hint: str | None
    warning: str | None


def run_read_executor(
    *,
    scope: ReadScope,
    request: ValidatedReadRequest,
) -> UnprojectedBrokerToolOutcome:
    target = resolve_read_target(scope=scope, raw_path=request.path)
    descriptor = open_read_target(target)
    try:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            return _read_directory(
                scope=scope,
                target=target,
                request=request,
                descriptor=descriptor,
            )
        return _read_file(target=target, request=request, descriptor=descriptor)
    finally:
        os.close(descriptor)


def open_read_target(target: ReadTarget) -> int:
    """Open the checked target once, walking down from its root.

    Every component below the root is opened without following a symlink, so
    a directory swapped for a link after the check cannot redirect the read;
    every branch reads this one descriptor instead of the path name again.
    """

    return open_workspace_entry_descriptor(
        root_path=target.canonical_root_path,
        relative_path=target.root_relative_path,
    )


def _read_file(
    *,
    target: ReadTarget,
    request: ValidatedReadRequest,
    descriptor: int,
) -> UnprojectedBrokerToolOutcome:
    sample = read_leading_bytes(descriptor, target=target, limit=SAMPLE_BYTES)
    mime_type = _sniff_image_mime(filepath=target.real_path, sample=sample)
    if mime_type is not None:
        _reject_start_unit(request)
        byte_size = os.fstat(descriptor).st_size
        if byte_size > MAX_ATTACHMENT_BYTES:
            raise BrokerPolicyError(
                (
                    f"Attachment is too large to read: {target.display_path} "
                    f"({byte_size} bytes > {MAX_ATTACHMENT_BYTES} bytes)"
                ),
                code=READ_ATTACHMENT_TOO_LARGE,
            )
        payload = _read_attachment_payload(target=target, descriptor=descriptor)
        workspace_attachment = build_workspace_file_attachment(
            canonical_root_path=target.canonical_root_path,
            root_relative_path=target.root_relative_path,
            display_path=target.display_path,
            mime_type=mime_type,
            payload=payload,
        )
        return UnprojectedBrokerToolOutcome(
            status="success",
            output={
                "kind": "attachment",
                "path": target.display_path,
                "mime_type": mime_type,
                "message": "Image read successfully",
                "attachments": [
                    {
                        "type": "file",
                        "mime_type": mime_type,
                        "path": target.display_path,
                        "ref": workspace_attachment.ref,
                        "byte_size": workspace_attachment.byte_size,
                    }
                ],
            },
            search_text=None,
            file_paths=(target.display_path,),
            file_reference_paths=action_reference_paths(target),
            attachments=(
                {
                    "type": "file",
                    "source_kind": "workspace_file",
                    "ref": workspace_attachment.ref,
                    "blob_ref": workspace_attachment.blob_ref,
                    "display_path": target.display_path,
                    "workspace_root_path": str(
                        workspace_attachment.canonical_root_path
                    ),
                    "workspace_relative_path": (
                        workspace_attachment.root_relative_path
                    ),
                    "mime_type": mime_type,
                    "byte_size": workspace_attachment.byte_size,
                    "sha256": workspace_attachment.sha256,
                },
            ),
        )
    reject_legacy_document(target)
    extracted_format = document_format(filepath=target.real_path, sample=sample)
    if extracted_format is not None:
        return read_document(
            target=target,
            request=request,
            document_format=extracted_format,
            descriptor=descriptor,
        )
    if _is_binary_file(filepath=target.real_path, sample=sample):
        raise BrokerPolicyError(
            f"Cannot read binary file: {target.display_path}",
            code=READ_BINARY_FILE_UNSUPPORTED,
        )
    _reject_start_unit(request)

    offset = request.offset or 1
    column = request.column or 1
    limit = request.limit or DEFAULT_READ_LIMIT
    result = read_text_descriptor_lines(
        descriptor=descriptor,
        offset=offset,
        column=column,
        limit=limit,
    )
    output = bound_text_page(
        text_page_output(
            kind="file",
            path=target.display_path,
            offset=offset,
            column=column,
            result=result,
        ),
        content=result.content,
        offset=offset,
        column=column,
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=output,
        search_text=cast(str, output["content"]),
        file_paths=(target.display_path,),
        file_reference_paths=action_reference_paths(target),
    )


def _read_directory(
    *,
    scope: ReadScope,
    target: ReadTarget,
    request: ValidatedReadRequest,
    descriptor: int,
) -> UnprojectedBrokerToolOutcome:
    if request.column not in {None, 1}:
        raise BrokerPolicyError("read.column is valid only for text files")
    _reject_start_unit(request)
    offset = request.offset or 1
    limit = request.limit or DEFAULT_READ_LIMIT
    result = _read_bounded_directory_entries(
        scope=scope,
        target=target,
        descriptor=descriptor,
        offset=offset,
        limit=limit,
    )
    search_text = "\n".join(
        f"{entry['name']}/" if entry["kind"] == "directory" else str(entry["name"])
        for entry in result.entries
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output={
            "kind": "directory",
            "path": target.display_path,
            "entries": cast(JSONValue, result.entries),
            "offset": offset,
            "next_offset": result.next_offset,
            "truncated": result.truncated,
            "truncation_reason": result.truncation_reason,
            "retry_hint": result.retry_hint,
            "warning": result.warning,
        },
        search_text=search_text,
        file_paths=(target.display_path,),
        file_reference_paths=(),
    )


def _read_bounded_directory_entries(
    *,
    scope: ReadScope,
    target: ReadTarget,
    descriptor: int,
    offset: int,
    limit: int,
) -> DirectoryReadResult:
    """One page of entries, from the offset-th entry in the directory's order.

    offset counts every entry, shown or skipped, so a page only checks its own
    entries and offset reaches any entry of a directory left unchanged.
    """

    entries: list[dict[str, JSONValue]] = []
    skipped_symlinks = 0
    unreadable = 0
    first_error: str | None = None
    session_temp = scope.scratch_root_path / SCRATCH_SESSION_TEMP_DIRNAME
    next_offset: int | None = None
    with os.scandir(descriptor) as iterator:
        for index, child in enumerate(iterator, start=1):
            if index < offset:
                continue
            if len(entries) >= limit:
                next_offset = index
                break
            child_path = target.real_path / child.name
            try:
                if child_path == session_temp or scope.hides(child_path):
                    continue
                if child.is_symlink() and not target.allow_symlink_directory_entries:
                    skipped_symlinks += 1
                    continue
                kind = "directory" if child.is_dir() else "file"
            except OSError as exc:
                unreadable += 1
                first_error = first_error or f"{child.name}: {exc.strerror or exc}"
                continue
            entries.append({"name": child.name, "kind": kind})
    warnings: list[str] = []
    if skipped_symlinks:
        warnings.append(
            f"This page skipped {skipped_symlinks} symlink(s): symlinks are listed "
            "only with full read access."
        )
    if unreadable:
        warnings.append(
            f"This page skipped {unreadable} entr(y/ies) that could not be read. "
            f"First error: {first_error}."
        )
    return DirectoryReadResult(
        entries=entries,
        next_offset=next_offset,
        truncated=next_offset is not None,
        truncation_reason="page_limit" if next_offset is not None else None,
        retry_hint=READ_DIRECTORY_PAGE_LIMIT_RETRY_HINT if next_offset else None,
        warning=" ".join(warnings) or None,
    )


def _reject_start_unit(request: ValidatedReadRequest) -> None:
    """Refuse a unit cursor on a target that has no units to count.

    Only an extracted document numbers what it returns in pages, sheets, slides
    or cells; reading a text file, an image or a directory from a unit would
    silently ignore the argument the caller meant to steer the read with.
    """

    if request.start_unit is None:
        return
    raise BrokerPolicyError(
        "read.start_unit is valid only for a PDF, .docx, .xlsx, .pptx or "
        ".ipynb document",
        code=READ_START_UNIT_UNSUPPORTED,
        fix_hint=(
            "Read this path again without start_unit, and use offset to "
            "continue from an earlier result."
        ),
    )


def read_leading_bytes(descriptor: int, *, target: ReadTarget, limit: int) -> bytes:
    """The first ``limit`` bytes of a regular file, left rewound for the next read.

    Every read of a file starts here, before its kind is known. The target was
    opened non-blocking, so a FIFO with no writer is refused here instead of
    blocking the one Action worker on a thread nothing can stop.
    """

    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise BrokerPolicyError(
            f"Cannot read {target.display_path}: it is not a regular file",
            code=READ_NOT_A_REGULAR_FILE,
        )
    with open(descriptor, "rb", closefd=False) as handle:
        payload = handle.read(limit)
        handle.seek(0)
    return payload


def _sniff_image_mime(*, filepath: Path, sample: bytes) -> str | None:
    """The media type of an image, which is the only file returned as itself.

    Every other binary this tool reads is turned into text, so an image is the
    one file whose bytes have to reach the model to be understood at all.
    """

    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if sample.startswith(b"GIF87a") or sample.startswith(b"GIF89a"):
        return "image/gif"
    if sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
        return "image/webp"
    guessed, _encoding = mimetypes.guess_type(filepath.name)
    return guessed if guessed in IMAGE_MIME_TYPES else None


def _read_attachment_payload(*, target: ReadTarget, descriptor: int) -> bytes:
    payload = read_leading_bytes(
        descriptor, target=target, limit=MAX_ATTACHMENT_BYTES + 1
    )
    if len(payload) > MAX_ATTACHMENT_BYTES:
        raise BrokerPolicyError(
            (
                f"Attachment is too large to read: {target.display_path} "
                f"(> {MAX_ATTACHMENT_BYTES} bytes)"
            ),
            code=READ_ATTACHMENT_TOO_LARGE,
        )
    return payload


def _is_binary_file(*, filepath: Path, sample: bytes) -> bool:
    if filepath.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    if not sample:
        return False
    non_printable = 0
    for byte in sample:
        if byte == 0:
            return True
        if byte < 9 or (byte > 13 and byte < 32):
            non_printable += 1
    return non_printable / len(sample) > 0.3


__all__ = [
    "SAMPLE_BYTES",
    "open_read_target",
    "read_leading_bytes",
    "run_read_executor",
]
