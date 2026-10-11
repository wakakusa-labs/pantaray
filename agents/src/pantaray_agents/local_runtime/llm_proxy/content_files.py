from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pantaray_agents.local_runtime.runtime.local_image_store import (
    read_local_image_blob,
)

LLM_FILE_INPUT_MAX_BYTES = 20 * 1024 * 1024
type MultipartFile = tuple[str, tuple[str, bytes, str]]
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# What opening a path component reports once the file it named is gone or
# replaced: missing (ENOENT), a file where a directory was (ENOTDIR), or a link
# that O_NOFOLLOW refuses (ELOOP). A directory or a FIFO in the file's place
# opens and is caught by its mode instead.
_GONE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


class FileInputAccessError(RuntimeError):
    """Raised when an LLM file input cannot be opened without crossing its trust boundary."""


class WorkspaceFileChangedError(RuntimeError):
    """A workspace file input no longer holds the bytes recorded for it.

    History written before ``read`` kept its own copy of an image names the
    workspace file itself, which anything may have rewritten or removed since.
    Only a safe path reaches this: an unsafe one is a ``FileInputAccessError``.
    """

    def __init__(self, *, application_ref: str, path: Path) -> None:
        super().__init__(
            "LLM workspace file input changed or was removed after it was "
            f"read: {application_ref}"
        )
        self.application_ref = application_ref
        self.path = path


@dataclass(frozen=True, slots=True)
class PreparedContentFile:
    blob_ref: str
    application_ref: str | None
    payload: bytes
    mime_type: str
    byte_size: int
    sha256: str


def prepare_content_file(
    *,
    file_data: Mapping[str, object],
    user_id: str,
) -> PreparedContentFile:
    blob_ref = _coerce_non_empty_string(
        file_data.get("blob_ref"),
        field_name="file_data.blob_ref",
    )
    application_ref = _coerce_non_empty_string(
        file_data.get("application_ref"),
        field_name="file_data.application_ref",
    )
    mime_type = _coerce_non_empty_string(
        file_data.get("mime_type"),
        field_name="file_data.mime_type",
    )
    expected_byte_size = _coerce_positive_int(
        file_data.get("byte_size"),
        field_name="file_data.byte_size",
    )
    expected_sha256 = _coerce_non_empty_string(
        file_data.get("sha256"),
        field_name="file_data.sha256",
    )
    _reject_file_input_over_limit(byte_size=expected_byte_size, label=blob_ref)

    source_kind = file_data.get("source_kind")
    if source_kind == "workspace_file":
        workspace_root_path = _coerce_non_empty_string(
            file_data.get("workspace_root_path"),
            field_name="file_data.workspace_root_path",
        )
        workspace_relative_path = _coerce_non_empty_string(
            file_data.get("workspace_relative_path"),
            field_name="file_data.workspace_relative_path",
        )
        payload = _read_workspace_file_input(
            root_path=Path(workspace_root_path),
            root_relative_path=workspace_relative_path,
            application_ref=application_ref,
            expected_byte_size=expected_byte_size,
            expected_sha256=expected_sha256,
        )
    elif source_kind is None:
        blob_path = _coerce_non_empty_string(
            file_data.get("blob_path"),
            field_name="file_data.blob_path",
        )
        payload = _read_blob_file_input(
            path=Path(blob_path),
            label=blob_ref,
            expected_byte_size=expected_byte_size,
        )
        _require_sha256(payload, expected=expected_sha256, label=blob_ref)
    elif source_kind == "local_image_blob":
        storage_path = _coerce_non_empty_string(
            file_data.get("storage_path"),
            field_name="file_data.storage_path",
        )
        payload = _read_local_image_file_input(
            user_id=user_id,
            storage_path=storage_path,
            label=blob_ref,
            mime_type=mime_type,
            expected_byte_size=expected_byte_size,
        )
        _require_sha256(payload, expected=expected_sha256, label=blob_ref)
    else:
        raise RuntimeError(f"Unsupported LLM file source_kind: {source_kind}")
    return PreparedContentFile(
        blob_ref=blob_ref,
        application_ref=application_ref,
        payload=payload,
        mime_type=mime_type,
        byte_size=expected_byte_size,
        sha256=expected_sha256,
    )


def _require_sha256(payload: bytes, *, expected: str, label: str) -> None:
    if hashlib.sha256(payload).hexdigest() != expected:
        raise RuntimeError(f"LLM file input sha256 mismatch: {label}")


def _reject_file_input_over_limit(*, byte_size: int, label: str) -> None:
    if byte_size > LLM_FILE_INPUT_MAX_BYTES:
        raise RuntimeError(f"LLM file input is too large: {label}")


def _read_workspace_file_input(
    *,
    root_path: Path,
    root_relative_path: str,
    application_ref: str,
    expected_byte_size: int,
    expected_sha256: str,
) -> bytes:
    components = _validated_relative_components(root_relative_path)
    changed = WorkspaceFileChangedError(
        application_ref=application_ref, path=root_path.joinpath(*components)
    )
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = _open_absolute_directory(root_path)
        for component in components[:-1]:
            next_fd = os.open(
                component,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            components[-1],
            _FILE_OPEN_FLAGS,
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(file_fd)
        # A size that differs needs no read to tell the file changed.
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size != expected_byte_size
        ):
            raise changed
        payload = _read_bounded_file_descriptor(file_fd)
    except OSError as exc:
        if exc.errno in _GONE_ERRNOS:
            raise changed from exc
        # Permissions, descriptors or the disk: the file may be unchanged, so
        # the turn fails and can be retried rather than lose the image.
        raise FileInputAccessError(
            f"LLM workspace file input could not be read: {application_ref}"
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)
    if (
        len(payload) != expected_byte_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise changed
    return payload


def _read_local_image_file_input(
    *,
    user_id: str,
    storage_path: str,
    label: str,
    mime_type: str,
    expected_byte_size: int,
) -> bytes:
    """Read a user-scoped image by its logical storage_path, never a caller path."""

    try:
        blob = read_local_image_blob(user_id=user_id, storage_path=storage_path)
    except ValueError as exc:
        raise FileInputAccessError(
            f"LLM local image input has an invalid storage_path: {label}"
        ) from exc
    if blob is None:
        raise FileInputAccessError(f"LLM local image input was not found: {label}")
    if blob.mime_type != mime_type:
        raise RuntimeError(f"LLM file input mime_type mismatch: {label}")
    payload = blob.payload
    _reject_file_input_over_limit(byte_size=len(payload), label=label)
    if len(payload) != expected_byte_size:
        raise RuntimeError(f"LLM file input byte_size mismatch: {label}")
    return payload


def _read_blob_file_input(
    *,
    path: Path,
    label: str,
    expected_byte_size: int,
) -> bytes:
    try:
        file_fd = os.open(path, _FILE_OPEN_FLAGS)
    except OSError as exc:
        raise FileInputAccessError(
            f"LLM blob file input could not be securely opened: {label}"
        ) from exc
    try:
        return _read_regular_file_descriptor(
            file_fd=file_fd,
            label=label,
            expected_byte_size=expected_byte_size,
        )
    except OSError as exc:
        raise FileInputAccessError(
            f"LLM blob file input could not be securely read: {label}"
        ) from exc
    finally:
        os.close(file_fd)


def _open_absolute_directory(path: Path) -> int:
    if not path.is_absolute() or path.anchor != os.sep:
        raise FileInputAccessError("LLM workspace root must be an absolute POSIX path")
    if any(component in {".", ".."} or "\0" in component for component in path.parts):
        raise FileInputAccessError("LLM workspace root path is not canonical")
    directory_fd = os.open(os.sep, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(
                component,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
    except OSError:
        os.close(directory_fd)
        raise
    return directory_fd


def _validated_relative_components(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or path.parts == (".",):
        raise FileInputAccessError(
            "LLM workspace file input must use a root-relative file path"
        )
    if any(
        component in {"", ".", ".."} or "\0" in component for component in path.parts
    ):
        raise FileInputAccessError(
            "LLM workspace file input contains an unsafe path component"
        )
    return path.parts


def _read_regular_file_descriptor(
    *,
    file_fd: int,
    label: str,
    expected_byte_size: int,
) -> bytes:
    file_stat = os.fstat(file_fd)
    if not stat.S_ISREG(file_stat.st_mode):
        raise FileInputAccessError(f"LLM file input is not a regular file: {label}")
    _reject_file_input_over_limit(byte_size=file_stat.st_size, label=label)
    if file_stat.st_size != expected_byte_size:
        raise RuntimeError(f"LLM file input byte_size mismatch: {label}")
    payload = _read_bounded_file_descriptor(file_fd)
    _reject_file_input_over_limit(byte_size=len(payload), label=label)
    if len(payload) != expected_byte_size:
        raise RuntimeError(f"LLM file input byte_size mismatch: {label}")
    return payload


def _read_bounded_file_descriptor(file_fd: int) -> bytes:
    remaining = LLM_FILE_INPUT_MAX_BYTES + 1
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = os.read(file_fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _coerce_non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(f"{field_name} must not be empty")
    return normalized


def _coerce_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{field_name} must be an integer")
    if value <= 0:
        raise RuntimeError(f"{field_name} must be positive")
    return value


__all__ = [
    "FileInputAccessError",
    "MultipartFile",
    "PreparedContentFile",
    "WorkspaceFileChangedError",
    "prepare_content_file",
]
