"""Read-only PDF extraction and local image OCR tools for Kara.

All inputs remain on the local machine and use Kara's existing file-read roots.
No document or image is uploaded to an external service.
"""
from __future__ import annotations

import base64
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import config
from tools.file_tools import _resolve_path

def _positive_int_setting(name: str, default: int, *, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer.") from exc
    if value <= 0 or value > maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}.")
    return value


def _positive_float_setting(name: str, default: float, *, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number.") from exc
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise RuntimeError(f"{name} must be greater than 0 and at most {maximum:g}.")
    return value


MAX_DOCUMENT_BYTES = _positive_int_setting(
    "KARA_DOCUMENT_MAX_BYTES", 50 * 1024 * 1024, maximum=1024 * 1024 * 1024
)
MAX_PDF_PAGES = _positive_int_setting("KARA_PDF_MAX_PAGES", 50, maximum=500)
MAX_EXTRACTED_CHARS = _positive_int_setting(
    "KARA_DOCUMENT_MAX_CHARS", 50000, maximum=5_000_000
)
MAX_IMAGE_PIXELS = _positive_int_setting(
    "KARA_OCR_MAX_IMAGE_PIXELS", 40_000_000, maximum=200_000_000
)
OCR_TIMEOUT_SECONDS = _positive_float_setting(
    "KARA_OCR_TIMEOUT_SECONDS", 30, maximum=600
)
PDF_TIMEOUT_SECONDS = _positive_float_setting(
    "KARA_PDF_TIMEOUT_SECONDS", 60, maximum=1800
)
PDF_WORKER_MEMORY_MB = _positive_int_setting(
    "KARA_PDF_WORKER_MEMORY_MB", 512, maximum=4096
)
_IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff"}
_PDF_WORKER_BOOTSTRAP = r"""
import importlib.util
import os
import pathlib
import sys
import types

root = pathlib.Path(sys.argv[1]).resolve()

def load_exact(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load worker module {name}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

config = types.ModuleType("config")
config.PACKAGE_DIR = root
config.FILE_READ_ROOTS = tuple(
    pathlib.Path(value).resolve()
    for value in os.environ["KARA_FILE_READ_ROOTS"].split(os.pathsep)
    if value
)
sys.modules["config"] = config
tools = types.ModuleType("tools")
tools.__path__ = []
sys.modules["tools"] = tools
file_tools = load_exact("tools.file_tools", root / "tools" / "file_tools.py")
setattr(tools, "file_tools", file_tools)
document_tools = load_exact("tools.document_tools", root / "tools" / "document_tools.py")
raise SystemExit(document_tools._run_pdf_worker())
"""


class _PdfTimeLimitError(RuntimeError):
    pass


_WINDOWS_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
function Await-WinRt {
    param($Operation, [Type]$ResultType)
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
            $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    try { $task.Wait() }
    catch { throw $task.Exception.Flatten().InnerException }
    return $task.Result
}
$path = $env:KARA_OCR_IMAGE_PATH
$file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { throw 'No Windows OCR language is installed.' }
$result = Await-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::Write($result.Text)
"""


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _read_target(path: str, extensions: set[str]) -> Path:
    target = _resolve_path(path, config.FILE_READ_ROOTS, purpose="read")
    if not target.exists() or not target.is_file():
        raise ValueError(f"File does not exist: {target}")
    if target.suffix.casefold() not in extensions:
        allowed = ", ".join(sorted(extensions))
        raise ValueError(f"Unsupported file type. Expected one of: {allowed}")
    size = target.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise ValueError(
            f"File is too large ({size} bytes); limit is {MAX_DOCUMENT_BYTES} bytes."
        )
    return target


def _trusted_powershell_path() -> Path:
    if os.name != "nt":
        raise RuntimeError("Windows PowerShell is required for local OCR.")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise RuntimeError("Could not resolve the trusted Windows directory.")
    windows_directory = Path(buffer.value).resolve()
    powershell = (
        windows_directory / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    ).resolve()
    expected_parent = (
        windows_directory / "System32" / "WindowsPowerShell" / "v1.0"
    ).resolve()
    if powershell.parent != expected_parent or not powershell.is_file():
        raise RuntimeError("Trusted Windows PowerShell executable was not found.")
    return powershell


def _minimal_local_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in (
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "LANG",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _minimal_ocr_environment(target: Path) -> dict[str, str]:
    environment = _minimal_local_environment()
    environment["KARA_OCR_IMAGE_PATH"] = str(target)
    return environment


def _windows_ocr_text(target: Path, *, timeout_seconds: float | None = None) -> str:
    powershell = _trusted_powershell_path()
    encoded = base64.b64encode(_WINDOWS_OCR_SCRIPT.encode("utf-16le")).decode("ascii")
    environment = _minimal_ocr_environment(target)
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=OCR_TIMEOUT_SECONDS if timeout_seconds is None else max(0.001, timeout_seconds),
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        detail = detail[-2000:] if detail else f"PowerShell exited {completed.returncode}."
        raise RuntimeError(detail)
    return completed.stdout.strip()


def ocr_image(path: str) -> str:
    """Extract text locally from an image using Windows' built-in OCR engine.

    Args:
        path: Existing PNG, JPEG, BMP, or TIFF image inside an allowed read root.

    Returns:
        Structured JSON containing OCR text and image dimensions. Nothing is uploaded.
    """
    try:
        from PIL import Image, UnidentifiedImageError

        target = _read_target(path, _IMAGE_EXTENSIONS)
        try:
            with Image.open(target) as image:
                width, height = image.size
                if width < 1 or height < 1:
                    raise ValueError("Image dimensions must be positive.")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ValueError(
                        f"Image has {width * height} pixels; limit is {MAX_IMAGE_PIXELS}."
                    )
                image.verify()
        except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise ValueError(f"Invalid or unsafe image: {exc}") from exc

        text = _windows_ocr_text(target)
        truncated = len(text) > MAX_EXTRACTED_CHARS
        if truncated:
            text = text[:MAX_EXTRACTED_CHARS]
        return _json(
            {
                "ok": True,
                "type": "image_ocr",
                "path": str(target),
                "width": width,
                "height": height,
                "engine": "windows_media_ocr",
                "text": text,
                "truncated": truncated,
            }
        )
    except subprocess.TimeoutExpired:
        return _json(
            {
                "ok": False,
                "error": f"Image OCR exceeded the {OCR_TIMEOUT_SECONDS:g}-second limit.",
            }
        )
    except (ImportError, ValueError, PermissionError, OSError, RuntimeError) as exc:
        return _json({"ok": False, "error": f"Could not OCR image: {exc}"})


def _assign_windows_worker_job(process: subprocess.Popen[str]) -> int | None:
    """Place the worker tree in a kill-on-close, memory-limited Windows Job Object."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(kernel32.GetLastError(), "CreateJobObjectW failed")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00000200 | 0x00002000
    info.JobMemoryLimit = PDF_WORKER_MEMORY_MB * 1024 * 1024
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)
    ):
        error = kernel32.GetLastError()
        kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        error = kernel32.GetLastError()
        kernel32.CloseHandle(job)
        raise OSError(error, "AssignProcessToJobObject failed")
    return int(job)


def _close_windows_handle(handle: int | None) -> None:
    if handle and os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.CloseHandle(handle)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a timed-out worker and any OCR child process it spawned."""
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        if taskkill.is_file():
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    if process.poll() is None:
        process.kill()


def _invoke_pdf_worker(request: dict[str, object], environment: dict[str, str]) -> tuple[int, str, str]:
    args = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        _PDF_WORKER_BOOTSTRAP,
        str(config.PACKAGE_DIR),
    ]
    windows_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if os.name == "nt"
        else 0
    )
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        cwd=str(Path(sys.executable).resolve().parent),
        creationflags=windows_flags,
        start_new_session=(os.name != "nt"),
    )
    job_handle: int | None = None
    try:
        job_handle = _assign_windows_worker_job(process)
    except OSError:
        _terminate_process_tree(process)
        raise
    try:
        stdout, stderr = process.communicate(
            json.dumps(request),
            timeout=max(0.001, PDF_TIMEOUT_SECONDS + 2.0),
        )
    except subprocess.TimeoutExpired:
        _close_windows_handle(job_handle)
        job_handle = None
        _terminate_process_tree(process)
        try:
            process.communicate(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        raise
    finally:
        _close_windows_handle(job_handle)
    return process.returncode, stdout, stderr


def read_pdf(
    path: str,
    start_page: int = 1,
    max_pages: int = 20,
    ocr_if_needed: bool = True,
    start_char: int = 0,
) -> str:
    """Extract bounded text from a local PDF in a time-limited worker process.

    The worker isolates PyMuPDF parsing and scanned-page rendering from Kara's
    long-lived chat process. All file access still uses Kara's configured read roots.
    """
    try:
        target = _read_target(path, {".pdf"})
        request = {
            "path": str(target),
            "start_page": int(start_page),
            "max_pages": int(max_pages),
            "ocr_if_needed": bool(ocr_if_needed),
            "start_char": max(0, int(start_char)),
            "deadline_monotonic": time.monotonic() + max(0.001, PDF_TIMEOUT_SECONDS),
        }
        environment = _minimal_local_environment()
        allow_sensitive = os.environ.get("KARA_ALLOW_SENSITIVE_FILES")
        if allow_sensitive is not None:
            environment["KARA_ALLOW_SENSITIVE_FILES"] = allow_sensitive
        environment["KARA_FILE_READ_ROOTS"] = os.pathsep.join(
            str(root) for root in config.FILE_READ_ROOTS
        )
        environment["KARA_PDF_TIMEOUT_SECONDS"] = str(PDF_TIMEOUT_SECONDS)
        environment["KARA_PDF_WORKER_MEMORY_MB"] = str(PDF_WORKER_MEMORY_MB)
        environment["KARA_DOCUMENT_MAX_BYTES"] = str(MAX_DOCUMENT_BYTES)
        environment["KARA_DOCUMENT_MAX_CHARS"] = str(MAX_EXTRACTED_CHARS)
        environment["KARA_PDF_MAX_PAGES"] = str(MAX_PDF_PAGES)
        environment["KARA_OCR_MAX_IMAGE_PIXELS"] = str(MAX_IMAGE_PIXELS)
        environment["KARA_OCR_TIMEOUT_SECONDS"] = str(OCR_TIMEOUT_SECONDS)
        returncode, stdout, stderr = _invoke_pdf_worker(request, environment)
        if returncode != 0:
            detail = (stderr or stdout).strip()
            detail = detail[-2000:] if detail else f"PDF worker exited {returncode}."
            raise RuntimeError(detail)
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("PDF worker returned an invalid response.")
        return _json(payload)
    except subprocess.TimeoutExpired:
        return _json(
            {
                "ok": False,
                "error": f"PDF processing exceeded the {PDF_TIMEOUT_SECONDS:g}-second time limit.",
            }
        )
    except (json.JSONDecodeError, ValueError, PermissionError, OSError, RuntimeError) as exc:
        return _json({"ok": False, "error": f"Could not read PDF: {exc}"})


def _read_pdf_local(
    path: str,
    start_page: int = 1,
    max_pages: int = 20,
    ocr_if_needed: bool = True,
    start_char: int = 0,
    deadline_monotonic: float | None = None,
) -> str:
    """Extract bounded text from a local PDF, with optional OCR for scanned pages.

    Args:
        path: Existing .pdf file inside an allowed read root.
        start_page: First 1-based page to read.
        max_pages: Maximum pages returned in this call (1-50 by default).
        ocr_if_needed: OCR pages that contain no useful embedded text.
        start_char: Character offset within the first requested page for lossless continuation.

    Returns:
        Structured JSON with page text, extraction method, and truncation state.
    """
    try:
        import pymupdf

        target = _read_target(path, {".pdf"})
        deadline = (
            float(deadline_monotonic)
            if deadline_monotonic is not None
            else time.monotonic() + max(0.001, PDF_TIMEOUT_SECONDS)
        )
        first = max(1, int(start_page))
        first_char = max(0, int(start_char))
        limit = max(1, min(int(max_pages), MAX_PDF_PAGES))
        document = pymupdf.open(target)
        try:
            if document.needs_pass:
                raise ValueError("Encrypted PDF requires a password and cannot be read.")
            total_pages = document.page_count
            if first > total_pages and total_pages:
                raise ValueError(
                    f"start_page {first} is beyond the PDF's {total_pages} page(s)."
                )
            pages: list[dict[str, Any]] = []
            remaining_chars = MAX_EXTRACTED_CHARS
            stop = min(total_pages, first - 1 + limit)
            for page_index in range(first - 1, stop):
                if time.monotonic() >= deadline:
                    raise _PdfTimeLimitError("PDF processing time limit reached.")
                page = document.load_page(page_index)
                embedded_text = page.get_text("text").strip()
                text = embedded_text
                method = "embedded_text"
                warning = ""
                if len(embedded_text) < 20 and ocr_if_needed:
                    method = "ocr"
                    try:
                        with tempfile.TemporaryDirectory(prefix="kara-pdf-ocr-") as raw:
                            render_width = max(1, int(page.rect.width * 200 / 72 + 0.5))
                            render_height = max(1, int(page.rect.height * 200 / 72 + 0.5))
                            render_pixels = render_width * render_height
                            if render_pixels > MAX_IMAGE_PIXELS:
                                raise ValueError(
                                    f"OCR page render has {render_pixels} pixels; "
                                    f"pixel limit is {MAX_IMAGE_PIXELS}."
                                )
                            image_path = Path(raw) / f"page-{page_index + 1}.png"
                            pixmap = page.get_pixmap(dpi=200, alpha=False)
                            pixmap.save(image_path)
                            remaining_time = deadline - time.monotonic()
                            if remaining_time <= 0:
                                raise _PdfTimeLimitError("PDF processing time limit reached.")
                            ocr_text = _windows_ocr_text(
                                image_path,
                                timeout_seconds=min(OCR_TIMEOUT_SECONDS, remaining_time),
                            )
                        if ocr_text:
                            text = ocr_text
                        else:
                            text = embedded_text
                            method = "embedded_text" if embedded_text else "ocr_empty"
                            warning = "OCR completed but found no text on this page."
                    except _PdfTimeLimitError:
                        raise
                    except subprocess.TimeoutExpired as exc:
                        raise _PdfTimeLimitError("PDF processing time limit reached.") from exc
                    except (ValueError, OSError, RuntimeError) as exc:
                        method = "ocr_failed"
                        warning = f"Could not OCR page: {exc}"
                page_start_char = first_char if page_index == first - 1 else 0
                if page_start_char > len(text):
                    raise ValueError(
                        f"start_char {page_start_char} is beyond page {page_index + 1}'s "
                        f"{len(text)} extracted character(s)."
                    )
                available_text = text[page_start_char:]
                text_truncated = len(available_text) > remaining_chars
                returned_text = available_text[:remaining_chars]
                if text_truncated:
                    warning = (warning + " " if warning else "") + "Character limit reached."
                remaining_chars -= len(returned_text)
                page_next_char = (
                    page_start_char + len(returned_text) if text_truncated else None
                )
                pages.append(
                    {
                        "number": page_index + 1,
                        "method": method,
                        "text": returned_text,
                        "start_char": page_start_char,
                        "next_char": page_next_char,
                        "text_truncated": text_truncated,
                        "warning": warning,
                    }
                )
                if text_truncated or remaining_chars <= 0:
                    break
            truncated_page = next(
                (page_data for page_data in pages if page_data["text_truncated"]), None
            )
            has_unread_pages = bool(pages and pages[-1]["number"] < total_pages)
            truncated = bool(truncated_page or has_unread_pages)
            if truncated_page:
                next_page = truncated_page["number"]
                next_char = truncated_page["next_char"]
            elif has_unread_pages:
                next_page = pages[-1]["number"] + 1
                next_char = 0
            else:
                next_page = None
                next_char = None
            return _json(
                {
                    "ok": True,
                    "type": "pdf",
                    "path": str(target),
                    "page_count": total_pages,
                    "pages": pages,
                    "truncated": truncated,
                    "next_page": next_page,
                    "next_char": next_char,
                }
            )
        finally:
            document.close()
    except (ImportError, ValueError, PermissionError, OSError, RuntimeError) as exc:
        return _json({"ok": False, "error": f"Could not read PDF: {exc}"})


def _run_pdf_worker() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("Worker request must be a JSON object.")
        result = _read_pdf_local(
            path=str(request["path"]),
            start_page=int(request.get("start_page", 1)),
            max_pages=int(request.get("max_pages", 20)),
            ocr_if_needed=bool(request.get("ocr_if_needed", True)),
            start_char=max(0, int(request.get("start_char", 0))),
            deadline_monotonic=float(request["deadline_monotonic"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = _json({"ok": False, "error": f"Invalid PDF worker request: {exc}"})
    sys.stdout.write(result)
    return 0
