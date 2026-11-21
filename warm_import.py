"""Reuse the clean prefix of the exact forced-import check."""

import atexit
import contextlib
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import cpu_budget
from import_check import FAILED, PASSED, REQUEST, RESPONSE, UNSUPPORTED


class WarmImport:
    def __init__(self, root):
        self.root = root.resolve()
        self.helper = Path(__file__).with_name("import_check.py")
        self.process = None
        self.disabled = False
        self.served = 0
        atexit.register(self.close)

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-IBS", str(self.helper), str(self.root), "--serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, start_new_session=True,
        )
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)

    def __call__(self, path, modified):
        if self.disabled:
            return None
        name, source = path.encode(), modified.encode()
        if not 0 < len(name) <= 1024 or not 0 < len(source) <= 1_500_000:
            return None
        deadline = time.monotonic() + 1.55
        try:
            if self.process is None:
                self.start()
            writer = self.process.stdin.fileno()
            payload = memoryview(REQUEST.pack(len(name), len(source)) + name + source)
            while payload:
                if not select.select([], [writer], [], max(0, deadline - time.monotonic()))[1]:
                    raise TimeoutError
                payload = payload[os.write(writer, payload):]
            reader = self.process.stdout.fileno()
            result = bytearray()
            while len(result) < RESPONSE.size:
                if not select.select([reader], [], [], max(0, deadline - time.monotonic()))[0]:
                    raise TimeoutError
                chunk = os.read(reader, RESPONSE.size - len(result))
                if not chunk:
                    raise EOFError
                result.extend(chunk)
            state, cpu_budget.live_import_cpu = RESPONSE.unpack(result)
            if state == UNSUPPORTED:
                return None
            if state in (PASSED, FAILED):
                self.served += 1
                return state == PASSED
        except (OSError, EOFError, ValueError):
            pass
        self.close()
        self.disabled = True
        return None

    def close(self):
        process = self.process
        if process is None:
            return
        self.process = None
        try:
            process.stdin.close()
            try:
                process.wait(timeout=0.12)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        finally:
            process.stdout.close()
            # Once waited, the server and its completed descendants are
            # already present in the selector's RUSAGE_CHILDREN accounting.
            cpu_budget.live_import_cpu = 0.0
