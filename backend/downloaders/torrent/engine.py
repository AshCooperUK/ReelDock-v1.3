"""BitTorrent session: preview, queue, peers, web seeds, and shutdown."""

from __future__ import annotations

import json
import tempfile
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..base import DownloadEngine
from ...operations import Halted, Operations
from ...vpn.transport import torrent
from .bandwidth import SlotJob, TokenBucket, choose_active
from .bencode import bdecode_all, bencode
from .dht import DHTNode
from .lsd import build_packet, parse_packet, send_packet
from .media import ClassifiedFile, gate
from .metainfo import Magnet, Metainfo, MetainfoError, parse_magnet, parse_torrent, usable_trackers
from .pieces import PiecePicker
from .rename import sanitize, suggest_download, suggested_title
from .portmap import try_natpmp, try_upnp
from .storage import Storage, UnsafePath
from .tracker import TrackerError, announce
from .webseed import WebSeedError, fetch_range, file_urls
from .wire import Stream, WireError, open_inbound, open_outbound, pack_message, run_peer
from .mse import skey_matches

DEFAULT_SETTINGS = {
    "share_metadata": True,
    "download_limit_bps": 0,
    "upload_limit_bps": 0,
    "enable_dht": True,
    "enable_pex": True,
    "enable_lsd": True,
    "encryption": "prefer",
    "enable_upnp": True,
    "enable_natpmp": True,
    "enable_web_seeds": True,
    "max_active_downloads": 3,
    "max_active_seeds": 3,
    "share_ratio_limit": 2.0,
    "listen_port": 6881,
    "theme": "light",
}

BOOTSTRAP = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
]


class EngineError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _Rate:
    def __init__(self) -> None:
        self.samples: deque[tuple[float, int]] = deque()

    def add(self, amount: int) -> None:
        now = time.monotonic()
        self.samples.append((now, amount))
        while self.samples and now - self.samples[0][0] > 5:
            self.samples.popleft()

    def bps(self) -> float:
        return sum(amount for _stamp, amount in self.samples) / 5


@dataclass
class Preview:
    id: str
    source: str
    status: str = "reading-metadata"
    accepted: bool = False
    reason: str = ""
    meta: Metainfo | None = None
    result_files: tuple[ClassifiedFile, ...] = ()
    magnet: Magnet | None = None
    peers: deque = field(default_factory=deque)
    done: threading.Event = field(default_factory=threading.Event)
    found_name: str = ""
    found_checked: bool = False


class Job:
    def __init__(self, engine: "TorrentEngine", info_hash: bytes) -> None:
        self.engine = engine
        self.hash_bytes = info_hash
        self.id = info_hash.hex()
        self.meta: Metainfo | None = None
        self.info_bytes: bytes | None = None
        self.storage: Storage | None = None
        self.picker: PiecePicker | None = None
        self.selected: set[int] = set()
        self.phase = "download"
        self.user_paused = False
        self.io = False
        self.removed = False
        self.metadata_only = False
        self.downloaded = 0
        self.uploaded = 0
        self.payload_requests_sent = 0
        self.payload_requests_received = 0
        self.peer_queue: deque[tuple[str, int]] = deque()
        self.seen: set[tuple[str, int]] = set()
        self.known: list[tuple[str, int]] = []
        self.links: list[Stream] = []
        self.writers = 0
        self.active_peers = 0
        self.dialing = 0
        self.trackers: list[str] = []
        self.download_limit_bps = 0
        self.upload_limit_bps = 0
        self.ratio_limit: float | None = None
        self.enable_dht: bool | None = None
        self.enable_pex: bool | None = None
        self.enable_lsd: bool | None = None
        self.enable_web_seeds: bool | None = None
        self.error = ""
        self.web_error = ""
        self.tracker_error = ""
        self.stop_reason = ""
        self.completed_announced = False
        self.added = time.time()
        self.categories: dict[int, str] = {}
        self.save_as = ""
        self.metadata_note = ""
        self.metadata_done: set[int] = set()
        self.metadata_started: set[int] = set()
        self.metadata_choice: dict[int, dict] = {}
        self.down_rate = _Rate()
        self.up_rate = _Rate()
        self.down_bucket = TokenBucket()
        self.up_bucket = TokenBucket()
        self.listen_port = 0
        self._lock = threading.Lock()

    def add_peers(self, peers: list[tuple[str, int]]) -> None:
        with self._lock:
            for ip, port in peers:
                if not ip or not isinstance(port, int) or not (0 < port < 65536):
                    continue
                if (ip, port) in self.seen:
                    continue
                self.seen.add((ip, port))
                self.known.append((ip, port))
                self.peer_queue.append((ip, port))

    def exchange_peers(self) -> list[tuple[str, int]]:
        if not self.engine.flag(self, "enable_pex"):
            return []
        return list(self.known)[:50]

    @property
    def io(self):
        return self._io and not self.engine.global_paused() and (self.metadata_only or not self.engine._activity_held(self))

    @io.setter
    def io(self, value):
        self._io = value

    def wait_for_resume(self):
        if not self.metadata_only and self.engine._activity_held(self):return False
        while self.engine.global_paused() and not (self.removed or self.user_paused or self.engine.stop.is_set()):
            self.engine.stop.wait(0.2)
        return not (self.removed or self.user_paused or self.engine.stop.is_set() or not self.metadata_only and self.engine._activity_held(self))

    def take_download(self, amount: int, stop: threading.Event | None = None) -> bool:
        if not self.wait_for_resume():
            return False
        if not self.engine.global_down.consume(amount, self.engine.stop):
            return False
        if self.download_limit_bps:
            return self.down_bucket.consume(amount, self.engine.stop)
        return True

    def take_upload(self, amount: int, stop: threading.Event | None = None) -> bool:
        # Media payload uploading is disabled regardless of saved legacy limits.
        return False

    def note_download(self, amount: int) -> None:
        self.downloaded += amount
        self.down_rate.add(amount)

    def note_upload(self, amount: int) -> None:
        self.uploaded += amount
        self.up_rate.add(amount)

    def broadcast_have(self, index: int) -> None:
        packet = pack_message(4, index.to_bytes(4, "big"))
        for stream in list(self.links):
            try:
                stream.send(packet)
            except OSError:
                continue

    def publish(self) -> None:
        def allowed(*,fresh=False):
            return not self.removed and not self.engine.stop.is_set() and not self.engine._activity_held(self,fresh=fresh)
        if self.storage is None or not allowed(fresh=True):
            return
        with self._lock:self.writers+=1
        try:
            created = self.storage.publish_ready(self.engine.library_layout(),self.categories,allowed=allowed)
            hook = getattr(self.engine, "metadata", None)
            if created and allowed(fresh=True) and callable(hook):hook(self,created)
        except (OSError, UnsafePath) as exc:
            if allowed():self.error = str(exc)
        finally:
            with self._lock:self.writers=max(0,self.writers-1)

    def note_dht_port(self, port: int) -> None:
        return None

    def metadata_ready(self, info_bytes: bytes) -> None:
        self.engine.finish_metadata(self, info_bytes)

    def usable(self) -> tuple[list[str], list[str]]:
        return usable_trackers(self.trackers)

    def selected_complete(self) -> bool:
        if self.storage is None:
            return False
        done, total = self.storage.selected_bytes_done()
        return total > 0 and done >= total


class TorrentEngine(DownloadEngine):
    source_type = "torrent"

    def __init__(
        self,
        media_base: Path,
        data_dir: Path,
        operations: Operations | None = None,
        settings: dict | None = None,
        global_paused=None,
    ) -> None:
        self.global_paused = global_paused or (lambda: False)
        self.media_base = Path(media_base)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.operations = operations
        if operations is not None:
            operations.register(self)
        self.config = dict(DEFAULT_SETTINGS)
        self.config.update(self._read_settings())
        if settings:
            self.config.update(settings)
        self.global_down = TokenBucket(int(self.config["download_limit_bps"]))
        self.global_up = TokenBucket(int(self.config["upload_limit_bps"]))
        self.peer_id = b"-RD0100-" + bytes(
            b"0123456789abcdefghijklmnopqrstuvwxyz"[value % 36] for value in os.urandom(12)
        )
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[Job] = []
        self._previews: dict[str, Preview] = {}
        self._meta_jobs: dict[bytes, Job] = {}
        self._threads: list[threading.Thread] = []
        self.layout = None
        self.metadata = None
        self.port = 0
        self._tcp: socket.socket | None = None
        self.dht: DHTNode | None = None
        self.upnp_status = {"ok": False, "detail": "not attempted"}
        self.natpmp_status = {"ok": False, "detail": "not attempted"}
        self.lsd_status = {"ok": False, "detail": "not started"}
        self.startup_network = {key: self.config[key] for key in ("listen_port", "enable_lsd", "enable_upnp", "enable_natpmp")}
        self._bind()
        self._spawn(self._accept_loop, "reeldock-accept")
        self._spawn(self._schedule_loop, "reeldock-schedule")
        if self.config["enable_dht"]:
            self.dht = DHTNode()
            self._spawn(self._dht_loop, "reeldock-dht-query")
        if self.config["enable_lsd"]:
            self._spawn(self._lsd_loop, "reeldock-lsd")
        if self.config["enable_upnp"] or self.config["enable_natpmp"]:
            self._spawn(self._map_ports, "reeldock-portmap")
        self._resume()

    def shutdown(self) -> None:
        if self.stop.is_set():
            return
        self.stop.set()
        for job in list(self._order) + list(self._meta_jobs.values()):
            job.removed = True
            job.io = False
            for stream in list(job.links):
                try:
                    stream.sock.close()
                except OSError:
                    pass
        if self._tcp is not None:
            try:
                self._tcp.close()
            except OSError:
                pass
        if self.dht is not None:
            self.dht.close()
        for thread in list(self._threads):
            thread.join(timeout=2)

    def preview(self, source: str) -> dict:
        self._ensure_running()
        text = source.strip()
        if text.startswith("magnet:?"):
            return self._preview_magnet(text)
        if text.startswith("http://") or text.startswith("https://"):
            data = self._fetch_torrent(text)
            return self._preview_bytes(data, text)
        raise EngineError("Paste a .torrent http(s) URL or a magnet link")

    def preview_bytes(self, data: bytes, source: str = "bytes") -> dict:
        return self._preview_bytes(data, source)

    def preview_video(self, preview_id: str) -> tuple[str, str]:
        """Largest scanned video and the Save as name, for a metadata hover before download."""
        preview = self._previews.get(preview_id)
        if preview is None:
            raise EngineError("Unknown preview", 404)
        if preview.status == "reading-metadata" or preview.meta is None:
            raise EngineError("Still reading torrent metadata.", 409)
        videos = [item for item in preview.result_files if item.kind == "video" and item.selectable]
        if not videos:
            raise EngineError("This torrent has no video to match.")
        video = max(videos, key=lambda item: item.size)
        suggestion = suggest_download(preview.result_files, self.media_base, self.library_layout())
        name = preview.found_name if preview.found_name and not suggestion["folder"] else suggestion["name"]
        return video.path, name

    def preview_status(self, preview_id: str) -> dict:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise EngineError("Unknown preview", 404)
        return self._preview_dict(preview)

    def start(self, preview_id: str, file_indexes: list[int] | None, title: str | None = None) -> dict:
        from ...acquisition import file_held
        if file_held(self.data_dir):
            raise EngineError("Downloads are stopped. Resume downloads in Activity before starting another.", 409)
        self._ensure_running()
        preview = self._previews.get(preview_id)
        if preview is None:
            raise EngineError("Unknown preview", 404)
        if preview.status == "reading-metadata":
            raise EngineError("Still reading torrent metadata. Nothing is downloading.", 409)
        if preview.status != "ready" or preview.meta is None:
            raise EngineError(preview.reason or "This torrent was rejected", 400)
        chosen = self._selection(preview, file_indexes)
        from ...media_holds import native_blocked
        if native_blocked(self.data_dir,['native:torrent:'+preview.meta.info_hash.hex(),'native:torrent-job:'+preview.meta.info_hash.hex()],fresh=True):
            raise EngineError('This torrent was cleared. Use its explicit Start or Resume action to download it again.',409)
        existing = self._jobs.get(preview.meta.info_hash.hex())
        if existing is not None and not existing.removed:
            if getattr(existing,'_held_restore',False):return self.update(existing.id,{'paused':False})
            return self._job_dict(existing)
        job = self._open_job(preview.meta, chosen, _chosen_title(preview, title, chosen, self.media_base, self.library_layout()))
        self._save_job(job)
        return self._job_dict(job)

    def jobs(self) -> list[dict]:
        return [self._job_dict(job) for job in list(self._order) if not job.removed]

    def activity_key(self,job_id: str) -> str:
        job=self._jobs.get(job_id)
        if job is None or job.removed:raise EngineError('Unknown torrent',404)
        return 'native:torrent:'+job.hash_bytes.hex()

    def preview_activity_key(self,preview_id: str,file_indexes=None) -> str:
        preview=self._previews.get(preview_id)
        if preview is None:raise EngineError('Unknown preview',404)
        if preview.status!='ready' or preview.meta is None or not preview.accepted:raise EngineError(preview.reason or 'This torrent is not ready.')
        self._selection(preview,file_indexes)
        return 'native:torrent:'+preview.meta.info_hash.hex()

    def _activity_held(self,job: Job,*,fresh=False) -> bool:
        from ...media_holds import native_blocked
        return native_blocked(self.data_dir,['native:torrent:'+job.hash_bytes.hex(),'native:torrent-job:'+job.id],fresh=fresh)

    def job(self, job_id: str) -> dict:
        found = self._jobs.get(job_id)
        if found is None or found.removed:
            raise EngineError("Unknown torrent", 404)
        return self._job_dict(found)

    def update(self, job_id: str, patch: dict) -> dict:
        found = self._jobs.get(job_id)
        if found is None or found.removed:
            raise EngineError("Unknown torrent", 404)
        if "paused" in patch:
            if not patch['paused'] and self._activity_held(found,fresh=True):raise EngineError('This torrent was cleared. Use its explicit Resume action to download it again.',409)
            if not patch['paused'] and getattr(found,'_held_restore',False):
                previous=found
                found=self._open_job(previous.meta,previous.selected,previous.save_as,restored={**previous._held_state,'user_paused':False})
                with self._lock:
                    if previous in self._order:self._order.remove(previous)
            found.user_paused = bool(patch["paused"])
        if "download_limit_bps" in patch:
            found.download_limit_bps = _nonnegative(patch["download_limit_bps"])
            found.down_bucket.set_rate(found.download_limit_bps)
        if "upload_limit_bps" in patch:
            found.upload_limit_bps = _nonnegative(patch["upload_limit_bps"])
            found.up_bucket.set_rate(found.upload_limit_bps)
        if "share_ratio_limit" in patch:
            raw = patch["share_ratio_limit"]
            found.ratio_limit = None if raw is None or raw == "" else _ratio(raw)
        for name in ("enable_dht", "enable_pex", "enable_lsd", "enable_web_seeds"):
            if name in patch:
                setattr(found, name, bool(patch[name]))
        if "trackers" in patch:
            if not isinstance(patch["trackers"], list):
                raise EngineError("Trackers must be a list")
            found.trackers = [str(item).strip() for item in patch["trackers"] if str(item).strip()][:64]
        if "files" in patch:
            self._reselect(found, patch["files"])
        if "placement" in patch:
            placement = patch["placement"]
            if not isinstance(placement, dict):
                raise EngineError("Choose a movie or a TV show.")
            try:
                index = int(placement.get("index"))
            except (TypeError, ValueError) as exc:
                raise EngineError("Choose a movie or a TV show.") from exc
            value = placement.get("value")
            if value not in {"movie", "series"}:
                raise EngineError("Choose a movie or a TV show.")
            if found.meta is None or index not in {item.index for item in found.meta.files}:
                raise EngineError("That file is not part of this download.")
            found.categories[index] = value
            found.publish()
            if found.storage is not None and found.selected_complete():
                if found.storage.questions(found.categories):
                    found.phase = "choose"
                    found.stop_reason = ""
                else:
                    found.phase = "stopped"
                    found.stop_reason = "Download finished"
                found.io = False
        self._save_job(found)
        return self._job_dict(found)

    def remove(self, job_id: str, timeout: float = 20) -> None:
        found = self._jobs.get(job_id)
        if found is None or found.removed:
            raise EngineError("Unknown torrent", 404)
        found.removed = True
        found.user_paused = True
        found.io = False
        for stream in list(found.links):
            try:
                stream.sock.close()
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with found._lock:
                busy = found.writers
            if busy == 0:
                break
            time.sleep(0.05)
        else:
            raise EngineError("This torrent is still stopping.", 409)
        if found.storage is not None:
            found.storage.remove_work()
        state = self.data_dir / "torrents" / f"{found.id}.json"
        blob = self.data_dir / "torrents" / f"{found.id}.torrent"
        state.unlink(missing_ok=True)
        blob.unlink(missing_ok=True)
        with self._lock:
            self._jobs.pop(found.id, None)
            if found in self._order:
                self._order.remove(found)

    def library_layout(self) -> dict:
        layout = self.layout() if callable(self.layout) else None
        if not isinstance(layout, dict):
            layout = {}
        profile = layout.get("profile") if layout.get("profile") in {"emby", "plex", "kodi"} else "emby"
        return {
            "profile": profile,
            "movies": layout.get("movies") or "Movies",
            "series": layout.get("series") or "TV Shows",
        }

    def settings(self) -> dict:
        body = dict(self.config)
        body["restart_required"] = any(self.config[key] != value for key, value in self.startup_network.items())
        body["listen_port_actual"] = self.port
        body["upnp"] = self.upnp_status
        body["natpmp"] = self.natpmp_status
        body["lsd"] = self.lsd_status
        body["dht_nodes"] = len(self.dht.nodes) if self.dht is not None else 0
        return body

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            return self._update_settings(patch)

    def _update_settings(self, patch: dict) -> dict:
        updated = dict(self.config)
        for key, value in patch.items():
            if key not in DEFAULT_SETTINGS or key == "listen_port":
                if key == "listen_port":
                    updated["listen_port"] = _port(value)
                elif key in DEFAULT_SETTINGS:
                    pass
                else:
                    continue
            elif key == "encryption":
                if value not in {"disabled", "prefer", "required"}:
                    raise EngineError("Encryption must be disabled, prefer, or required")
                updated[key] = value
            elif key == "theme":
                if value not in {"light", "dark"}:
                    raise EngineError("Unknown theme")
                updated[key] = value
            elif key == "share_ratio_limit":
                updated[key] = _ratio(value)
            elif key == "max_active_downloads":
                number = _nonnegative(value)
                if number > 999:
                    raise EngineError("Use 999 or fewer torrents at once. Trackers are not limited by this.")
                updated[key] = number
            elif key == "max_active_seeds":
                updated[key] = _nonnegative(value)
            elif key in {"download_limit_bps", "upload_limit_bps"}:
                updated[key] = _nonnegative(value)
            elif key in DEFAULT_SETTINGS:
                updated[key] = bool(value)
        if 'share_metadata' in patch:
            if type(patch['share_metadata']) is not bool:
                raise EngineError('Choose on or off.')
            updated['metadata_generation'] = secrets.token_hex(16)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.data_dir, prefix='.settings-', delete=False) as stream:
                temporary = stream.name
                json.dump(updated, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.data_dir / 'settings.json')
        except OSError as exc:
            raise EngineError('Torrent settings could not be saved.') from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        self.config = updated
        if 'share_metadata' in patch:
            self._metadata_caught = False
        self.global_down.set_rate(int(updated["download_limit_bps"]))
        self.global_up.set_rate(int(updated["upload_limit_bps"]))
        if updated["enable_dht"] and self.dht is None and not self.stop.is_set():
            self.dht = DHTNode()
            self._spawn(self._dht_loop, "reeldock-dht-query")
        return self.settings()

    def inject_peer(self, target: str, host: str, port: int) -> None:
        preview = self._previews.get(target)
        if preview is not None:
            preview.peers.append((host, port))
            return
        found = self._jobs.get(target)
        if found is None or found.removed:
            raise EngineError("Unknown torrent", 404)
        found.add_peers([(host, port)])

    def wait_preview(self, preview_id: str, timeout: float = 5) -> dict:
        preview = self._previews[preview_id]
        preview.done.wait(timeout)
        return self._preview_dict(preview)

    def wait_job(self, job_id: str, timeout: float = 8) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self._jobs[job_id]
            if found.error:
                raise EngineError(found.error)
            if found.selected_complete() and found.phase in {"seed", "stopped", "choose"}:
                return self._job_dict(found)
            time.sleep(0.05)
        detail = found.web_error or found.tracker_error or found.error or "timed out waiting for torrent"
        raise EngineError(detail)

    def flag(self, job: Job, name: str) -> bool:
        if job.meta is not None and job.meta.private and name in {"enable_dht", "enable_pex", "enable_lsd"}:
            return False
        override = getattr(job, name)
        if override is not None:
            return bool(override)
        return bool(self.config[name])

    def finish_metadata(self, job: Job, info_bytes: bytes) -> None:
        preview = next((item for item in self._previews.values() if item.magnet and item.magnet.info_hash == job.hash_bytes and not item.done.is_set()), None)
        job.removed = True
        if preview is None:
            return
        try:
            wrapped = _wrap_info(info_bytes, job.trackers)
            meta = parse_torrent(wrapped)
        except (MetainfoError, Exception) as exc:
            preview.status = "error"
            preview.reason = f"Could not read torrent metadata ({exc}). Nothing was downloaded."
            preview.done.set()
            return
        if meta.info_hash != job.hash_bytes:
            preview.status = "error"
            preview.reason = "Metadata did not match the magnet. Nothing was downloaded."
            preview.done.set()
            return
        self._apply_gate(preview, meta)

    def _ensure_running(self) -> None:
        if self.stop.is_set() or (self.operations is not None and self.operations.halted.is_set()):
            raise Halted("downloads are stopped")

    @torrent
    def _bind(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        requested = int(self.config["listen_port"])
        attempts = [requested] if requested else [0]
        if requested:
            attempts += [requested + offset for offset in range(1, 21) if requested + offset <= 65535]
            attempts.append(0)
        error: Exception | None = None
        for candidate in attempts:
            try:
                sock.bind(("0.0.0.0", candidate))
                error = None
                break
            except OSError as exc:
                error = exc
        if error is not None and sock.getsockname()[1] == 0 and requested:
            raise error
        sock.listen(40)
        sock.settimeout(0.5)
        self._tcp = sock
        self.port = sock.getsockname()[1]

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=torrent(target), name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _accept_loop(self) -> None:
        assert self._tcp is not None
        while not self.stop.is_set():
            try:
                conn, addr = self._tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._spawn(lambda conn=conn, addr=addr: self._inbound(conn, addr), "reeldock-peer")

    def _inbound(self, conn: socket.socket, addr) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            stream, info_hash, remote = open_inbound(
                conn, self.stop, self.peer_id, str(self.config["encryption"]), self._lookup
            )
            job = self._find_job(info_hash)
            if job is None or job.removed:
                conn.close()
                return
            job.active_peers += 1
            job.links.append(stream)
            try:
                run_peer(stream, job, f"{addr[0]}:{addr[1]}", self.stop, remote)
            finally:
                if stream in job.links:
                    job.links.remove(stream)
                job.active_peers = max(0, job.active_peers - 1)
        except (OSError, WireError, Exception):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _lookup(self, *args):
        if len(args) == 1:
            return self._find_job(args[0])
        _ignored, obfuscated, secret = args
        for info_hash in self._known_hashes():
            if skey_matches(obfuscated, secret, info_hash):
                return info_hash
        return None

    def _known_hashes(self) -> list[bytes]:
        hashes = [job.hash_bytes for job in self._jobs.values() if not job.removed]
        hashes.extend(self._meta_jobs.keys())
        return hashes

    def _find_job(self, info_hash: bytes) -> Job | None:
        found = self._jobs.get(info_hash.hex())
        if found is not None and not found.removed:
            return found
        return self._meta_jobs.get(info_hash)

    def _schedule_loop(self) -> None:
        while not self.stop.is_set():
            self._schedule_once()
            if self.stop.wait(0.2):
                return

    def _schedule_once(self) -> None:
        resume = getattr(self, "metadata_resume", None)
        if resume is not None:
            resume()
        jobs = [job for job in list(self._order) if not job.removed and not job.metadata_only]
        slots: list[SlotJob] = []
        for job in jobs:
            previous = job.phase
            if job.selected_complete() and job.phase in {"download", "seed", "choose"}:
                job.publish()
                waiting = bool(job.storage and job.storage.questions(job.categories))
                job.phase = "choose" if waiting else "stopped"
                job.stop_reason = "" if waiting else "Download finished"
                job.io = False
            if job.phase != previous:
                self._save_job(job)
            phase = job.phase if job.phase in {"download", "seed", "stopped", "choose"} else "download"
            slots.append(SlotJob(job.id, "stopped" if phase == "choose" else phase, job.user_paused or phase in {"stopped", "choose"}))
        active = choose_active(
            slots,
            int(self.config["max_active_downloads"]),
            int(self.config["max_active_seeds"]),
        )
        for job in jobs:
            job.io = job.id in active

    def _preview_bytes(self, data: bytes, source: str) -> dict:
        try:
            meta = parse_torrent(data)
        except MetainfoError as exc:
            raise EngineError(str(exc)) from exc
        preview = Preview(id="pv_" + secrets.token_urlsafe(12), source=source, meta=meta)
        self._apply_gate(preview, meta)
        self._previews[preview.id] = preview
        return self._preview_dict(preview)

    def _preview_magnet(self, source: str) -> dict:
        try:
            magnet = parse_magnet(source)
        except MetainfoError as exc:
            raise EngineError(str(exc)) from exc
        preview = Preview(id="pv_" + secrets.token_urlsafe(12), source=source, magnet=magnet)
        self._previews[preview.id] = preview
        self._spawn(lambda: self._magnet_worker(preview), "reeldock-metadata")
        return self._preview_dict(preview)

    def _apply_gate(self, preview: Preview, meta: Metainfo) -> None:
        result = gate([(item.index, item.path, item.length) for item in meta.files])
        preview.meta = meta
        preview.result_files = result.files
        preview.accepted = result.accepted
        preview.reason = result.reason
        preview.status = "ready" if result.accepted else "rejected"
        preview.done.set()

    def _magnet_worker(self, preview: Preview) -> None:
        assert preview.magnet is not None
        job = Job(self, preview.magnet.info_hash)
        job.metadata_only = True
        job.io = True
        job.trackers = list(preview.magnet.trackers)
        job.listen_port = self.port
        self._meta_jobs[job.hash_bytes] = job
        self._spawn(lambda: self._tracker_loop(job), "reeldock-meta-tracker")
        deadline = time.monotonic() + 90
        try:
            while not self.stop.is_set() and not preview.done.is_set() and time.monotonic() < deadline:
                while preview.peers:
                    job.add_peers([preview.peers.popleft()])
                if job.peer_queue and job.active_peers < 8:
                    addr = job.peer_queue.popleft()
                    self._connect(job, addr)
                if self.stop.wait(0.05):
                    return
            if not preview.done.is_set():
                preview.status = "error"
                preview.reason = "Timed out reading torrent metadata. No files were downloaded."
                preview.done.set()
        finally:
            job.removed = True
            self._meta_jobs.pop(job.hash_bytes, None)

    @torrent
    def _fetch_torrent(self, url: str) -> bytes:
        class Guard(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if urllib.parse.urlparse(newurl).scheme not in {"http", "https"}:
                    raise EngineError("Refusing a non-HTTP redirect")
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        opener = urllib.request.build_opener(Guard)
        request = urllib.request.Request(url, headers={"User-Agent": "ReelDock/0.1"})
        try:
            with opener.open(request, timeout=20) as response:
                chunks = []
                total = 0
                while total <= 2_000_000:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    chunks.append(chunk)
        except EngineError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise EngineError(f"Could not fetch torrent file ({exc})") from exc
        if total > 2_000_000:
            raise EngineError("Torrent file is too large")
        return b"".join(chunks)

    def _selection(self, preview: Preview, file_indexes: list[int] | None) -> set[int]:
        allowed = {item.index: item for item in preview.result_files}
        if file_indexes is None:
            return {index for index, item in allowed.items() if item.selected}
        chosen = set()
        for index in file_indexes:
            item = allowed.get(int(index))
            if item is None or not item.selectable:
                raise EngineError("Only video, audio, subtitles, artwork, and .nfo files can be downloaded")
            chosen.add(item.index)
        if not chosen:
            raise EngineError("Choose at least one media file")
        return chosen

    def _open_job(self, meta: Metainfo, selected: set[int], save_as: str = "", *, restored: dict | None = None) -> Job:
        from ...media_holds import native_blocked
        if native_blocked(self.data_dir,['native:torrent:'+meta.info_hash.hex(),'native:torrent-job:'+meta.info_hash.hex()],fresh=True):raise EngineError('This torrent was cleared. Retry it explicitly before downloading.',409)
        allowed = gate([(item.index, item.path, item.length) for item in meta.files])
        selected = selected & {item.index for item in allowed.files if item.selectable}
        if not selected:
            raise EngineError("No downloadable media remains after excluding sample files.")
        storage = Storage(self.media_base, meta, selected, self.operations)
        storage.save_as = save_as
        storage.prepare()
        lengths = [storage.piece_size(index) for index in range(meta.piece_count)]
        picker = PiecePicker(lengths, _wanted(meta, selected))
        job = Job(self, meta.info_hash)
        job.meta = meta
        job.info_bytes = meta.info_bytes
        job.storage = storage
        job.picker = picker
        job.selected = set(selected)
        job.save_as = save_as
        job.trackers = list(meta.trackers)
        job.listen_port = self.port
        job.phase = "download"
        if restored is not None:_restore_saved(job,restored)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job)
        self._restore_pieces(job)
        self._schedule_once()
        self._spawn(lambda: self._webseed_loop(job), "reeldock-webseed")
        self._spawn(lambda: self._tracker_loop(job), "reeldock-tracker")
        self._spawn(lambda: self._peer_loop(job), "reeldock-peers")
        return job

    def _restore_pieces(self, job: Job) -> None:
        assert job.storage is not None and job.picker is not None and job.meta is not None
        for index in range(job.meta.piece_count):
            data = job.storage.read_unchecked(index)
            if data is None:
                continue
            if data == b"" or len(data) != job.storage.piece_size(index):
                continue
            import hashlib

            expected = job.meta.pieces[index * 20 : (index + 1) * 20]
            if hashlib.sha1(data).digest() == expected:
                job.storage.verified[index] = True
                job.storage.have[index] = True
                job.picker.mark_verified(index)

    def _reselect(self, job: Job, indexes) -> None:
        if job.meta is None or job.picker is None or job.storage is None:
            raise EngineError("Torrent is not ready")
        result = gate([(item.index, item.path, item.length) for item in job.meta.files])
        allowed = {item.index: item for item in result.files}
        chosen = set()
        for index in indexes:
            item = allowed.get(int(index))
            if item is None or not item.selectable:
                raise EngineError("Only video, audio, subtitles, artwork, and .nfo files can be downloaded")
            chosen.add(item.index)
        if not chosen:
            raise EngineError("Choose at least one media file")
        job.selected = chosen
        job.storage.selected = set(chosen)
        job.picker.wanted = _wanted(job.meta, chosen) - set(job.picker.verified)

    def _connect(self, job: Job, addr: tuple[str, int]) -> None:
        mode = str(self.config["encryption"])
        if mode == "required":
            attempts = ["required"]
        elif mode == "disabled":
            attempts = ["disabled"]
        else:
            # Plaintext first. The encrypted handshake does not yet
            # interoperate with other clients, and a peer that ignores it
            # used to hold the connection forever.
            attempts = ["disabled", "required"]

        def work() -> None:
            try:
                last: Exception | None = None
                for attempt in attempts:
                    sock = None
                    try:
                        sock = socket.create_connection(addr, timeout=8)
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        stream, remote = open_outbound(sock, self.stop, job.hash_bytes, self.peer_id, attempt)
                        with job._lock:
                            job.active_peers += 1
                        try:
                            job.links.append(stream)
                            try:
                                run_peer(stream, job, f"{addr[0]}:{addr[1]}", self.stop, remote)
                            finally:
                                if stream in job.links:
                                    job.links.remove(stream)
                        finally:
                            with job._lock:
                                job.active_peers = max(0, job.active_peers - 1)
                        return
                    except (OSError, WireError, Exception) as exc:
                        last = exc
                        if sock is not None:
                            try:
                                sock.close()
                            except OSError:
                                pass
                if last is not None and not job.metadata_only:
                    detail = str(last)
                    if isinstance(last, TimeoutError) or "timed out" in detail.lower():
                        job.tracker_error = (
                            "A peer did not answer within 8 seconds. "
                            "The download is still running and will try other peers."
                        )
                    else:
                        job.tracker_error = detail[:200]
            finally:
                with job._lock:
                    job.dialing = max(0, job.dialing - 1)

        self._spawn(work, "reeldock-out")

    def _peer_loop(self, job: Job) -> None:
        while not self.stop.is_set() and not job.removed:
            addr = None
            with job._lock:
                if job.io and job.peer_queue and job.active_peers < 30 and job.dialing < 8:
                    addr = job.peer_queue.popleft()
                    job.dialing += 1
            if addr is not None:
                self._connect(job, addr)
            if self.stop.wait(0.1):
                return

    def _webseed_loop(self, job: Job) -> None:
        while not self.stop.is_set() and not job.removed:
            if job.picker is None or job.meta is None or not job.meta.url_list or not job.io or not self.flag(job, "enable_web_seeds"):
                if self.stop.wait(0.1):
                    return
                continue
            span = job.picker.claim_span("web", 4)
            if not span:
                if self.stop.wait(0.1):
                    return
                continue
            try:
                self._fetch_span(job, span)
            except (WebSeedError, OSError, Halted, Exception) as exc:
                job.picker.release_span(span, "web")
                job.web_error = str(exc)[:240]
                if self.stop.wait(0.5):
                    return

    def _fetch_span(self, job: Job, indexes: list[int]) -> None:
        assert job.meta is not None and job.storage is not None and job.picker is not None
        meta = job.meta
        start = indexes[0] * meta.piece_length
        end = indexes[-1] * meta.piece_length + job.storage.piece_size(indexes[-1])
        buf = bytearray(end - start)
        for torrent_file in meta.files:
            overlap_start = max(start, torrent_file.offset)
            overlap_end = min(end, torrent_file.offset + torrent_file.length)
            if overlap_start >= overlap_end:
                continue
            length = overlap_end - overlap_start
            data = None
            error = "no web seed"
            for url in file_urls(meta, torrent_file):
                try:
                    data = fetch_range(
                        url,
                        overlap_start - torrent_file.offset,
                        length,
                        job.take_download,
                        self.stop,
                    )
                    break
                except WebSeedError as exc:
                    error = str(exc)
                    data = None
            if data is None:
                raise WebSeedError(error)
            buf[overlap_start - start : overlap_end - start] = data
        for index in indexes:
            piece_start = index * meta.piece_length
            size = job.storage.piece_size(index)
            piece = bytes(buf[piece_start - start : piece_start - start + size])
            if job.removed:
                raise WebSeedError("Stopped.")
            with job._lock:
                job.writers += 1
            try:
                saved = not job.removed and job.storage.write_piece(index, piece)
            finally:
                with job._lock:
                    job.writers = max(0, job.writers - 1)
            if not saved:
                raise WebSeedError(f"piece {index} failed verification")
            job.picker.mark_verified(index)
            job.note_download(size)
            job.broadcast_have(index)
        job.publish()
        job.web_error = ""

    def _tracker_loop(self, job: Job) -> None:
        event = "started"
        while not self.stop.is_set() and not job.removed:
            if not job.io and not job.metadata_only:
                if self.stop.wait(0.2):
                    return
                continue
            usable, _skipped = job.usable()
            interval = 30
            left = 0
            if job.storage is not None:
                done, total = job.storage.selected_bytes_done()
                left = max(0, total - done)
            if job.phase == "seed" and not job.completed_announced:
                event = "completed"
                job.completed_announced = True
            for url in usable:
                try:
                    peers, interval = announce(
                        url,
                        job.hash_bytes,
                        self.peer_id,
                        self.port,
                        job.uploaded,
                        job.downloaded,
                        left,
                        event,
                    )
                    job.add_peers(peers)
                    job.tracker_error = ""
                except TrackerError as exc:
                    job.tracker_error = _tracker_failure(url, exc)
            event = ""
            if self.stop.wait(min(max(interval, 5), 90)):
                return
        usable, _skipped = job.usable()
        for url in usable:
            try:
                announce(url, job.hash_bytes, self.peer_id, self.port, job.uploaded, job.downloaded, 0, "stopped", 1)
            except TrackerError:
                pass

    def _dht_loop(self) -> None:
        while not self.stop.is_set():
            if self.dht is not None and self.config["enable_dht"]:
                for job in list(self._order):
                    if job.removed or job.meta is None or not job.io or not self.flag(job, "enable_dht"):
                        continue
                    seeds = list(BOOTSTRAP) + list(job.meta.nodes)
                    try:
                        peers = self.dht.get_peers(job.hash_bytes, seeds, timeout=1.0)
                        job.add_peers(peers)
                    except OSError:
                        pass
            if self.stop.wait(15):
                return

    def _lsd_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", 6771))
            group = socket.inet_aton("239.192.152.143")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group + socket.inet_aton("0.0.0.0"))
            sock.settimeout(0.5)
        except OSError as exc:
            self.lsd_status = {"ok": False, "detail": str(exc)}
            sock.close()
            return
        self.lsd_status = {"ok": True, "detail": "Listening for local peers"}
        cookie = secrets.token_hex(4)
        next_send = 0.0
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if now >= next_send:
                    for job in list(self._order):
                        if job.removed or not job.io or not self.flag(job, "enable_lsd"):
                            continue
                        try:
                            send_packet(build_packet(job.hash_bytes, self.port, cookie))
                        except OSError as exc:
                            self.lsd_status = {"ok": False, "detail": str(exc)}
                    next_send = now + 45
                try:
                    packet, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                parsed = parse_packet(packet)
                if parsed is None:
                    continue
                info_hash, port = parsed
                job = self._find_job(info_hash)
                if job is not None and self.flag(job, "enable_lsd"):
                    job.add_peers([(addr[0], port)])
        finally:
            sock.close()

    def _map_ports(self) -> None:
        if self.config["enable_upnp"]:
            self.upnp_status = try_upnp(self.port)
        if self.config["enable_natpmp"]:
            self.natpmp_status = try_natpmp(self.port)

    def _read_settings(self) -> dict:
        path = self.data_dir / "settings.json"
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {key: loaded[key] for key in DEFAULT_SETTINGS if key in loaded}

    def _save_job(self, job: Job) -> None:
        if job.meta is None:
            return
        from ...activity_clear import native_generation
        activity_generation = native_generation(job, job.phase in {'stopped', 'error', 'failed'})
        folder = self.data_dir / "torrents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{job.id}.torrent").write_bytes(job.meta.raw)
        payload = {
            "activity_generation": activity_generation,
            "selected": sorted(job.selected),
            "trackers": job.trackers,
            "downloaded": job.downloaded,
            "uploaded": job.uploaded,
            "phase": job.phase,
            "user_paused": job.user_paused,
            "download_limit_bps": job.download_limit_bps,
            "upload_limit_bps": job.upload_limit_bps,
            "ratio_limit": job.ratio_limit,
            "enable_dht": job.enable_dht,
            "enable_pex": job.enable_pex,
            "enable_lsd": job.enable_lsd,
            "enable_web_seeds": job.enable_web_seeds,
            "added": int(job.added),
            "categories": {str(index): value for index, value in job.categories.items()},
            "save_as": job.save_as,
            "metadata_note": job.metadata_note,
            "metadata_done": sorted(job.metadata_done),
            "metadata_choice": {str(index): value for index, value in job.metadata_choice.items()},
        }
        from ..job_state import save
        save(folder / f"{job.id}.json",payload)

    def _resume(self) -> None:
        folder = self.data_dir / "torrents"
        if not folder.exists():
            return
        for torrent_path in folder.glob("*.torrent"):
            state_path = torrent_path.with_suffix(".json")
            if not state_path.exists():
                continue
            try:
                meta = parse_torrent(torrent_path.read_bytes())
                state = json.loads(state_path.read_text(encoding="utf-8"))
                from ...media_holds import native_blocked
                if native_blocked(self.data_dir,['native:torrent:'+meta.info_hash.hex(),'native:torrent-job:'+meta.info_hash.hex()],fresh=True):
                    job=Job(self,meta.info_hash);job.meta=meta;job.info_bytes=meta.info_bytes
                    job.selected={int(index) for index in state.get('selected') or []};job.save_as=_stored_title(state)
                    job.phase=state.get('phase') or 'download';job.user_paused=True;job.trackers=list(state.get('trackers') or meta.trackers)
                    job.downloaded=int(state.get('downloaded') or 0);job.uploaded=int(state.get('uploaded') or 0)
                    job._held_restore=True;job._held_state=dict(state)
                    _restore_saved(job,state,state_path)
                    job.user_paused=True
                    with self._lock:self._jobs[job.id]=job;self._order.append(job)
                    continue
                if state.get("phase") == "stopped":
                    self._attach_finished(meta, state)
                    continue
                # _open_job schedules and starts transfer loops. Restore pause,
                # limits and metadata before that first opportunity to do I/O.
                job = self._open_job(meta, set(state.get("selected") or []), _stored_title(state), restored=state)
            except (OSError, MetainfoError, EngineError, json.JSONDecodeError):
                continue
            _restore_saved(job,state,state_path)
            job.publish()

    def _attach_finished(self, meta: Metainfo, state: dict) -> None:
        """Keep a finished torrent on the activity list without starting it again."""
        selected = {int(index) for index in state.get("selected") or []}
        storage = Storage(self.media_base, meta, selected, self.operations)
        storage.save_as = _stored_title(state)
        storage.prepare()
        job = Job(self, meta.info_hash)
        job.save_as = storage.save_as
        job.meta = meta
        job.storage = storage
        job.selected = selected
        job.phase = "stopped"
        job.stop_reason = "Download finished"
        job.trackers = list(state.get("trackers") or meta.trackers)
        added = state.get("added")
        if isinstance(added, (int, float)) and added > 0:
            job.added = float(added)
        saved_categories = state.get("categories") or {}
        if isinstance(saved_categories, dict):
            for index, value in saved_categories.items():
                if value in {"movie", "series"}:
                    try:
                        job.categories[int(index)] = value
                    except (TypeError, ValueError):
                        continue
        _restore_metadata(job, state)
        storage.verified = [True] * meta.piece_count
        storage.have = [True] * meta.piece_count
        storage.note_existing(self.library_layout(), job.categories)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job)

    def _preview_dict(self, preview: Preview) -> dict:
        meta = preview.meta
        trackers = meta.trackers if meta is not None else (preview.magnet.trackers if preview.magnet else [])
        usable, skipped = usable_trackers(trackers)
        suggestion = suggest_download(preview.result_files, self.media_base, self.library_layout())
        suggested_name = preview.found_name if preview.found_name and not suggestion["folder"] else suggestion["name"]
        return {
            "id": preview.id,
            "status": preview.status,
            "accepted": preview.accepted,
            "name": meta.name if meta is not None else (preview.magnet.name if preview.magnet else ""),
            "reason": preview.reason,
            "info_hash": meta.info_hash.hex() if meta is not None else (preview.magnet.info_hash.hex() if preview.magnet else ""),
            "files": [_file_dict(item) for item in preview.result_files],
            "trackers": usable,
            "skipped_trackers": skipped,
            "web_seeds": meta.url_list if meta is not None else [],
            "source": preview.source,
            "suggested_name": suggested_name,
            "suggested_folder": suggestion["folder"],
        }

    def _job_dict(self, job: Job) -> dict:
        done, total = job.storage.selected_bytes_done() if job.storage is not None else (0, 0)
        if job.user_paused or (self.global_paused() and job.phase not in {"stopped", "choose"}):
            state = "paused"
        elif job.phase == "choose":
            state = "choose"
        elif job.phase == "stopped":
            state = "stopped"
        elif not job.io:
            state = "queued"
        elif job.phase == "seed":
            state = "seeding"
        else:
            state = "downloading"
        files = []
        if job.meta is not None:
            result = gate([(item.index, item.path, item.length) for item in job.meta.files])
            for item in result.files:
                files.append({**_file_dict(item), "selected": item.index in job.selected})
        usable, skipped = job.usable()
        result = {
            "id": job.id,
            "activity_generation": getattr(job, 'activity_generation', 0),
            "name": job.save_as or (job.meta.name if job.meta is not None else job.id),
            "state": state,
            "progress": (done / total) if total else 0,
            "downloaded": job.downloaded,
            "uploaded": job.uploaded,
            "ratio": (job.uploaded / job.downloaded) if job.downloaded else 0,
            "down_rate": job.down_rate.bps(),
            "up_rate": job.up_rate.bps(),
            "peers": job.active_peers,
            "trackers": job.trackers,
            "usable_trackers": usable,
            "skipped_trackers": skipped,
            "web_seeds": job.meta.url_list if job.meta is not None else [],
            "files": files,
            "error": job.error or ("" if job.selected_complete() else job.web_error or job.tracker_error),
            "stop_reason": job.stop_reason,
            "added": int(job.added),
            "published": list(job.storage.published.values()) if job.storage is not None else [],
            "placements": job.storage.questions(job.categories) if job.storage is not None else [],
            "metadata_note": job.metadata_note,
            "limits": {
                "download_limit_bps": job.download_limit_bps,
                "upload_limit_bps": job.upload_limit_bps,
                "share_ratio_limit": job.ratio_limit,
                "enable_dht": job.enable_dht,
                "enable_pex": job.enable_pex,
                "enable_lsd": job.enable_lsd,
                "enable_web_seeds": job.enable_web_seeds,
            },
        }
        from ...activity_observation import capture
        result['activity_observation']=capture('torrent',result)
        return result


def _restore_saved(job: Job, state: dict, state_path: Path | None = None) -> None:
    job.downloaded = int(state.get("downloaded") or 0)
    job.uploaded = int(state.get("uploaded") or 0)
    job.user_paused = bool(state.get("user_paused"))
    job.trackers = list(state.get("trackers") or job.trackers)
    job.download_limit_bps = int(state.get("download_limit_bps") or 0)
    job.upload_limit_bps = int(state.get("upload_limit_bps") or 0)
    job.ratio_limit = state.get("ratio_limit")
    added = state.get("added")
    if isinstance(added, (int, float)) and added > 0:
        job.added = float(added)
    else:
        if state_path is not None:
            try:job.added = state_path.stat().st_mtime
            except OSError:pass
    for name in ("enable_dht", "enable_pex", "enable_lsd", "enable_web_seeds"):
        if name in state:
            setattr(job, name, state[name])
    saved_categories = state.get("categories") or {}
    if isinstance(saved_categories, dict):
        for index, value in saved_categories.items():
            if value in {"movie", "series"}:
                try:job.categories[int(index)] = value
                except (TypeError, ValueError):continue
    if isinstance(state.get("metadata_note"), str):
        job.metadata_note = state["metadata_note"][:200]
    _restore_metadata(job, state)
    job.down_bucket.set_rate(job.download_limit_bps)
    job.up_bucket.set_rate(job.upload_limit_bps)


def _restore_metadata(job: Job, state: dict) -> None:
    from ...activity_clear import restore_native
    restore_native(job, state, state.get('phase') in {'stopped', 'error', 'failed'})
    if isinstance(state.get("metadata_note"), str):
        job.metadata_note = state["metadata_note"][:200]
    for index in state.get("metadata_done") or []:
        try:
            job.metadata_done.add(int(index))
        except (TypeError, ValueError):
            continue
    saved = state.get("metadata_choice") or {}
    if not isinstance(saved, dict):
        return
    for index, value in saved.items():
        if not isinstance(value, dict) or value.get("mode") not in {"tmdb", "manual"}:
            continue
        if value.get("media_type") not in {"movie", "tv"}:
            continue
        try:
            job.metadata_choice[int(index)] = value
        except (TypeError, ValueError):
            continue


def _wanted(meta: Metainfo, selected: set[int]) -> set[int]:
    wanted: set[int] = set()
    for torrent_file in meta.files:
        if torrent_file.index not in selected or torrent_file.length <= 0:
            continue
        first = torrent_file.offset // meta.piece_length
        last = (torrent_file.offset + torrent_file.length - 1) // meta.piece_length
        wanted.update(range(first, last + 1))
    return wanted


def _stored_title(state: dict) -> str:
    value = state.get("save_as")
    return value[:180] if isinstance(value, str) else ""


def _chosen_title(preview: Preview, value: str | None, selected: set[int], base=None, layout=None) -> str:
    """Use the scan box, then a found show title, then the filename suggestion."""
    if isinstance(value, str) and value.strip():
        return sanitize(value)
    suggestion = suggest_download(
        [item for item in preview.result_files if item.index in selected],
        base,
        layout or {},
    )
    if suggestion["folder"] or not preview.found_name:
        return suggestion["name"]
    return preview.found_name


def _save_title(value: str | None, files, selected: set[int], base=None, layout=None) -> str:
    """Use the name from the scan dialog, or the recommendation when it is left blank."""
    if isinstance(value, str) and value.strip():
        return sanitize(value)
    videos = [item for item in files if item.index in selected and item.kind == "video" and item.selectable]
    if not videos:
        return ""
    if base is not None:
        return suggest_download(videos, base, layout or {})["name"]
    return suggested_title(max(videos, key=lambda item: item.size).path)


def _file_dict(item: ClassifiedFile) -> dict:
    return {
        "index": item.index,
        "path": item.path,
        "size": item.size,
        "kind": item.kind,
        "selected": item.selected,
        "selectable": item.selectable,
        "reason": item.reason,
    }


def _wrap_info(info_bytes: bytes, trackers: list[str]) -> bytes:
    body = b"4:info" + info_bytes
    if trackers:
        body = bencode(b"announce") + bencode(trackers[0]) + body
    return b"d" + body + b"e"


def _nonnegative(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise EngineError("Expected a number") from exc
    if number < 0:
        raise EngineError("Expected a positive number")
    return number


def _ratio(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EngineError("Expected a ratio") from exc
    if number < 0:
        raise EngineError("Ratio cannot be negative")
    return number


def _port(value) -> int:
    number = _nonnegative(value)
    if number > 65535:
        raise EngineError("Port is out of range")
    return number


def _tracker_failure(url: str, exc: BaseException) -> str:
    host = urllib.parse.urlparse(url)
    where = host.netloc or url
    text = str(exc)
    if "Connection refused" in text or "Errno 111" in text:
        return (
            f"The tracker at {where} refused the connection. Nothing is accepting announce requests there, "
            "so it cannot send a peer list. Peers already found can still send the file."
        )
    if "timed out" in text.lower():
        return f"The tracker at {where} did not answer in time. ReelDock will try again. Peers already found can still send the file."
    return f"The tracker at {where} could not be reached. {text}"[:240]


def bdecode_info_ok(info_bytes: bytes) -> bool:
    try:
        value = bdecode_all(info_bytes)
    except Exception:
        return False
    return isinstance(value, dict)
