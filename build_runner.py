"""Bounded tool execution without preexec_fn (the HTTP server is threaded).

Linux prlimit applies inherited per-process limits before exec. A process group
and Bubblewrap's PID namespace ensure descendants die on timeout/limit failure.
"""
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time
import threading

BUILD_TIMEOUT = int(os.environ.get("BUILD_TIMEOUT_SECONDS", "2400"))
MEMORY_LIMIT = int(os.environ.get("BUILD_MEMORY_MB", "4096")) * 1024 * 1024
CPU_LIMIT = int(os.environ.get("BUILD_CPU_SECONDS", "1800"))
DISK_LIMIT = int(os.environ.get("BUILD_DISK_MB", "512")) * 1024 * 1024
FILE_LIMIT = int(os.environ.get("BUILD_FILE_MB", "128")) * 1024 * 1024
FILE_COUNT_LIMIT = 2048
TOOL_SLOT = threading.BoundedSemaphore(1)


def workspace_size(cwd):
    size, count = 0, 0
    for root, dirs, files in os.walk(cwd, followlinks=False):
        count += len(dirs) + len(files)
        if count > FILE_COUNT_LIMIT:
            raise ValueError("Workspace file count limit exceeded")
        for name in files:
            try:
                stat = (Path(root) / name).lstat()
                size += stat.st_size
            except FileNotFoundError:
                pass
    if count > FILE_COUNT_LIMIT:
        raise ValueError("Workspace file count limit exceeded")
    return size


def _execute(command, cwd, timeout, output_file=None, deadline=None, progress=None):
    prlimit = shutil.which("prlimit")
    if not prlimit:
        raise ValueError("Linux prlimit is required to enforce build resource limits")
    stop = min(time.monotonic() + timeout, deadline or float("inf"))
    if stop <= time.monotonic():
        raise ValueError("Build time limit exceeded")
    if workspace_size(cwd) > DISK_LIMIT:
        raise ValueError("Workspace disk limit exceeded")
    command = [prlimit, f"--as={MEMORY_LIMIT}", f"--cpu={CPU_LIMIT}",
               f"--fsize={FILE_LIMIT}", "--nofile=256", "--", *command]
    destination = (Path(cwd) / output_file).open("wb") if output_file else None
    tail = bytearray()
    if progress:
        progress("Starting tool process.\n")
    process = None
    acquired = False
    try:
        acquired = TOOL_SLOT.acquire(timeout=max(0, stop - time.monotonic()))
        if not acquired:
            raise ValueError("Tool/build time limit exceeded while waiting for compute")
        process = subprocess.Popen(command, cwd=cwd, stdout=destination or subprocess.PIPE,
                                   stderr=subprocess.PIPE if destination else subprocess.STDOUT,
                                   start_new_session=True)
        pipe = process.stderr if destination else process.stdout
        with selectors.DefaultSelector() as selector:
            selector.register(pipe, selectors.EVENT_READ)
            last_disk_check = 0
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if now >= stop:
                    raise ValueError(f"Tool/build time limit exceeded ({timeout}s tool limit)")
                if now - last_disk_check >= 0.1:
                    last_disk_check = now
                    if workspace_size(cwd) > DISK_LIMIT:
                        raise ValueError("Workspace disk limit exceeded")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        tail.extend(chunk)
                        del tail[:-24000]
                        if progress:
                            progress(chunk.decode("utf-8", errors="replace"))
            process.wait()
        if destination:
            destination.flush()
        if workspace_size(cwd) > DISK_LIMIT:
            raise ValueError("Workspace disk limit exceeded")
        output = tail.decode("utf-8", errors="replace")
        if process.returncode:
            raise ValueError(output.strip() or f"Tool exited with {process.returncode}; CPU/memory/file limit may have been reached")
        return output
    except BaseException:
        if process:
            # Kill even when the group leader exited and left a child holding a pipe.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if output_file:
            (Path(cwd) / output_file).unlink(missing_ok=True)
        raise
    finally:
        if process:
            # Successful tools must not leave background children running either.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        if destination:
            destination.close()
        if acquired:
            TOOL_SLOT.release()


def run(command, cwd, timeout=120, *, deadline=None, progress=None):
    return _execute(command, cwd, timeout, deadline=deadline, progress=progress)


def run_to_file(command, output_file, cwd, timeout=120, *, deadline=None, progress=None):
    return _execute(command, cwd, timeout, output_file, deadline, progress)
