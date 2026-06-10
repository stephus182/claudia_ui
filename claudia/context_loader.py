"""
Loads context.md and principles.md, computes a SHA-256 hash for integrity
tracking, and watches for file changes via watchdog so a running session
can hot-reload without restart.
"""

import hashlib
import logging
from pathlib import Path
from threading import Event
from typing import Callable

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

log = logging.getLogger(__name__)

_CONTEXT_HEADER = "# ROLE & CONTEXT\n\n"
_PRINCIPLES_HEADER = "\n\n# TRADING PRINCIPLES & STRATEGIES\n\n"


class ContextLoader:
    """
    Manages the two user-written documents that form ClaudIA's system prompt.

    Documents are loaded from CLAUDIA_DOCS_PATH (default: docs/).
    Both files must exist; missing files produce a clear error.
    """

    def __init__(self, docs_path: str | Path = "docs"):
        self.docs_path = Path(docs_path)
        self._context_path = self.docs_path / "context.md"
        self._principles_path = self.docs_path / "principles.md"
        self._observer: Observer | None = None
        self._reload_callback: Callable[[str, str], None] | None = None

    def load_system_prompt(self) -> str:
        """Return concatenated context + principles as a single system prompt string."""
        context = self._read_required(self._context_path, "context.md")
        principles = self._read_required(self._principles_path, "principles.md")
        return _CONTEXT_HEADER + context + _PRINCIPLES_HEADER + principles

    def compute_hash(self) -> str:
        """SHA-256 of context.md + principles.md content, for integrity tracking."""
        combined = (
            self._context_path.read_text(encoding="utf-8", errors="replace")
            + self._principles_path.read_text(encoding="utf-8", errors="replace")
        )
        return hashlib.sha256(combined.encode()).hexdigest()

    def start_watching(self, on_reload: Callable[[str, str], None]) -> None:
        """
        Start a background watchdog thread. Calls on_reload(filename, new_prompt)
        whenever context.md or principles.md changes.  If a watcher is already
        running (e.g. a previous session's) it is stopped first so macOS FSEvents
        doesn't raise "already scheduled" on the same path.
        """
        self.stop_watching()
        self._reload_callback = on_reload
        handler = _DocChangeHandler(
            watched={self._context_path, self._principles_path},
            on_change=self._handle_change,
        )
        self._observer = Observer()
        self._observer.schedule(handler, str(self.docs_path), recursive=False)
        self._observer.start()
        log.info("Watching %s for document changes", self.docs_path)

    def stop_watching(self) -> None:
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join()

    def _handle_change(self, changed_file: str) -> None:
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
    def __init__(self, watched: set[Path], on_change: Callable[[str], None]):
        super().__init__()
        self._watched = {str(p) for p in watched}
        self._on_change = on_change
        self._debounce: dict[str, Event] = {}

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and event.src_path in self._watched:
            log.info("Document changed: %s", event.src_path)
            self._on_change(Path(event.src_path).name)

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)
