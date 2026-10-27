"""Atomic file writes below an already authorized directory descriptor."""

from __future__ import annotations

import os
import uuid
from pathlib import Path, PurePosixPath

from .descriptor_access import (
    DescriptorPathMissingError,
    open_directory_at_descriptor,
    open_directory_descriptor,
)

FILE_WRITE_TEMP_PREFIX = ".pantaray-patch-"


class CommittedFileWriteError(OSError):
    """The file changed, but its directory sync or temporary cleanup failed."""


def open_writable_parent_descriptor(
    *, root_path: Path, relative_path: str, create_missing: bool
) -> tuple[int, str]:
    descriptor = open_directory_descriptor(root_path=root_path)
    try:
        return open_writable_parent_at_descriptor(
            root_descriptor=descriptor,
            relative_path=relative_path,
            create_missing=create_missing,
        )
    finally:
        os.close(descriptor)


def open_writable_parent_at_descriptor(
    *, root_descriptor: int, relative_path: str, create_missing: bool
) -> tuple[int, str]:
    components = PurePosixPath(relative_path).parts
    descriptor = open_directory_at_descriptor(
        parent_descriptor=root_descriptor, relative_path="."
    )
    try:
        for component in components[:-1]:
            try:
                child = open_directory_at_descriptor(
                    parent_descriptor=descriptor,
                    relative_path=component,
                )
            except DescriptorPathMissingError:
                if not create_missing:
                    raise
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
                os.fsync(descriptor)
                child = open_directory_at_descriptor(
                    parent_descriptor=descriptor,
                    relative_path=component,
                )
            os.close(descriptor)
            descriptor = child
        return descriptor, components[-1]
    except BaseException:
        os.close(descriptor)
        raise


def write_text_at_descriptor(
    *,
    parent_descriptor: int,
    destination_name: str,
    text: str,
    replacement_mode: int | None,
    exclusive: bool,
) -> None:
    write_bytes_at_descriptor(
        parent_descriptor=parent_descriptor,
        destination_name=destination_name,
        data=text.encode("utf-8"),
        replacement_mode=replacement_mode,
        exclusive=exclusive,
    )


def write_bytes_at_descriptor(
    *,
    parent_descriptor: int,
    destination_name: str,
    data: bytes,
    replacement_mode: int | None,
    exclusive: bool,
) -> None:
    temp_name = f"{FILE_WRITE_TEMP_PREFIX}{uuid.uuid4().hex}.tmp"
    temp_descriptor: int | None = None
    committed = False
    try:
        try:
            temp_descriptor = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o666,
                dir_fd=parent_descriptor,
            )
            with os.fdopen(temp_descriptor, "wb") as handle:
                temp_descriptor = None
                handle.write(data)
                if replacement_mode is not None:
                    os.fchmod(handle.fileno(), replacement_mode)
                handle.flush()
                os.fsync(handle.fileno())
            if exclusive:
                os.link(
                    temp_name,
                    destination_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            else:
                os.replace(
                    temp_name,
                    destination_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            committed = True
        finally:
            if temp_descriptor is not None:
                os.close(temp_descriptor)
            try:
                os.unlink(temp_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.fsync(parent_descriptor)
    except OSError as error:
        if committed:
            raise CommittedFileWriteError(
                error.errno,
                f"file changed, but directory sync or temporary cleanup failed: {error}",
                destination_name,
            ) from error
        raise
