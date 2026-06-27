"""
Loads context.md and principles.md, computes a SHA-256 hash for integrity
tracking, and watches for file changes via watchdog so a running session
can hot-reload without restart.

File watching uses the watchdog library with a module-level shared Observer.
On macOS, watchdog uses FSEventsObserver (kernel-level kqueue/FSEvents);
on Linux, InotifyObserver; on Windows, ReadDirectoryChangesW.
A shared Observer is required because macOS FSEvents raises RuntimeError
"already scheduled" if the same path is added to multiple Observer instances.

Source (watchdog): https://watchdog.readthedocs.io/en/stable/
Source (watchdog Observer): https://watchdog.readthedocs.io/en/stable/api.html
"""

import hashlib
import logging
from contextlib import suppress
from pathlib import Path
import threading
from typing import Callable

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

log = logging.getLogger(__name__)

# Single long-lived Observer shared across all ContextLoader instances.
# Creating + stopping Observers per session causes macOS FSEvents to raise
# "already scheduled" because the kernel-level watch isn't freed between
# stop() and the next Observer's start(). Scheduling/unscheduling on one
# persistent Observer avoids this entirely.
_shared_observer: Observer | None = None


def _get_shared_observer() -> Observer:
    global _shared_observer
    if _shared_observer is None or not _shared_observer.is_alive():
        _shared_observer = Observer()
        _shared_observer.daemon = True
        _shared_observer.start()
    return _shared_observer

_CONTEXT_HEADER = "# ROLE & CONTEXT\n\n"
_PRINCIPLES_HEADER = "\n\n# TRADING PRINCIPLES & STRATEGIES\n\n"


class ContextLoader:
    """
    Manages the two user-written documents that form ClaudIA's system prompt.

    Documents are loaded from CLAUDIA_DOCS_PATH (default: docs/).
    Optional context_text / principles_text override local file reads (used
    when Drive versions are fetched at session start). A local file-change
    event clears the overrides so hot-reload reverts to the local files.
    """

    def __init__(
        self,
        docs_path: str | Path = "docs",
        context_text: str | None = None,
        principles_text: str | None = None,
    ) -> None:
        self.docs_path = Path(docs_path)
        self._context_path = self.docs_path / "context.md"
        self._principles_path = self.docs_path / "principles.md"
        self._context_override: str | None = context_text
        self._principles_override: str | None = principles_text
        self._watch: ObservedWatch | None = None
        self._reload_callback: Callable[[str, str], None] | None = None

    def _get_text(self, override: str | None, path: Path, name: str) -> str:
        if override is not None:
            return override.strip()  # match _read_required's strip() for hash stability
        return self._read_required(path, name)

    def get_effective_texts(self) -> tuple[str, str]:
        """Return (context_text, principles_text) as actually loaded (Drive override or file)."""
        return (
            self._get_text(self._context_override, self._context_path, "context.md"),
            self._get_text(self._principles_override, self._principles_path, "principles.md"),
        )

    def load_system_prompt(self) -> str:
        """Return concatenated context + principles as a single system prompt string."""
        context, principles = self.get_effective_texts()
        return _CONTEXT_HEADER + context + _PRINCIPLES_HEADER + principles

    def compute_hash(self) -> str:
        """SHA-256 of context + principles content (stripped), for integrity tracking.

        Note: uses stripped content via _get_text/_read_required. Any session that stored
        a hash with the old raw-bytes path will see a one-time hash mismatch on upgrade;
        subsequent sessions compare stripped-vs-stripped and are stable.
        """
        context, principles = self.get_effective_texts()
        return hashlib.sha256((context + principles).encode()).hexdigest()

    def start_watching(self, on_reload: Callable[[str, str], None]) -> None:
        """
        Register a watchdog handler on the shared module-level Observer.
        Unschedules any previous watch for this instance first.
        Uses a shared Observer so macOS FSEvents never sees the same path
        added twice (which raises RuntimeError "already scheduled").
        """
        self.stop_watching()
        self._reload_callback = on_reload
        handler = _DocChangeHandler(
            watched={self._context_path, self._principles_path},
            on_change=self._handle_change,
        )
        obs = _get_shared_observer()
        self._watch = obs.schedule(handler, str(self.docs_path), recursive=False)
        log.info("Watching %s for document changes", self.docs_path)

    def stop_watching(self) -> None:
        """Unschedule the watchdog handler and clear the reload callback."""
        if self._watch is not None:
            with suppress(Exception):
                _get_shared_observer().unschedule(self._watch)
            self._watch = None
        self._reload_callback = None

    def _handle_change(self, changed_file: str) -> None:
        # Both overrides are cleared atomically regardless of which file changed —
        # local files become the sole source of truth after any edit.
        self._context_override = None
        self._principles_override = None
        if self._reload_callback:
            try:
                new_prompt = self.load_system_prompt()
                self._reload_callback(changed_file, new_prompt)
            except Exception as exc:
                log.error("Failed to reload documents after change: %s", exc)

    @staticmethod
    def _read_required(path: Path, name: str) -> str:
        if not path.exists():
            raise FileNotFoundError(
                f"Required document not found: {path}\n"
                f"Create docs/{name} to configure ClaudIA's {name.replace('.md', '')}."
            )
        return path.read_text(encoding="utf-8", errors="replace").strip()


class _DocChangeHandler(FileSystemEventHandler):
    """Watchdog handler that debounces filesystem events for the two watched docs.

    Source: https://watchdog.readthedocs.io/en/stable/api.html#watchdog.events.FileSystemEventHandler
    """

    _DEBOUNCE_SECS = 0.3

    def __init__(self, watched: set[Path], on_change: Callable[[str], None]):
        """Track watched paths (resolved absolute) and a callback for change events."""
        super().__init__()
        self._watched = {str(p.resolve()) for p in watched}
        self._on_change = on_change
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event: FileSystemEvent) -> None:
        """Trigger debounced callback when one of the watched files is saved."""
        if not event.is_directory and event.src_path in self._watched:
            log.info("Document changed: %s", event.src_path)
            self._schedule(Path(event.src_path).name)

    def on_created(self, event: FileSystemEvent) -> None:
        """Treat file creation as a modification (covers atomic save patterns like vim/nano)."""
        self.on_modified(event)

    def _schedule(self, filename: str) -> None:
        # Rapid saves (e.g. editor writing a temp file then renaming) fire multiple events.
        # Cancel any pending timer before starting a new one so the callback fires once,
        # 300ms after the last event.
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                self._DEBOUNCE_SECS, self._on_change, args=(filename,)
            )
            self._timer.daemon = True
            self._timer.start()
