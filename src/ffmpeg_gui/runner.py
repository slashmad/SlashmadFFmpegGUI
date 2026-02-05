from __future__ import annotations

import subprocess
import threading
from typing import Callable


class FFmpegRunner:
    def __init__(
        self,
        on_output: Callable[[str], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self._on_output = on_output
        self._on_exit = on_exit
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._process is not None

    def start(self, cmd: list[str]) -> None:
        if self._process is not None:
            raise RuntimeError("Process already running")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._process = process

        self._thread = threading.Thread(target=self._reader, args=(process,), daemon=True)
        self._thread.start()

    def _reader(self, process: subprocess.Popen[str]) -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    if line:
                        self._on_output(line.rstrip("\n"))
        finally:
            rc = process.wait()
            if self._process is process:
                self._process = None
            self._on_exit(rc)

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
