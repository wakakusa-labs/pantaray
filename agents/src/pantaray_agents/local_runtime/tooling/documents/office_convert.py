"""Lay out a Word, PowerPoint or Excel file as a PDF the page renderer can draw.

LibreOffice does the layout, headless, in a process of its own: a fresh work
directory, a fresh profile, a short environment, the seatbelt profile in
office_convert_sandbox, and a deadline. Nothing of a conversion outlives the
call: its whole process group is killed, and the work directory, the profile
and the copy of the input are removed, however the call ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import signal
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from ..action_session_temp_paths import create_private_temp_dir
from .document_model import DocumentExtractionError
from .office_convert_sandbox import sandboxed_argv

OfficeFormat = Literal["docx", "pptx", "xlsx"]

# Design limit: a cold conversion measured about 3.5 s per file, so this only
# ends a conversion that has stopped making progress; raise it if real
# documents are reported as timing out.
MAX_OFFICE_CONVERT_SECONDS: Final = 60.0
# Each format's PDF export, with the options that keep a page number meaning
# what the read tool's unit number means: every slide including hidden ones, so
# slide N is page N, and one page per sheet, so a chart is never split across
# pages.
_EXPORT_FILTERS: Final[Mapping[OfficeFormat, str]] = MappingProxyType(
    {
        "docx": "pdf:writer_pdf_Export",
        "pptx": (
            "pdf:impress_pdf_Export:"
            '{"ExportHiddenSlides":{"type":"boolean","value":"true"}}'
        ),
        "xlsx": (
            'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
        ),
    }
)
# The parent's variables LibreOffice may use; everything else stays behind.
_INHERITED_ENVIRONMENT: Final = ("HOME", "LANG")
# LibreOffice's stderr, as relayed by a process that was holding an untrusted
# file when it wrote it. Read as bytes first so a long stderr is never loaded.
_MAX_REASON_CHARS: Final = 200
_MAX_REASON_BYTES: Final = 4 * _MAX_REASON_CHARS
# What opening the output reports when it is absent, or when soffice put a
# link where the output or its directory should be.
_MISSING_OR_PLANTED: Final = frozenset({errno.ENOENT, errno.ELOOP, errno.ENOTDIR})


class OfficeDocumentUnreadableError(DocumentExtractionError):
    """LibreOffice exited without producing a PDF of the document."""


class OfficeConversionTimeoutError(DocumentExtractionError):
    """LibreOffice was still converting when its time ran out, and was killed."""


async def convert_office_to_pdf(
    *,
    db_path: Path,
    libreoffice_app: Path,
    source: Path,
    document_format: OfficeFormat,
    destination: Path,
    timeout_seconds: float = MAX_OFFICE_CONVERT_SECONDS,
) -> None:
    """Convert ``source`` and leave the PDF at ``destination``.

    ``libreoffice_app`` is the resolved real path of a LibreOffice.app bundle
    the caller has already checked. ``source`` is copied before LibreOffice
    starts, so the conversion reads only its own copy. ``destination`` is
    replaced atomically, and is untouched when the conversion fails.

    Raises:
        OfficeDocumentUnreadableError: LibreOffice failed or produced no PDF.
        OfficeConversionTimeoutError: the conversion ran out of time and its
            process group was killed.
    """

    work_dir = create_private_temp_dir(db_path=db_path, prefix="pantaray-office-")
    try:
        for name in ("in", "out", "tmp"):
            (work_dir / name).mkdir()
        staged = work_dir / "in" / f"input.{document_format}"
        shutil.copyfile(source, staged)
        returncode, reason = await _run_libreoffice(
            libreoffice_app=libreoffice_app,
            work_dir=work_dir,
            staged=staged,
            document_format=document_format,
            timeout_seconds=timeout_seconds,
        )
        pdf = _open_output(work_dir / "out")
        # LibreOffice exits 0 when it cannot load a document, so the PDF itself
        # is the evidence of success.
        if returncode != 0 or pdf is None:
            if pdf is not None:
                os.close(pdf)
            raise OfficeDocumentUnreadableError(
                f"LibreOffice did not convert the document (exit status "
                f"{returncode}): {reason}"
            )
        try:
            _replace_from(pdf, destination)
        finally:
            os.close(pdf)
    finally:
        shutil.rmtree(work_dir)


async def _run_libreoffice(
    *,
    libreoffice_app: Path,
    work_dir: Path,
    staged: Path,
    document_format: OfficeFormat,
    timeout_seconds: float,
) -> tuple[int, str]:
    """LibreOffice's exit status and the tail of what it wrote to stderr."""

    argv = (
        str(libreoffice_app / "Contents" / "MacOS" / "soffice"),
        "--headless",
        "--norestore",
        "--nologo",
        "--nolockcheck",
        # A profile of its own per conversion: the user's LibreOffice profile
        # is never touched, and concurrent conversions get separate
        # single-instance pipes instead of handing work to one another.
        f"-env:UserInstallation={(work_dir / 'profile').as_uri()}",
        "--convert-to",
        _EXPORT_FILTERS[document_format],
        "--outdir",
        str(work_dir / "out"),
        str(staged),
    )
    # stderr goes to a file rather than a pipe: a descendant that inherits a
    # pipe can hold it open after soffice exits, and waiting for its end would
    # then wait for that descendant. The file sits inside the work dir, where
    # the sandbox lets soffice write, and is read back through this process's
    # own descriptor, so renaming or replacing the name changes nothing here.
    with (work_dir / "stderr.log").open("w+b") as errors:
        process = await asyncio.create_subprocess_exec(
            *sandboxed_argv(argv, libreoffice_app=libreoffice_app, work_dir=work_dir),
            cwd=work_dir,
            env=_environment(work_dir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=errors,
            # Its own process group, so whatever it started can be ended with it.
            start_new_session=True,
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout_seconds)
        except TimeoutError as error:
            raise OfficeConversionTimeoutError(
                f"conversion did not finish within {timeout_seconds:g} seconds"
            ) from error
        finally:
            # However soffice ended, nothing it started outlives the call. The
            # group is already empty when nothing was left behind.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.returncode is None:
                await process.wait()
        errors.seek(max(0, errors.seek(0, os.SEEK_END) - _MAX_REASON_BYTES))
        reason = errors.read().decode("utf-8", errors="replace").strip()
    return returncode, reason[-_MAX_REASON_CHARS:]


def _open_output(out_dir: Path) -> int | None:
    """The PDF soffice wrote, opened without trusting anything it could plant.

    Everything under the work directory was writable by the sandboxed process,
    and this one is not sandboxed, so the output is opened without following a
    link at either level, without blocking on a FIFO, and only kept when it is
    a regular file no other name shares -- a link or a hard link would hand the
    caller a file from outside the sandbox. None when there is no such PDF.
    """

    try:
        dir_fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(
                "input.pdf",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=dir_fd,
            )
        finally:
            os.close(dir_fd)
    except OSError as error:
        if error.errno in _MISSING_OR_PLANTED:
            return None
        raise
    status = os.fstat(fd)
    if stat.S_ISREG(status.st_mode) and status.st_nlink == 1:
        return fd
    os.close(fd)
    return None


def _replace_from(pdf: int, destination: Path) -> None:
    """Copy the open ``pdf`` beside ``destination``, then rename it into place."""

    handle, partial = tempfile.mkstemp(
        prefix=".office-", suffix=".pdf.partial", dir=destination.parent
    )
    try:
        with (
            open(pdf, "rb", closefd=False) as source,
            os.fdopen(handle, "wb") as target,
        ):
            shutil.copyfileobj(source, target)
        os.replace(partial, destination)
    except BaseException:
        os.unlink(partial)
        raise


def _environment(work_dir: Path) -> dict[str, str]:
    inherited = {
        name: os.environ[name] for name in _INHERITED_ENVIRONMENT if name in os.environ
    }
    return {**inherited, "PATH": "/usr/bin:/bin", "TMPDIR": str(work_dir / "tmp")}


__all__ = [
    "MAX_OFFICE_CONVERT_SECONDS",
    "OfficeConversionTimeoutError",
    "OfficeDocumentUnreadableError",
    "OfficeFormat",
    "convert_office_to_pdf",
]
