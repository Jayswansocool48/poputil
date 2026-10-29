"""Background job runner with a shared activity log.

A job is a list of steps:
  ("user", "shell command")   runs as you
  ("root", "shell command")   runs through pkexec; consecutive root steps share one password prompt
  ("py",   callable)          runs in-process; print() output goes to the log, returning False fails the job
"""
import contextlib
import io
import os
import subprocess
import threading
import time

from .util import ANSI, which


class _LogWriter(io.TextIOBase):
    """Streams print() output from python steps into the log line by line."""

    def __init__(self, emit):
        self.emit, self.buf = emit, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.emit(line)
        return len(s)

    def flush(self):
        if self.buf:
            self.emit(self.buf)
            self.buf = ""


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.log, self.seq = [], 0
        self.queue, self.current, self.history = [], None, []
        self.job_seq = 0
        threading.Thread(target=self._worker, daemon=True, name="jobs").start()

    # ── log ──
    def emit(self, text, kind="out"):
        with self.lock:
            for line in ANSI.sub("", str(text)).split("\n"):
                if kind == "out" and not line.strip():
                    continue
                self.seq += 1
                self.log.append({"n": self.seq, "k": kind, "s": line})
            del self.log[:-4000]

    def since(self, n):
        with self.lock:
            return [line for line in self.log if line["n"] > n]

    # ── queue ──
    def submit(self, title, steps, tag="", done_title=None):
        with self.cv:
            self.job_seq += 1
            job = {"id": self.job_seq, "title": title, "done_title": done_title or title, "steps": steps,
                   "tag": tag, "status": "queued", "started": None, "ended": None}
            self.queue.append(job)
            self.cv.notify()
        return job["id"]

    def busy(self):
        with self.lock:
            return bool(self.current or self.queue)

    def status(self):
        def strip(j):
            return {k: j[k] for k in ("id", "title", "done_title", "status", "tag", "started", "ended")}
        with self.lock:
            return {"current": strip(self.current) if self.current else None,
                    "queued": [strip(j) for j in self.queue],
                    "history": [strip(j) for j in self.history[-30:]]}

    # ── running ──
    def _stream(self, argv):
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 text=True, bufsize=1, errors="replace",
                                 env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
        except OSError as e:
            self.emit(f"Couldn't start {argv[0]}: {e}", "err")
            return 127
        with p.stdout:
            for line in p.stdout:
                self.emit(line.rstrip("\n"))
        return p.wait()

    def _run_root(self, cmds):
        if not which("pkexec"):
            self.emit("pkexec isn't installed, so steps that need your password can't run.", "err")
            return 1
        for c in cmds:
            self.emit(f"$ sudo {c}", "cmd")
        script = "set -e\nexport DEBIAN_FRONTEND=noninteractive\n" + "\n".join(cmds)
        rc = self._stream(["pkexec", "/bin/bash", "-c", script])
        if rc in (126, 127):
            self.emit("The password prompt was cancelled, so nothing that needed it was changed.", "err")
        return rc

    def _run_py(self, fn):
        out = _LogWriter(self.emit)
        try:
            with contextlib.redirect_stdout(out):
                res = fn()
            return res is not False
        except Exception as e:  # noqa: BLE001 — anything a step raises is reported, never swallowed
            self.emit(f"{type(e).__name__}: {e}", "err")
            return False
        finally:
            out.flush()

    def _worker(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                job = self.queue.pop(0)
                job.update(status="running", started=time.time())
                self.current = job
            self.emit(job["title"], "job")
            ok, steps, i = True, job["steps"], 0
            while i < len(steps) and ok:
                kind, payload = steps[i]
                if kind == "root":
                    batch = []
                    while i < len(steps) and steps[i][0] == "root":
                        batch.append(steps[i][1])
                        i += 1
                    ok = self._run_root(batch) == 0
                    continue
                if kind == "user":
                    self.emit(f"$ {payload}", "cmd")
                    ok = self._stream(["/bin/bash", "-c", payload]) == 0
                else:
                    ok = self._run_py(payload)
                i += 1
            with self.lock:
                job.update(status="done" if ok else "failed", ended=time.time())
                self.history.append(job)
                self.current = None
            self.emit("Done." if ok else "Stopped: a step didn't finish. Details are above.", "ok" if ok else "err")


JOBS = Jobs()
