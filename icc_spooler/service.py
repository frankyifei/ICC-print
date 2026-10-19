"""Spool folder watcher: process PostScript jobs and forward them RAW to a queue."""
from __future__ import annotations

import os
import time
from pathlib import Path

from .colorops import ColorEngine
from .engine import process_ps
from .i18n import tr


def list_printers() -> list[str]:
    """Enumerate local + connected printer queues (Windows)."""
    try:
        from brother_icc_print.printer import get_printer_names
        names = get_printer_names()
        if names:
            return names
    except Exception:
        pass
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return [item[2] for item in win32print.EnumPrinters(flags)]
    except Exception:
        return []


def forward_raw(printer: str, data: bytes, document_name: str):
    try:
        from brother_icc_print.printer import send_raw
        return send_raw(printer, data, document_name)
    except Exception:
        pass
    import win32print
    handle = win32print.OpenPrinter(printer)
    job_id = None
    try:
        job_id = win32print.StartDocPrinter(handle, 1, (document_name, None, "RAW"))
        win32print.StartPagePrinter(handle)
        try:
            win32print.WritePrinter(handle, data)
        finally:
            win32print.EndPagePrinter(handle)
    finally:
        if job_id is not None:
            win32print.EndDocPrinter(handle)
        win32print.ClosePrinter(handle)
    return job_id


def _is_stable(path: str, cache: dict) -> bool:
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    prev = cache.get(path)
    cache[path] = size
    if prev is None:
        return False
    return prev == size and size > 0


def process_file(source: Path, engine: ColorEngine, archive: Path,
                 printer: str, dry_run: bool, logger) -> None:
    data = source.read_bytes()
    out = process_ps(data, engine)
    archive.mkdir(parents=True, exist_ok=True)
    try:
        (archive / source.name).write_bytes(out)
    except OSError as exc:
        logger(tr('[warn] cannot write archive {name}: {exc}').format(name=source.name, exc=exc))
    if dry_run:
        logger(tr('[dry-run] {name}: {size_in} -> {size_out} bytes, archived to {dest} '
                  '(not sent); stats={stats}').format(
            name=source.name, size_in=len(data), size_out=len(out),
            dest=archive / source.name, stats=engine.stats))
        return
    job_id = forward_raw(printer, out, source.name)
    logger(tr('[sent] {name} -> job {job}; stats={stats}').format(
        name=source.name, job=job_id, stats=engine.stats))


def watch_loop(spool_dir: str, archive_dir: str, printer: str, engine: ColorEngine,
               pattern: str = "*.ps", poll_seconds: float = 1.0,
               dry_run: bool = False, logger=print, max_iterations=None,
               stop_event=None) -> None:
    spool = Path(spool_dir)
    spool.mkdir(parents=True, exist_ok=True)
    archive = Path(archive_dir)
    logger(f"[watch] spool={spool} pattern={pattern} archive={archive} printer={printer} dry_run={dry_run}")
    existing = [p.name for p in spool.glob(pattern) if p.is_file()]
    if existing:
        logger(tr('[watch] existing matching files: {files}').format(files=', '.join(existing)))
    else:
        allfiles = [p.name for p in spool.iterdir() if p.is_file()]
        if allfiles:
            logger(tr("[watch] note: files in the folder do not match '{pattern}': {files}").format(
                pattern=pattern, files=', '.join(allfiles)))
            logger(tr("[watch] change spool_pattern to a matching name (e.g. use '*.prn' for in.prn)."))
    cache: dict = {}
    iterations = 0
    while not (stop_event is not None and stop_event.is_set()):
        for path in sorted(spool.glob(pattern)):
            if not path.is_file():
                continue
            key = str(path)
            if not _is_stable(key, cache):
                continue
            try:
                process_file(path, engine, archive, printer, dry_run, logger)
            except Exception as exc:
                logger(tr('[error] processing {name} failed, skipped: {exc}').format(
                    name=path.name, exc=exc))
            finally:
                # Always remove the spool file after a job is handled. In dry-run
                # the processed copy stays in archive_dir, so a later restart can
                # never resend a leftover spool file to the real printer.
                try:
                    path.unlink()
                except OSError:
                    pass
                cache.pop(key, None)
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        deadline = time.time() + max(0.05, poll_seconds)
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(min(0.1, max(0.0, deadline - time.time())))
