"""Process orchestration: start an approved CLI, watch it, capture everything.

    task  -> argv (registry)  -> subprocess.Popen(shell=False, scrubbed env,
             per-run working dir, rlimits, own process group)
          -> reader threads for stdout (NDJSON events) and stderr (diagnostics)
          -> watchdog thread (hard timeout) / cancel flag
          -> finalize: exit code + output file + hash verification -> status

Cross-platform: uses threads + Popen (works on Linux, macOS and Windows).
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from . import config
from .intent import TaskSpec
from .redact import redact
from .tools import REGISTRY, ToolSpec, build_argv

IS_POSIX = os.name == "posix"
RLIMITS = {"cpu_seconds": 120, "address_space_mb": 1024, "max_file_mb": 50, "open_files": 256}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sandbox_env() -> dict:
    """Least-privilege environment: no API keys or other secrets reach the CLI."""
    keep = ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"PYTHONPATH": str(config.CLI_DIR), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def _limit_resources():  # runs in the child before exec (POSIX only)
    import resource
    mb = 1024 * 1024
    for lim, val in ((resource.RLIMIT_CPU, RLIMITS["cpu_seconds"]),
                     (resource.RLIMIT_AS, RLIMITS["address_space_mb"] * mb),
                     (resource.RLIMIT_FSIZE, RLIMITS["max_file_mb"] * mb),
                     (resource.RLIMIT_NOFILE, RLIMITS["open_files"])):
        try:
            resource.setrlimit(lim, (val, val))
        except (ValueError, OSError):
            pass


class ResultModel(BaseModel):
    """Minimal schema the CLI output must satisfy before we trust it."""
    schema_version: str
    status: str
    start_url: str
    pages: list[dict]
    stats: dict
    security: dict


class Run:
    def __init__(self, task: TaskSpec, tool: ToolSpec, replay_of: str | None = None):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.task, self.tool, self.replay_of = task, tool, replay_of
        self.dir = config.RUNS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.out_path = self.dir / "result.json"
        self.argv = build_argv(tool, task, "result.json")  # relative: resolved inside the run dir
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.state = "queued"
        self.cancelled = self.timed_out = False
        self.exit_code: int | None = None
        self.started_at = self.ended_at = None
        self.t0 = None
        self.stderr_lines: list[str] = []
        self.stdout_events = 0
        self.final: dict | None = None
        self.done = threading.Event()

    # -------------------------------------------------------------- events
    def add(self, kind: str, **data):
        with self.lock:
            ev = {"seq": len(self.events), "kind": kind, "t": round(time.monotonic() - (self.t0 or time.monotonic()), 3), **data}
            self.events.append(ev)
        return ev

    def events_since(self, n: int) -> list[dict]:
        with self.lock:
            return self.events[n:]

    # --------------------------------------------------------------- start
    def start(self, on_finish=None):
        self.t0 = time.monotonic()
        self.started_at = now_iso()
        self.add("stage", stage="execute", status="active")
        kwargs = dict(cwd=self.dir, env=_sandbox_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1, shell=False)
        if IS_POSIX:
            kwargs.update(start_new_session=True, preexec_fn=_limit_resources)
        else:
            kwargs.update(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            self.proc = subprocess.Popen(self.argv, **kwargs)
        except OSError as exc:
            self.add("process_error", error=f"could not start CLI: {exc}")
            self.exit_code = -1
            self._finalize()
            if on_finish:
                on_finish(self)
            return
        self.state = "running"
        self.add("process_started", pid=self.proc.pid, argv=self.display_argv(), cwd=f"runs/{self.id}")
        t_out = threading.Thread(target=self._read_stdout, daemon=True)
        t_err = threading.Thread(target=self._read_stderr, daemon=True)
        t_out.start(); t_err.start()

        def watchdog():
            try:
                self.proc.wait(timeout=config.HARD_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.timed_out = True
                self.add("timeout", message=f"hard timeout of {config.HARD_TIMEOUT_S}s reached - terminating process group")
                self._terminate()
            t_out.join(5); t_err.join(5)
            self.exit_code = self.proc.returncode
            self._finalize()
            if on_finish:
                on_finish(self)

        threading.Thread(target=watchdog, daemon=True).start()

    def display_argv(self) -> list[str]:
        return ["python", *self.argv[1:]]

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            self.stdout_events += 1
            with open(self.dir / "stdout.ndjson", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            try:
                ev = json.loads(line)
                name = ev.pop("event", "unknown")
                ev.pop("ts", None)
                self.add("cli", event=name, data=ev)
            except ValueError:
                self.add("cli", event="raw", data={"line": redact(line[:300])})

    def _read_stderr(self):
        for line in self.proc.stderr:
            line = redact(line.rstrip())
            if not line:
                continue
            self.stderr_lines.append(line)
            with open(self.dir / "stderr.log", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self.add("stderr", line=line[:400])

    # ----------------------------------------------------- cancel / kill
    def cancel(self) -> bool:
        if self.state != "running" or not self.proc:
            return False
        self.cancelled = True
        self.add("cancel", message="cancel requested by user - terminating process group")
        self._terminate()
        return True

    def _terminate(self):
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            if IS_POSIX:
                os.killpg(p.pid, signal.SIGTERM)
            else:
                p.send_signal(signal.CTRL_BREAK_EVENT)
        except (ProcessLookupError, OSError):
            pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL) if IS_POSIX else p.kill()
            except (ProcessLookupError, OSError):
                pass
            self.add("killed", message="process did not exit after SIGTERM; sent SIGKILL")

    # ------------------------------------------------------------ finalize
    def _finalize(self):
        from .respond import build_response  # local import to avoid a cycle
        self.ended_at = now_iso()
        self.add("process_exited", exit_code=self.exit_code, meaning=self.tool.exit_codes.get(self.exit_code, "terminated/unknown"))
        self.add("stage", stage="verify", status="active")

        result, validation_error, file_sha = None, None, None
        if self.out_path.exists():
            raw = self.out_path.read_bytes()
            file_sha = hashlib.sha256(raw).hexdigest()
            try:
                result = json.loads(raw)
                ResultModel.model_validate(result)
            except (ValueError, ValidationError) as exc:
                validation_error = f"output failed validation: {str(exc)[:200]}"
                result = None
        done_events = [e for e in self.events if e["kind"] == "cli" and e["event"] == "done"]
        reported_sha = done_events[-1]["data"].get("sha256") if done_events else None
        sha_verified = bool(file_sha and reported_sha and file_sha == reported_sha)
        event_pages = sum(1 for e in self.events if e["kind"] == "cli" and e["event"] == "page")
        pages_consistent = result is not None and len(result.get("pages", [])) == event_pages

        status, reason = self._decide_status(result, validation_error, sha_verified)
        self.state = "finished"
        receipt = {
            "run_id": self.id,
            "replay_of": self.replay_of,
            "tool": {"name": self.tool.name, "module": self.tool.module},
            "argv": self.display_argv(),
            "shell": False,
            "working_dir": f"runs/{self.id}",
            "pid": self.proc.pid if self.proc else None,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": int((time.monotonic() - self.t0) * 1000),
            "exit_code": self.exit_code,
            "exit_meaning": self.tool.exit_codes.get(self.exit_code, "terminated/unknown"),
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "stdout_events": self.stdout_events,
            "stderr_lines": len(self.stderr_lines),
            "stderr_tail": self.stderr_lines[-8:],
            "output_file": "result.json" if self.out_path.exists() else None,
            "output_sha256": file_sha,
            "cli_reported_sha256": reported_sha,
            "sha256_verified": sha_verified,
            "page_events_match_output": pages_consistent,
            "validation_error": validation_error,
            "status": status,
            "status_reason": reason,
            "page_fingerprints": [{"url": p["url"], "status": p["status_code"], "sha256": p["sha256"]} for p in (result or {}).get("pages", [])],
            "sandbox": {"env_vars_passed": sorted(_sandbox_env().keys()), "rlimits": RLIMITS if IS_POSIX else "n/a on Windows",
                        "process_group": True, "hard_timeout_s": config.HARD_TIMEOUT_S},
        }
        (self.dir / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        self.final = build_response(self, result, receipt)
        self.add("stage", stage="respond", status="done")
        self.add("final", status=status)
        self.done.set()

    def _decide_status(self, result, validation_error, sha_verified):
        code = self.exit_code
        n = len((result or {}).get("pages", []))
        if self.cancelled:
            return ("CANCELLED_PARTIAL", f"cancelled by user after {n} page(s); showing checkpointed data") if n else ("CANCELLED", "cancelled by user before any page was crawled")
        if self.timed_out:
            return ("TIMED_OUT_PARTIAL", f"hard timeout after {n} page(s); showing checkpointed data") if n else ("TIMED_OUT", "hard timeout before any page was crawled")
        if validation_error:
            return "FAILED", validation_error
        if code == 0 and result and n and sha_verified:
            return "SUCCESS", "exit code 0, valid JSON output, output hash verified"
        if code == 0 and result and n:
            return "PARTIAL", "exit code 0 but output hash could not be verified"
        if code == 3:
            return "PARTIAL", f"crawler reported partial result ({n} page(s); some pages failed or time budget ran out)"
        if code == 4:
            fatal = (result or {}).get("fatal_error")
            return "FAILED", "site unreachable" + (f": {fatal}" if fatal else "")
        if code == 5:
            return "BLOCKED", "robots.txt disallows crawling this URL"
        if code == 6:
            return "BLOCKED", "URL rejected by security policy (SSRF / scheme / port guard)"
        if code == 2:
            return "FAILED", "CLI rejected the arguments"
        return "FAILED", f"CLI exited with code {code}"


class RunManager:
    def __init__(self):
        self.runs: dict[str, Run] = {}
        self.sem = threading.BoundedSemaphore(config.MAX_CONCURRENT_RUNS)

    def start(self, task: TaskSpec, replay_of: str | None = None) -> Run | None:
        if not self.sem.acquire(blocking=False):
            return None
        tool = REGISTRY["sitecrawl"]
        run = Run(task, tool, replay_of)
        self.runs[run.id] = run
        run.add("stage", stage="understand", status="done", interpreted_by=task.interpreted_by)
        run.add("stage", stage="plan", status="done", tool=tool.name)
        run.add("stage", stage="validate", status="done", checks=["URL syntax + scheme", "server-side limits applied", "approved tool only", "argv list (no shell)"])
        run.start(on_finish=lambda r: self.sem.release())
        return run

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)
