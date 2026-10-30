"""Shared control surface for media download engines."""

from __future__ import annotations

from abc import ABC, abstractmethod


class DownloadEngine(ABC):
    """Preview, select files, then download.

    The NZB engine sits beside the torrent engine and implements these methods.
    """

    source_type: str

    @abstractmethod
    def preview(self, source: str) -> dict:
        """Inspect a source and return a file list. Must not write payload bytes."""

    @abstractmethod
    def preview_status(self, preview_id: str) -> dict:
        pass

    @abstractmethod
    def start(self, preview_id: str, file_indexes: list[int] | None) -> dict:
        pass

    @abstractmethod
    def jobs(self) -> list[dict]:
        pass

    @abstractmethod
    def job(self, job_id: str) -> dict:
        pass

    @abstractmethod
    def update(self, job_id: str, patch: dict) -> dict:
        pass

    @abstractmethod
    def remove(self, job_id: str) -> None:
        pass

    @abstractmethod
    def settings(self) -> dict:
        pass

    @abstractmethod
    def update_settings(self, patch: dict) -> dict:
        pass

    @abstractmethod
    def shutdown(self) -> None:
        pass


class SourceRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, DownloadEngine] = {}

    def register(self, engine: DownloadEngine) -> None:
        self._engines[engine.source_type] = engine

    def get(self, source_type: str) -> DownloadEngine:
        try:
            return self._engines[source_type]
        except KeyError as exc:
            raise KeyError(f"unknown source {source_type}") from exc

    def torrent(self) -> DownloadEngine:
        return self.get("torrent")
