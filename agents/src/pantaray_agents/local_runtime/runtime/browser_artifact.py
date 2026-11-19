"""The PNG or PDF Electron writes for one browser operation.

Electron writes it at ``{artifact_root}/.tmp/browser/{operation_request_id}.{png|pdf}``
and its answer claims the file's size and sha256; the file is read only once it
matches that claim. The relay removes it on every path.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    open_regular_file_descriptor,
)

from .browser_contract import BrowserPdfClaim, BrowserPngClaim

BROWSER_TEMP_DIRECTORY = ".tmp/browser"

type BrowserArtifactKind = Literal["png", "pdf"]


class BrowserArtifactMismatchError(Exception):
    """The output does not match the answer; the message says how."""


def browser_artifact_path(operation_request_id: str, kind: BrowserArtifactKind) -> str:
    # Relative to the artifact root; Electron derives the same path from the id.
    return f"{BROWSER_TEMP_DIRECTORY}/{operation_request_id}.{kind}"


def read_claimed_artifact(
    *,
    artifact_root: Path,
    operation_request_id: str,
    expected_kind: BrowserArtifactKind | None,
    claim: BrowserPngClaim | BrowserPdfClaim | None,
) -> bytes | None:
    """The claimed file's bytes, or None for an operation that writes none.

    Raises :class:`BrowserArtifactMismatchError` when the claim names another kind
    than the operation writes, or the file is missing or differs from it.
    """

    if (None if claim is None else claim.kind) != expected_kind:
        raise BrowserArtifactMismatchError(
            "the answer names a different kind of output"
        )
    if claim is None:
        return None
    try:
        descriptor = open_regular_file_descriptor(
            root_path=artifact_root,
            relative_path=browser_artifact_path(operation_request_id, claim.kind),
        )
    except DescriptorPathError as error:
        raise BrowserArtifactMismatchError("no regular file was written") from error
    try:
        # Checked before reading, so a wrong claim never pulls an unbounded file
        # into memory.
        if os.fstat(descriptor).st_size != claim.byte_size:
            raise BrowserArtifactMismatchError("byte size differs")
        with open(descriptor, "rb", closefd=False) as handle:
            data = handle.read()
    finally:
        os.close(descriptor)
    if hashlib.sha256(data).hexdigest() != claim.sha256:
        raise BrowserArtifactMismatchError("sha256 differs")
    return data


__all__ = [
    "BROWSER_TEMP_DIRECTORY",
    "BrowserArtifactKind",
    "BrowserArtifactMismatchError",
    "browser_artifact_path",
    "read_claimed_artifact",
]
