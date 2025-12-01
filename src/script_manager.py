import json
import os
import threading
import traceback
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


def _iso_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


class ProgressStore:
    """Lightweight persistence for processed items per script key."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # If the file is corrupted, start fresh but do not crash the app
            return {}

    def _write(self) -> None:
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._state, f)
        os.replace(tmp_path, self.path)

    def mark_processed(self, key: str, item: str) -> None:
        timestamp = _iso_now()
        with self._lock:
            entry = self._state.setdefault(key, {"items": {}, "updated_at": None})
            entry["items"][item] = timestamp
            entry["updated_at"] = timestamp
            self._write()

    def is_processed(self, key: str, item: str) -> bool:
        with self._lock:
            return item in self._state.get(key, {}).get("items", {})

    def count(self, key: str) -> int:
        with self._lock:
            return len(self._state.get(key, {}).get("items", {}))

    def reset(self, key: str) -> None:
        with self._lock:
            if key in self._state:
                self._state.pop(key)
                self._write()


class ScriptJob:
    def __init__(
        self,
        script_name: str,
        params: Dict[str, Any],
        progress_key: str,
        log_path: str,
        job_id: Optional[str] = None,
    ) -> None:
        self.id = job_id or str(uuid.uuid4())
        self.script_name = script_name
        self.params = params
        self.progress_key = progress_key
        self.log_path = log_path
        self.status = "queued"
        self.started_at: Optional[str] = None
        self.ended_at: Optional[str] = None
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "script": self.script_name,
            "params": self.params,
            "progress_key": self.progress_key,
            "log_path": self.log_path,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class ScriptContext:
    """Context passed to individual script runners."""

    def __init__(
        self,
        job: ScriptJob,
        progress: ProgressStore,
        log_fn: Callable[[str], None],
    ) -> None:
        self.job = job
        self.progress = progress
        self._log_fn = log_fn

    def log(self, message: str) -> None:
        self._log_fn(message)

    def should_stop(self) -> bool:
        return self.job.stop_event.is_set()

    def is_processed(self, item: str) -> bool:
        return self.progress.is_processed(self.job.progress_key, item)

    def mark_processed(self, item: str) -> None:
        self.progress.mark_processed(self.job.progress_key, item)

    def processed_count(self) -> int:
        return self.progress.count(self.job.progress_key)


class ScriptManager:
    """Runs allowed scripts in background threads and keeps lightweight state."""

    def __init__(
        self,
        state_dir: str,
        scripts: Dict[str, Dict[str, Any]],
    ) -> None:
        self.state_dir = state_dir
        self.log_dir = os.path.join(state_dir, "logs")
        self.jobs_file = os.path.join(state_dir, "jobs.json")
        os.makedirs(self.log_dir, exist_ok=True)

        self.progress = ProgressStore(os.path.join(state_dir, "progress.json"))
        self.scripts = scripts
        self._lock = threading.Lock()
        self.jobs: Dict[str, ScriptJob] = {}
        self._load_jobs()

    def _load_jobs(self) -> None:
        if not os.path.exists(self.jobs_file):
            return
        try:
            with open(self.jobs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for job_id, payload in data.items():
                job = ScriptJob(
                    payload["script"],
                    payload.get("params", {}),
                    payload.get("progress_key", payload["script"]),
                    payload["log_path"],
                    job_id=job_id,
                )
                job.status = payload.get("status", "stopped")
                job.started_at = payload.get("started_at")
                job.ended_at = payload.get("ended_at")
                # Any job that was previously "running" was interrupted by a restart.
                if job.status == "running":
                    job.status = "stopped"
                self.jobs[job_id] = job
        except Exception:
            # Avoid crashing if the file is missing or corrupted
            return

    def _persist_jobs(self) -> None:
        payload = {job_id: job.to_dict() for job_id, job in self.jobs.items()}
        tmp_path = f"{self.jobs_file}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, self.jobs_file)

    def _make_progress_key(self, script_name: str, params: Dict[str, Any]) -> str:
        script = self.scripts[script_name]
        key_fn = script.get("progress_key_fn")
        if key_fn:
            return key_fn(params)
        return f"{script_name}:{json.dumps(params, sort_keys=True)}"

    def _log_path_for(self, job_id: str, script_name: str) -> str:
        safe_script = "".join(ch for ch in script_name if ch.isalnum() or ch in ("-", "_"))
        return os.path.join(self.log_dir, f"{safe_script}-{job_id}.log")

    def start_job(self, script_name: str, params: Optional[Dict[str, Any]] = None) -> ScriptJob:
        if script_name not in self.scripts:
            raise ValueError("Unknown script")
        params = params or {}
        progress_key = self._make_progress_key(script_name, params)
        job = ScriptJob(
            script_name,
            params,
            progress_key,
            log_path="",
        )
        job.log_path = self._log_path_for(job.id, script_name)
        with self._lock:
            # Prevent running the same script twice concurrently
            for existing in self.jobs.values():
                if existing.script_name == script_name and existing.status == "running":
                    raise RuntimeError(f"Script '{script_name}' is already running.")
            self.jobs[job.id] = job
            self._persist_jobs()
            thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
            job.thread = thread
            thread.start()
        return job

    def _run_job(self, job: ScriptJob) -> None:
        script = self.scripts[job.script_name]
        runner = script["runner"]
        job.status = "running"
        job.started_at = _iso_now()
        self._persist_jobs()

        os.makedirs(os.path.dirname(job.log_path), exist_ok=True)
        with open(job.log_path, "a", encoding="utf-8") as log_file:
            def log_fn(message: str) -> None:
                log_file.write(f"[{_iso_now()}] {message}\n")
                log_file.flush()

            ctx = ScriptContext(job=job, progress=self.progress, log_fn=log_fn)
            log_fn(f"Starting script '{job.script_name}' with params {job.params}")
            try:
                runner(ctx, **job.params)
                if job.stop_event.is_set():
                    job.status = "stopped"
                    log_fn("Stop requested; exiting early.")
                else:
                    job.status = "completed"
                    log_fn("Script finished successfully.")
            except Exception:
                job.status = "failed"
                log_fn("Script failed:\n" + traceback.format_exc())
            finally:
                job.ended_at = _iso_now()
                self._persist_jobs()

    def stop_job(self, job_id: str) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job or job.status != "running":
                return False
            job.stop_event.set()
            return True

    def get_job(self, job_id: str) -> Optional[ScriptJob]:
        return self.jobs.get(job_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        jobs = [job.to_dict() for job in self.jobs.values()]
        # Sort newest first
        jobs.sort(key=lambda j: j.get("started_at") or "", reverse=True)
        return jobs

    def job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.jobs.get(job_id)
        if not job:
            return None
        data = job.to_dict()
        data["processed_count"] = self.progress.count(job.progress_key)
        return data

    def read_log(self, job_id: str, offset: int = 0) -> Optional[Dict[str, Any]]:
        job = self.jobs.get(job_id)
        if not job:
            return None
        if not os.path.exists(job.log_path):
            return {"data": "", "offset": 0, "status": job.status}
        with open(job.log_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        return {"data": data, "offset": new_offset, "status": job.status}
