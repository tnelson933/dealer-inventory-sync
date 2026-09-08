#!/usr/bin/env python3
"""Windows background agent for synchronizing dealer Excel inventory files."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import queue
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

EXCEL_EXTENSIONS = {".xlsx", ".xls"}
STOP_ITEM = object()


def application_directory() -> Path:
    """Return the directory containing the script or packaged executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_local_path(base_directory: Path, value: str) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else base_directory / path


@dataclass(frozen=True)
class AgentConfig:
    watch_folder: Path
    api_url: str
    api_key: str
    dealer_id: int
    fallback_interval_seconds: float
    settings_poll_interval_seconds: float
    log_file: Path
    state_file: Path
    request_timeout_seconds: float
    retry_attempts: int
    retry_base_delay_seconds: float
    file_stability_checks: int
    file_stability_delay_seconds: float

    @classmethod
    def load(cls, config_path: Path) -> "AgentConfig":
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError(
                f"Configuration file not found: {config_path}. "
                "Copy config.example.json to config.json and edit it."
            ) from error
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Configuration file contains invalid JSON at line {error.lineno}."
            ) from error

        if not isinstance(raw, dict):
            raise ValueError("Configuration must be a JSON object.")

        base_directory = config_path.parent
        required_strings = ("watch_folder", "api_url", "api_key")
        for field in required_strings:
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise ValueError(f"Configuration field '{field}' is required.")

        api_url = raw["api_url"].strip()
        parsed_url = urlparse(api_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("'api_url' must be a complete HTTP or HTTPS URL.")
        if parsed_url.scheme != "https" and parsed_url.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError(
                "'api_url' must use HTTPS except when connecting to localhost."
            )
        if "[my-replit-app]" in api_url:
            raise ValueError("Replace the placeholder in 'api_url' with the real API URL.")

        api_key = raw["api_key"].strip()
        if api_key.lower().startswith(("replace", "your_")):
            raise ValueError("Replace the placeholder in 'api_key' with the dealer API key.")

        def positive_number(name: str, default: float) -> float:
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"Configuration field '{name}' must be greater than zero.")
            return float(value)

        def positive_integer(name: str, default: int) -> int:
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"Configuration field '{name}' must be a positive integer.")
            return value

        interval_minutes = positive_number("fallback_interval_minutes", 15)
        return cls(
            watch_folder=resolve_local_path(base_directory, raw["watch_folder"].strip()),
            api_url=api_url,
            api_key=api_key,
            dealer_id=positive_integer("dealer_id", 0),
            fallback_interval_seconds=interval_minutes * 60,
            settings_poll_interval_seconds=positive_number(
                "settings_poll_interval_minutes", 5
            )
            * 60,
            log_file=resolve_local_path(
                base_directory, str(raw.get("log_file", "dealer_sync.log"))
            ),
            state_file=resolve_local_path(
                base_directory, str(raw.get("state_file", "dealer_sync_state.json"))
            ),
            request_timeout_seconds=positive_number("request_timeout_seconds", 60),
            retry_attempts=positive_integer("retry_attempts", 5),
            retry_base_delay_seconds=positive_number("retry_base_delay_seconds", 5),
            file_stability_checks=positive_integer("file_stability_checks", 3),
            file_stability_delay_seconds=positive_number(
                "file_stability_delay_seconds", 2
            ),
        )


def configure_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dealer-sync-agent")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    if not getattr(sys, "frozen", False):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

    return logger


class UploadState:
    """Thread-safe record of successfully uploaded file versions."""

    def __init__(self, path: Path, logger: logging.Logger) -> None:
        self.path = path
        self.logger = logger
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._items = data
            else:
                self.logger.warning("State file was not a JSON object; starting fresh")
        except (OSError, json.JSONDecodeError) as error:
            self.logger.warning("Could not read state file; starting fresh: %s", error)

    def has_uploaded(self, path: Path, fingerprint: str) -> bool:
        with self._lock:
            entry = self._items.get(str(path.resolve()))
            return bool(entry and entry.get("fingerprint") == fingerprint)

    def mark_uploaded(self, path: Path, fingerprint: str) -> None:
        with self._lock:
            self._items[str(path.resolve())] = {
                "fingerprint": fingerprint,
                "uploaded_at_epoch": time.time(),
            }
            self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(self._items, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary_path.replace(self.path)


class InventoryUploader:
    def __init__(
        self,
        config: AgentConfig,
        state: UploadState,
        logger: logging.Logger,
        stop_event: threading.Event,
    ) -> None:
        self.config = config
        self.state = state
        self.logger = logger
        self.stop_event = stop_event
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-api-key": config.api_key,
                "User-Agent": "DealerInventorySync/1.0",
            }
        )

    def fetch_sync_frequency(self) -> float | None:
        api_base = self.config.api_url.rstrip("/").rsplit("/", 1)[0]
        settings_url = f"{api_base}/dealers/{self.config.dealer_id}/settings"
        try:
            response = self.session.get(
                settings_url,
                timeout=self.config.request_timeout_seconds,
            )
            if response.status_code != 200:
                self.logger.warning(
                    "Could not refresh dealer settings (HTTP %d); keeping current interval",
                    response.status_code,
                )
                return None
            payload = response.json()
            minutes = payload.get("syncFrequencyMinutes")
            if (
                isinstance(minutes, bool)
                or not isinstance(minutes, (int, float))
                or minutes < 1
                or minutes > 10080
            ):
                raise ValueError("syncFrequencyMinutes is outside the supported range")
            return float(minutes) * 60
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as error:
            self.logger.warning(
                "Could not refresh dealer settings; keeping current interval: %s",
                error,
            )
            return None

    @staticmethod
    def is_excel_file(path: Path) -> bool:
        return path.suffix.lower() in EXCEL_EXTENSIONS and not path.name.startswith("~$")

    def wait_until_stable(self, path: Path) -> tuple[int, int] | None:
        previous: tuple[int, int] | None = None
        stable_count = 0

        while stable_count < self.config.file_stability_checks:
            try:
                stat = path.stat()
                current = (stat.st_size, stat.st_mtime_ns)
            except (FileNotFoundError, PermissionError, OSError) as error:
                self.logger.warning("File is not ready yet: %s (%s)", path, error)
                return None

            if current == previous and current[0] > 0:
                stable_count += 1
            else:
                previous = current
                stable_count = 0
            if self.stop_event.wait(self.config.file_stability_delay_seconds):
                return None

        return previous

    @staticmethod
    def fingerprint(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def upload_if_changed(self, path: Path) -> bool:
        path = path.resolve()
        if not self.is_excel_file(path):
            return True
        if self.stop_event.is_set():
            return False

        stable_version = self.wait_until_stable(path)
        if stable_version is None:
            return False

        try:
            content = path.read_bytes()
            final_stat = path.stat()
        except (FileNotFoundError, PermissionError, OSError) as error:
            self.logger.warning("Could not read stable file %s: %s", path, error)
            return False

        if (len(content), final_stat.st_mtime_ns) != stable_version:
            self.logger.info("File changed while being read; deferring upload: %s", path)
            return False

        fingerprint = self.fingerprint(content)
        if self.state.has_uploaded(path, fingerprint):
            self.logger.debug("Skipping unchanged file: %s", path)
            return True

        for attempt in range(1, self.config.retry_attempts + 1):
            if self.stop_event.is_set():
                self.logger.info("Upload cancelled during shutdown: %s", path.name)
                return False
            try:
                self.logger.info(
                    "Uploading %s (attempt %d/%d)",
                    path.name,
                    attempt,
                    self.config.retry_attempts,
                )
                response = self.session.post(
                    self.config.api_url,
                    headers={"x-idempotency-key": fingerprint},
                    files={
                        "file": (
                            path.name,
                            io.BytesIO(content),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                    timeout=self.config.request_timeout_seconds,
                )

                if 200 <= response.status_code < 300:
                    self.state.mark_uploaded(path, fingerprint)
                    self.logger.info(
                        "Upload succeeded for %s (HTTP %d)",
                        path.name,
                        response.status_code,
                    )
                    return True

                response_summary = response.text.strip().replace("\r", " ").replace("\n", " ")
                response_summary = response_summary[:500]
                retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
                if not retryable:
                    self.logger.error(
                        "Upload rejected for %s (HTTP %d): %s",
                        path.name,
                        response.status_code,
                        response_summary,
                    )
                    return False

                self.logger.warning(
                    "Retryable API response for %s (HTTP %d): %s",
                    path.name,
                    response.status_code,
                    response_summary,
                )
            except (requests.RequestException, OSError) as error:
                self.logger.warning("Upload attempt failed for %s: %s", path.name, error)

            if attempt < self.config.retry_attempts:
                delay = self.config.retry_base_delay_seconds * (2 ** (attempt - 1))
                delay += random.uniform(0, min(1.0, delay * 0.1))
                self.logger.info("Retrying %s in %.1f seconds", path.name, delay)
                if self.stop_event.wait(delay):
                    self.logger.info("Retry cancelled during shutdown: %s", path.name)
                    return False

        self.logger.error(
            "Upload failed for %s after %d attempts; fallback scan will retry later",
            path.name,
            self.config.retry_attempts,
        )
        return False

    def close(self) -> None:
        self.session.close()


class SyncCoordinator:
    def __init__(self, uploader: InventoryUploader, logger: logging.Logger) -> None:
        self.uploader = uploader
        self.logger = logger
        self.work_queue: queue.Queue[Path | object] = queue.Queue()
        self._pending: set[Path] = set()
        self._pending_lock = threading.Lock()
        self._accepting_work = True

    def enqueue(self, path: Path) -> None:
        resolved_path = path.resolve()
        if not self.uploader.is_excel_file(resolved_path):
            return
        with self._pending_lock:
            if not self._accepting_work:
                return
            if resolved_path in self._pending:
                return
            self._pending.add(resolved_path)
        self.work_queue.put(resolved_path)
        self.logger.info("Queued inventory file: %s", resolved_path)

    def scan_folder(self, watch_folder: Path) -> None:
        self.logger.info("Running fallback scan: %s", watch_folder)
        try:
            paths = sorted(
                path for path in watch_folder.iterdir() if path.is_file()
            )
        except OSError as error:
            self.logger.error("Could not scan watch folder: %s", error)
            return
        for path in paths:
            self.enqueue(path)

    def worker(self) -> None:
        while True:
            item = self.work_queue.get()
            try:
                if item is STOP_ITEM:
                    return
                path = item
                assert isinstance(path, Path)
                if not self.uploader.stop_event.is_set():
                    self.uploader.upload_if_changed(path)
            except Exception:
                self.logger.exception("Unexpected error while processing %s", item)
            finally:
                if isinstance(item, Path):
                    with self._pending_lock:
                        self._pending.discard(item)
                self.work_queue.task_done()

    def finish_after_pending(self) -> None:
        self.work_queue.put(STOP_ITEM)

    def stop_accepting(self) -> None:
        with self._pending_lock:
            self._accepting_work = False


class ExcelEventHandler(FileSystemEventHandler):
    def __init__(self, coordinator: SyncCoordinator) -> None:
        super().__init__()
        self.coordinator = coordinator

    def _queue_event(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.coordinator.enqueue(Path(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        self._queue_event(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._queue_event(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory and hasattr(event, "dest_path"):
            self.coordinator.enqueue(Path(event.dest_path))


def fallback_scheduler(
    coordinator: SyncCoordinator,
    uploader: InventoryUploader,
    watch_folder: Path,
    initial_interval_seconds: float,
    settings_poll_interval_seconds: float,
    stop_event: threading.Event,
) -> None:
    interval_seconds = initial_interval_seconds
    last_scan = time.monotonic()
    while not stop_event.is_set():
        refreshed_interval = uploader.fetch_sync_frequency()
        if refreshed_interval is not None and refreshed_interval != interval_seconds:
            interval_seconds = refreshed_interval
            uploader.logger.info(
                "Server sync frequency updated to %.1f minutes",
                interval_seconds / 60,
            )

        elapsed = time.monotonic() - last_scan
        if elapsed >= interval_seconds:
            coordinator.scan_folder(watch_folder)
            last_scan = time.monotonic()

        remaining = max(1.0, interval_seconds - (time.monotonic() - last_scan))
        stop_event.wait(min(settings_poll_interval_seconds, remaining))


def run() -> int:
    base_directory = application_directory()
    config_path = base_directory / "config.json"

    try:
        config = AgentConfig.load(config_path)
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    logger = configure_logging(config.log_file)
    config.watch_folder.mkdir(parents=True, exist_ok=True)
    state = UploadState(config.state_file, logger)
    shutdown_event = threading.Event()
    upload_cancel_event = threading.Event()
    uploader = InventoryUploader(config, state, logger, upload_cancel_event)
    coordinator = SyncCoordinator(uploader, logger)

    worker_thread = threading.Thread(
        target=coordinator.worker,
        name="upload-worker",
        daemon=False,
    )
    scheduler_thread = threading.Thread(
        target=fallback_scheduler,
        args=(
            coordinator,
            uploader,
            config.watch_folder,
            config.fallback_interval_seconds,
            config.settings_poll_interval_seconds,
            shutdown_event,
        ),
        name="fallback-scan",
        daemon=False,
    )
    observer = Observer()
    observer.schedule(
        ExcelEventHandler(coordinator),
        str(config.watch_folder),
        recursive=False,
    )

    def request_shutdown(_signum: int, _frame: Any) -> None:
        logger.info("Shutdown requested")
        shutdown_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    logger.info(
        "Dealer sync agent starting; watching %s; fallback interval %.1f minutes",
        config.watch_folder,
        config.fallback_interval_seconds / 60,
    )
    worker_thread.start()
    scheduler_thread.start()
    observer.start()
    coordinator.scan_folder(config.watch_folder)

    try:
        while not shutdown_event.wait(1):
            pass
    finally:
        coordinator.stop_accepting()
        shutdown_event.set()
        observer.stop()
        observer.join()
        scheduler_thread.join()
        logger.info("Finishing queued uploads before shutdown")
        coordinator.finish_after_pending()
        worker_thread.join()
        uploader.close()
        logger.info("Dealer sync agent stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(run())