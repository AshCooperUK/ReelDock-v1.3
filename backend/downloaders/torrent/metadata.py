"""Fetch TMDB details after a torrent video is renamed, and save Emby sidecars."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import stat
import threading
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from ...metadata_control import Cancelled, checkpoint, work
from ...vpn.transport import metadata
from ...m3u_metadata import TMDB
from .rename import _describe, category_for, library_path, named_info

BASIC_NOTE = 'Basic local metadata saved. TMDB is optional for posters, plot and cast.'


def _local_bytes(base, path, limit=12*1024*1024):
    from ...m3u import contained
    path=contained(base,path.relative_to(base))
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,'rb') as stream:
        state=os.fstat(stream.fileno())
        if not stat.S_ISREG(state.st_mode) or state.st_size>limit:raise ValueError('Metadata sidecar is not a bounded regular file.')
        raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('Metadata sidecar is too large.')
    return raw


def write_basic(store, base: Path, relative: str, category: str, info: dict) -> bool:
    """Serialise local generic sidecars shared by native and VOD writers."""
    from ...m3u import SIDECAR_LOCK
    with SIDECAR_LOCK:
        return _write_basic(store,base,relative,category,info)


def _write_basic(store, base: Path, relative: str, category: str, info: dict) -> bool:
    """Write only known local facts; never replace valid custom or rich XML."""
    from ... import m3u
    from ...m3u_sidecars import initialise, xml
    if category not in {'movie','series'}:return False
    base=Path(base).absolute();m3u.contained(base,'');target=m3u.contained(base,relative)
    if not target.is_file():return False
    initialise(store)
    title=str(info.get('name') or info.get('title') or target.stem).strip()
    year=info.get('year');fields={'title':title}
    if re.fullmatch(r'(?:18|19|20)\d{2}',str(year or '')):fields['year']=str(year)
    runtime=info.get('runtime')
    if type(runtime) in (int,float) and math.isfinite(runtime) and runtime>0:fields['runtime']=runtime
    changed=False
    def write(path,tag,data,**extra):
        nonlocal changed
        checkpoint()
        path=m3u.contained(base,path.relative_to(base))
        if path.is_file():
            try:
                ET.fromstring(_local_bytes(base,path))
                return  # Existing valid metadata belongs to its current author.
            except ET.ParseError:pass
        changed=_write_nfo(store,base,path,xml(tag,data,**extra)) or changed
    if category=='movie':write(target.with_suffix('.nfo'),'movie',fields)
    else:
        folder=target.parent.parent if target.parent.name.lower().startswith('season') else target.parent
        write(folder/'tvshow.nfo','tvshow',fields)
        season=info.get('season');episode=info.get('episode')
        season=season if type(season) is int and 0<=season<=9999 else None
        episode=episode if type(episode) is int and 0<=episode<=9999 else None
        episode_fields={key:value for key,value in fields.items() if key!='title'}
        episode_fields['title']=str(info.get('episode_title') or '').strip() or ('Episode '+str(episode) if episode is not None else 'Episode')
        write(target.with_suffix('.nfo'),'episodedetails',episode_fields,season=season,episode=episode,show=title)
    # Existing local frames are useful generic art, never catalogue artwork.
    from ...m3u_sidecars import owned_write
    import io
    from PIL import Image
    folder=target.parent.parent if category=='series' and target.parent.name.lower().startswith('season') else target.parent
    for candidate in (target.with_name(target.stem+'-thumb.jpg'),target.with_suffix('.jpg'),target.with_name(target.stem+'-thumb.png'),target.with_suffix('.png')):
        try:
            raw=_local_bytes(base,candidate)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ('JPEG','PNG') or image.width*image.height>24000000:continue
                image.load();image.thumbnail((1920,1920));output=io.BytesIO();image.convert('RGB').save(output,format='JPEG',quality=90);raw=output.getvalue()
                if len(raw)>12*1024*1024:continue
            for destination in (folder/'poster.jpg',target.with_name(target.stem+'-thumb.jpg')):
                checkpoint();destination=m3u.contained(base,destination.relative_to(base))
                if not destination.exists():changed=owned_write(store,base,destination,raw) or changed
            break
        except (OSError,ValueError,Image.DecompressionBombError):continue
    return changed


def _key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().replace("'", "").replace("’", "")
    return "".join(character for character in text if character.isalnum())


def _identified(rows: list, name: str, year: int | None) -> dict | None:
    """Use the catalogue identity. A single hit is that film even if the release title differs."""
    usable = [item for item in rows if isinstance(item, dict) and isinstance(item.get("id"), int) and item["id"] > 0]
    if len(usable) == 1:
        return usable[0]
    return choose(usable, name, year)


def choose(results: list, name: str, year: int | None = None) -> dict | None:
    """Return the one result whose title matches, using the year when it is known."""
    wanted = _key(name)
    exact = [item for item in results if isinstance(item, dict) and _key(item.get("title") or item.get("name") or "") == wanted]
    if year:
        dated = [item for item in exact if str(item.get("release_date") or item.get("first_air_date") or "").startswith(str(year))]
        exact = dated or []
    if len(exact) == 1 and isinstance(exact[0].get("id"), int) and exact[0]["id"] > 0:
        return exact[0]
    return None


@metadata
def fetch(store, base: Path, relative: str, category: str, info: dict, client=None) -> bool:
    """Write the NFO and artwork for one renamed movie or episode. Return whether anything changed."""
    from ... import m3u
    from ...m3u_sidecars import artwork, xml

    if category not in {"movie", "series"}:
        return False
    target = m3u.contained(base, relative)
    if not target.is_file():
        return False
    client = client or TMDB(store)
    kind = "movie" if category == "movie" else "tv"
    year = info.get("year") if category == "movie" or info.get("use_year") else None
    if info.get("provider") == "tmdb" and str(info.get("identity") or "").isdigit():
        found = {"id": int(info["identity"])}
    elif info.get("provider") == "imdb" and info.get("identity"):
        identity = str(info["identity"])
        if identity.isdigit():
            identity = "tt" + identity.zfill(7)
        found_id = client.get("find/" + identity, external_source="imdb_id")
        rows = found_id.get("movie_results" if kind == "movie" else "tv_results", [])
        found = _identified(rows, info.get("name") or "", year)
    elif info.get("provider") == "tvdb" and str(info.get("identity") or "").isdigit():
        found_id = client.get("find/" + str(info["identity"]), external_source="tvdb_id")
        rows = found_id.get("tv_results" if kind == "tv" else "movie_results", [])
        found = _identified(rows, info.get("name") or "", year)
    else:
        found = choose(client.get("search/" + kind, query=info.get("name") or "").get("results", []), info.get("name") or "", year)
    if not found:
        raise ValueError("No unique metadata match for this title.")
    entity = client.get(f"{kind}/{found['id']}", append_to_response="credits")
    if not entity:
        raise ValueError("Metadata details were not available.")
    changed = False

    def write(path: Path, tag: str, data: dict, **extra) -> None:
        nonlocal changed
        changed = _write_nfo(store, base, path, xml(tag, data, **extra)) or changed

    def art(path: Path, remote) -> None:
        nonlocal changed
        try:
            changed = artwork(store, base, path, remote) or changed
        except (OSError, ValueError):
            return

    if kind == "movie":
        write(target.with_suffix(".nfo"), "movie", entity)
        art(target.with_name(target.stem + "-poster.jpg"), entity.get("poster_path"))
        art(target.with_name("poster.jpg"), entity.get("poster_path"))
        art(target.with_name(target.stem + "-fanart.jpg"), entity.get("backdrop_path"))
        art(target.with_name("fanart.jpg"), entity.get("backdrop_path"))
        return changed
    folder = target.parent.parent if target.parent.name.lower().startswith("season") else target.parent
    write(folder / "tvshow.nfo", "tvshow", entity)
    art(folder / "poster.jpg", entity.get("poster_path"))
    art(folder / "fanart.jpg", entity.get("backdrop_path"))
    season, episode = info.get("season"), info.get("episode")
    if season is not None and episode is not None:
        data = client.get(f"tv/{found['id']}/season/{int(season)}")
        matched = next((item for item in data.get("episodes", []) if item.get("episode_number") == int(episode)), None)
        if matched:
            write(target.with_suffix(".nfo"), "episodedetails", matched, season=season, episode=episode, show=entity.get("name", ""))
            art(target.with_name(target.stem + "-thumb.jpg"), matched.get("still_path"))
    return changed


def _write_nfo(store, base: Path, path: Path, raw: bytes) -> bool:
    """Coordinate rich and generic local writes without holding network work."""
    from ...m3u import SIDECAR_LOCK
    with SIDECAR_LOCK:
        try:return _write_nfo_locked(store,base,path,raw)
        except FileExistsError:
            # Another process can publish a valid sidecar before our link.
            # Keep that author's metadata and continue with this episode.
            try:ET.fromstring(_local_bytes(base,path))
            except (OSError,ValueError,ET.ParseError):raise
            return False


def _write_nfo_locked(store, base: Path, path: Path, raw: bytes) -> bool:
    """Save Emby metadata. A release-text .nfo copied from the torrent can be replaced."""
    from ...m3u_sidecars import owned_write

    checkpoint()
    from ...m3u import contained
    path=contained(base,path.relative_to(base))
    if path.is_file():
        previous = _local_bytes(base,path)
        try:ET.fromstring(previous);emby=True
        except ET.ParseError:emby=False
        if not emby:
            backup=path.with_name(path.stem+'.release-note-'+hashlib.sha256(previous).hexdigest()[:12]+'.txt')
            owned_write(store,base,backup,previous)
            if not backup.is_file() or _local_bytes(base,backup)!=previous:
                raise ValueError('The original release note could not be preserved.')
            checkpoint()
            path.unlink()
    return owned_write(store, base, path, raw)


def schedule(store, base: Path, job, created) -> None:
    """Look up each renamed video without blocking the download."""
    if job.meta is None:
        return
    if not getattr(job.engine, 'config', {}).get('share_metadata', True):
        return
    pending = []
    with job._lock:
        for index, relative, kind in created:
            if kind != "video" or index in job.metadata_started or index in job.metadata_done:
                continue
            source = next((item.path for item in job.meta.files if item.index == index), "")
            info = named_info(source, getattr(job, "save_as", ""))
            provider = str(getattr(job, "lookup_provider", "") or "")
            identity = str(getattr(job, "lookup_identity", "") or "")
            if provider in {"imdb", "tvdb", "tmdb"} and identity:
                info = dict(info, provider=provider, identity=identity)
            category = job.categories.get(index) or _category(info, source)
            if category not in {"movie", "series"}:
                continue
            job.metadata_started.add(index)
            pending.append((index, relative, category, info))
    for index, relative, category, info in pending:
        threading.Thread(
            target=_save,
            args=(store, Path(base), job, index, relative, category, info),
            daemon=True,
            name="reeldock-metadata",
        ).start()


def catch_up(store, base: Path, engine) -> None:
    """Fetch metadata for videos already renamed, including finished downloads."""
    ready=bool(store and store.get('m3u_tmdb_token'))
    previous=getattr(engine,'_metadata_ready_snapshot',None)
    engine._metadata_ready_snapshot=ready
    if ready and previous is False:engine._metadata_caught=False
    if not getattr(engine, "config", {}).get("share_metadata", True) or getattr(engine, "_metadata_caught", False):
        return
    engine._metadata_caught = True
    for job in list(getattr(engine, "_jobs", {}).values()):
        if job.removed or job.storage is None:
            continue
        rows = [(index, relative, "video") for index, relative in job.storage.published.items() if _kind(job, index) == "video"]
        schedule(store, base, job, rows)
    folder = engine.data_dir / "torrents"
    if not folder.exists():
        return
    active = {job.id for job in getattr(engine, "_jobs", {}).values()}
    for torrent_path in folder.glob("*.torrent"):
        state_path = torrent_path.with_suffix(".json")
        if not state_path.exists():
            continue
        try:
            from .metainfo import parse_torrent

            meta = parse_torrent(torrent_path.read_bytes())
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if meta.info_hash.hex() in active or state.get("phase") != "stopped":
            continue
        selected = {int(index) for index in state.get("selected") or []}
        categories = {}
        for index, value in (state.get("categories") or {}).items():
            if value in {"movie", "series"}:
                try:
                    categories[int(index)] = value
                except (TypeError, ValueError):
                    continue
        job = engine._jobs.get(meta.info_hash.hex())
        if job is None:
            from .engine import Job

            job = Job(engine, meta.info_hash)
            job.meta = meta
            job.selected = selected
            job.categories = categories
            job.phase = "stopped"
            saved_title = state.get("save_as")
            job.save_as = saved_title[:180] if isinstance(saved_title, str) else ""
            for index in state.get("metadata_done") or []:
                try:
                    job.metadata_done.add(int(index))
                except (TypeError, ValueError):
                    continue
        rows = _saved_rows(engine, meta, selected, categories, getattr(job, "save_as", ""))
        schedule(store, base, job, rows)


def _category(info: dict, source: str) -> str:
    """Prefer the name chosen at scan time when the release name is ambiguous."""
    if info.get("kind") == "series" and info.get("season"):
        return "series"
    if info.get("kind") == "movies" and info.get("year"):
        return "movie"
    return category_for(source)


def _kind(job, index: int) -> str:
    from .media import classify_path

    source = next((item.path for item in job.meta.files if item.index == index), "")
    return classify_path(source)[0]


def _saved_rows(engine, meta, selected: set[int], categories: dict, title: str = "") -> list:
    from ... import m3u
    from .media import classify_path
    from .rename import library_path,previous_library_path,existing_saved_path

    layout = engine.library_layout() if callable(getattr(engine, "layout", None)) else {}
    profile = str(layout.get("profile") or "emby")
    folders = {"movies": layout.get("movies") or "Movies", "series": layout.get("series") or "TV Shows"}
    rows = []
    base = Path(engine.media_base)
    for item in meta.files:
        if item.index not in selected or classify_path(item.path)[0] != "video":
            continue
        relative = library_path(meta.name, item.path, "video", categories.get(item.index), profile, folders, title or None)
        previous=previous_library_path(meta.name,item.path,'video',categories.get(item.index),profile,folders,title or None)
        adopted=existing_saved_path(base,relative,previous,item.length)
        if adopted:rows.append((item.index,adopted,'video'))
    return rows


@metadata
def _save(store, base: Path, job, index: int, relative: str, category: str, info: dict) -> None:
    engine = job.engine
    if getattr(engine, 'source_type', '') not in {'nzb', 'torrent'}:
        return _save_active(store, base, job, index, relative, category, info)
    generation = engine.config.get('metadata_generation', '')
    def enabled():
        return (not engine.stop.is_set() and not job.removed and
                engine.config.get('share_metadata', True) and
                engine.config.get('metadata_generation', '') == generation)
    try:
        with work(enabled):
            return _save_active(store, base, job, index, relative, category, info)
    except Cancelled:
        _remember(job, index, 'Metadata cancelled in Settings.', False)
        engine._metadata_caught = False


def _save_active(store, base: Path, job, index: int, relative: str, category: str, info: dict) -> None:
    finished = True
    try:
        checkpoint()
        choice = dict(job.metadata_choice.get(index) or {})
        if choice.get("mode") == "manual":
            write_manual(store, base, relative, choice.get("media_type") or "movie", choice.get("data") or {})
            note = ""
            _remember(job, index, note, True)
            return
        if choice.get("mode") == "tmdb" and str(choice.get("tmdb_id") or "").isdigit():
            info = dict(info, provider="tmdb", identity=str(choice["tmdb_id"]))
            category = "series" if choice.get("media_type") == "tv" else "movie"
        if not store.get("m3u_tmdb_token"):
            finished=False
            write_basic(store,base,relative,category,info)
            try:
                from ...emby import queue_refresh
                if store.get('emby_url') and store.get('emby_auto_refresh')=='true':queue_refresh(store,'torrent-'+job.id[:12])
            except Exception:pass
            _remember(job,index,BASIC_NOTE,False)
            return
        fetch(store, base, relative, category, info)
        checkpoint()
        note = ""
        try:
            from ...emby import queue_refresh
            if store.get("emby_url") and store.get("emby_auto_refresh") == "true":
                queue_refresh(store, "torrent-" + job.id[:12])
        except Exception:
            pass
    except Cancelled:
        raise
    except ValueError as exc:
        note = str(exc)[:200]
    except Exception:
        finished = False
        note = "Metadata could not be saved. The renamed file is still in place."
    _remember(job, index, note, finished)


def _remember(job, index: int, note: str, finished: bool) -> None:
    with job._lock:
        job.metadata_note = note
        if finished:
            job.metadata_done.add(index)
        else:
            job.metadata_started.discard(index)
    folder = getattr(job.engine, "state_dir", None) or (job.engine.data_dir / "torrents")
    path = Path(folder) / f"{job.id}.json"
    def change(payload):
        payload["metadata_note"] = note
        if finished:
            done = []
            for item in payload.get("metadata_done") or []:
                try:
                    done.append(int(item))
                except (TypeError, ValueError):
                    continue
            if index not in done:
                done.append(index)
            payload["metadata_done"] = done
    try:
        from ..job_state import update
        update(path,change)
    except (OSError, json.JSONDecodeError):
        return


def primary_video(job):
    from .media import classify_path

    if job.meta is None:
        raise ValueError("This torrent has no video to match.")
    videos = [item for item in job.meta.files if item.index in job.selected and classify_path(item.path)[0] == "video"]
    if not videos:
        raise ValueError("This torrent has no video to match.")
    return max(videos, key=lambda item: item.length)


def _card(matched: bool, title, year, genres, rating, overview, cast, poster, media_type, message="", identity=None) -> dict:
    result = {
        "matched": matched,
        "title": title or "",
        "year": str(year or ""),
        "genres": [str(item) for item in genres if item],
        "rating": rating if isinstance(rating, (int, float)) else None,
        "overview": overview or "",
        "cast": [str(item) for item in cast if item][:12],
        "poster": poster or "",
        "media_type": "tv" if media_type == "tv" else "movie",
        "message": message,
    }
    if matched and identity is not None:
        from ...activity_media import tmdb_item
        item=tmdb_item(identity,media_type,result)
        if item:result['mediaItem']=item
    return result


def matched_save_as(card: dict) -> str:
    """Save-as text from a found title. A show year is the year it started."""
    from .rename import sanitize

    if not card.get("matched"):
        return ""
    title = sanitize(str(card.get("title") or ""))
    if not title or title == "untitled":
        return ""
    year = str(card.get("year") or "")
    if re.fullmatch(r"(?:19|20)\d{2}", year):
        return f"{title} ({year})"
    return title


def scan_lookup(store, file_path: str, save_as: str = "", client=None) -> dict:
    """Look up a scanned video on TMDB before the download starts."""
    info = named_info(file_path, save_as)
    return _lookup_card(store, info, _category(info, file_path), client)


def preview(store, job, index: int | None = None, client=None) -> dict:
    """Look up one torrent video on TMDB without saving files."""
    item = primary_video(job) if index is None else next((file for file in job.meta.files if file.index == index), None)
    if item is None:
        raise ValueError("That file is not part of this download.")
    info = named_info(item.path, getattr(job, "save_as", ""))
    category = job.categories.get(item.index) or _category(info, item.path)
    return _lookup_card(store, info, category, client, job.metadata_choice.get(item.index) or {})


@metadata
def _lookup_card(store, info: dict, category: str, client=None, choice: dict | None = None) -> dict:
    from ...m3u_player import portrait

    choice = choice or {}
    kind = choice.get("media_type") if choice.get("media_type") in {"movie", "tv"} else ("tv" if category == "series" or info.get("season") else "movie")
    if choice.get("mode") == "manual":
        data = choice.get("data") or {}
        return _card(True, data.get("title") or info.get("name"), data.get("year"), data.get("genres") or [], data.get("rating"), data.get("description"), data.get("cast") or [], data.get("artwork") or "", kind, "Using your own details.")
    if client is None:
        if not store.get("m3u_tmdb_token"):
            card=_card(False, info.get("name"), info.get("year"), [], None, "", [], "", kind, "Basic local metadata will be created from the file name. No TMDB login is needed.")
            if choice.get('mode')=='tmdb' and type(choice.get('tmdb_id')) is int and 0<choice['tmdb_id']<2147483648 and choice.get('media_type') in ('movie','tv'):
                from ...activity_media import tmdb_item
                saved=tmdb_item(choice['tmdb_id'],kind,dict(card,title=(choice.get('data') or {}).get('title') or card['title']))
                if saved:card['mediaItem']=saved
            return card
        client = TMDB(store)
    if choice.get("mode") == "tmdb" and str(choice.get("tmdb_id") or "").isdigit():
        found = {"id": int(choice["tmdb_id"])}
    else:
        year = info.get("year") if kind == "movie" or info.get("use_year") else None
        found = choose(client.get("search/" + kind, query=info.get("name") or "").get("results", []), info.get("name") or "", year)
    if not found:
        return _card(False, info.get("name"), info.get("year"), [], None, "", [], "", kind, "No unique metadata match for this title.")
    entity = client.get(f"{kind}/{found['id']}", append_to_response="credits") or {}
    if not entity:
        return _card(False, info.get("name"), info.get("year"), [], None, "", [], "", kind, "Metadata details were not available.")
    day = str(entity.get("release_date") or entity.get("first_air_date") or "")
    cast = [person.get("name") for person in (entity.get("credits") or {}).get("cast") or [] if person.get("name")]
    return _card(
        True,
        entity.get("title") or entity.get("name") or info.get("name"),
        day[:4],
        [genre.get("name") for genre in entity.get("genres") or [] if genre.get("name")],
        entity.get("vote_average"),
        entity.get("overview") or "",
        cast,
        portrait(entity.get("poster_path")),
        kind,
        identity=found['id'] if not entity.get('id') or str(entity['id'])==str(found['id']) else None,
    )


def _source_name(job) -> str:
    name = str(getattr(job.meta, "name", "") or "")
    return "" if name.casefold() in ("discover", "discover.nzb") else name


def match_state(store, job, index: int) -> dict:
    item = next((file for file in job.meta.files if file.index == index), None)
    if item is None:
        raise ValueError("That file is not part of this download.")
    info = named_info(item.path, getattr(job, "save_as", ""))
    choice = job.metadata_choice.get(index) or {}
    category = job.categories.get(index) or _category(info, item.path)
    media_type = choice.get("media_type") if choice.get("media_type") in {"movie", "tv"} else ("tv" if category == "series" or info.get("season") else "movie")
    storage = getattr(job, "storage", None)
    published = (getattr(storage, "published", None) or {}).get(index) or item.path
    return {
        "locked": bool(choice.get("mode")),
        "mode": choice.get("mode") or "",
        "data": choice.get("data") if choice.get("mode") == "manual" else {"title": (choice.get("data") or {}).get("title", "")},
        "media_type": media_type,
        "name": info.get("name") or item.path,
        "year": info.get("year") or "",
        "tmdb_saved": bool(store.get("m3u_tmdb_token")),
        "filename": Path(published).name,
        "original_filename": Path(item.path).name,
        "source_name": _source_name(job),
    }


def apply_match(store, base: Path, job, index: int, action: str, payload: dict) -> dict:
    item = next((file for file in job.meta.files if file.index == index), None)
    if item is None:
        raise ValueError("That file is not part of this download.")
    if action == "search":
        from ...m3u_overrides import search
        return {"results": search(store, payload.get("query"), payload.get("year"), payload.get("media_type"))}
    if action == "revert":
        job.metadata_choice.pop(index, None)
        job.metadata_done.discard(index)
        job.metadata_started.discard(index)
        _rewrite(store, base, job, index, item.path)
        job.engine._save_job(job)
        return match_state(store, job, index)
    if action == "tmdb":
        kind = payload.get("media_type")
        identity = payload.get("tmdb_id")
        if kind not in {"movie", "tv"} or type(identity) is not int or not 0 < identity < 2147483648:
            raise ValueError("Choose a TMDB result.")
        entity = TMDB(store).get(f"{kind}/{identity}")
        if not entity.get("id"):
            raise ValueError("That TMDB title is unavailable.")
        title = entity.get("title") or entity.get("name") or ""
        day = str(entity.get("release_date") or entity.get("first_air_date") or "")
        previous = job.categories.get(index)
        job.categories[index] = "series" if kind == "tv" else "movie"
        try:
            _retitle(base, job, index, item.path, title, day[:4])
        except Exception:
            if previous is None:
                job.categories.pop(index, None)
            else:
                job.categories[index] = previous
            raise
        job.metadata_choice[index] = {"mode": "tmdb", "media_type": kind, "tmdb_id": identity, "data": {"title": title}}
    elif action == "manual":
        described = _describe(item.path)
        category = job.categories.get(index) or category_for(item.path)
        media_type = "tv" if category == "series" or described.get("season") else "movie"
        data = _manual_data(payload.get("fields"))
        data["media_type"] = media_type
        previous = job.categories.get(index)
        job.categories[index] = "series" if media_type == "tv" else "movie"
        try:
            _retitle(base, job, index, item.path, data.get("title") or "", str(data.get("year") or ""))
        except Exception:
            if previous is None:
                job.categories.pop(index, None)
            else:
                job.categories[index] = previous
            raise
        job.metadata_choice[index] = {"mode": "manual", "media_type": media_type, "data": data}
    else:
        raise ValueError("Choose a match action.")
    job.metadata_done.discard(index)
    job.metadata_started.discard(index)
    job.engine._save_job(job)
    _rewrite(store, base, job, index, item.path)
    job.engine._save_job(job)
    return match_state(store, job, index)


def _manual_data(fields) -> dict:
    if not isinstance(fields, dict):
        raise ValueError("Enter the title details.")
    def text(name, limit):
        value = fields.get(name, "")
        if value is None:
            value = ""
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError("Check the details you entered.")
        value = str(value).strip()
        if len(value) > limit:
            raise ValueError(name.capitalize() + " is too long.")
        return value
    title = text("title", 300)
    if not title:
        raise ValueError("Enter a title.")
    year = text("year", 4)
    if year and (len(year) != 4 or not year.isdigit()):
        raise ValueError("Enter a four-digit year, or leave it blank.")
    rating = text("rating", 5)
    number = None
    if rating:
        try:
            number = round(float(rating), 1)
        except ValueError as exc:
            raise ValueError("Enter a rating from 0 to 10.") from exc
        if not 0 <= number <= 10:
            raise ValueError("Enter a rating from 0 to 10.")
    def names(name, count):
        values = [part.strip() for part in text(name, 4000).split(",") if part.strip()]
        if len(values) > count:
            raise ValueError("Use fewer " + name + " names.")
        return values
    return {
        "manual": True,
        "media_type": "movie",
        "title": title,
        "year": int(year) if year else None,
        "rating": number,
        "description": text("plot", 10000),
        "genres": names("genres", 20),
        "cast": names("cast", 50),
        "poster_url": text("poster_url", 2048),
    }


def _retitle(base: Path, job, index: int, source: str, title: str, year: str) -> None:
    """Use the corrected title for the activity name, the folder, and the filename."""
    label = matched_save_as({"matched": True, "title": title, "year": year})
    if not label:
        return
    layout = job.engine.library_layout()
    folders = {"movies": layout["movies"], "series": layout["series"]}
    category = job.categories.get(index)
    new_video = library_path(getattr(job.meta, "name", ""), source, "video", category, layout["profile"], folders, label)
    published = job.storage.published if job.storage is not None else {}
    old_video = published.get(index)
    if old_video and new_video and old_video != new_video:
        _move_library(base, old_video, new_video)
        old_root = "/".join(old_video.split("/")[:2])
        new_root = "/".join(new_video.split("/")[:2])
        old_stem = Path(old_video).stem
        new_stem = Path(new_video).stem
        old_folder = old_video.split("/")[1] if "/" in old_video else ""
        new_folder = new_video.split("/")[1] if "/" in new_video else ""
        for key, relative in list(published.items()):
            published[key] = _mapped_relative(relative, old_root, new_root, old_stem, new_stem, old_folder, new_folder)
        published[index] = new_video
    job.save_as = label


def _move_library(base: Path, old_video: str, new_video: str) -> None:
    from ... import m3u

    old_root = "/".join(old_video.split("/")[:2])
    new_root = "/".join(new_video.split("/")[:2])
    source = m3u.contained(base, old_video)
    if not source.is_file() or source.is_symlink():
        raise ValueError("The saved video could not be renamed.")
    if old_root == new_root:
        _rename_inside(m3u.contained(base, old_root), Path(old_video).stem, Path(new_video).stem, "", "")
        return
    origin = m3u.contained(base, old_root)
    destination = m3u.contained(base, new_root)
    if destination.exists():
        raise ValueError(destination.name + " is already in the library.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        origin.rename(destination)
    except OSError as exc:
        raise ValueError("The corrected name could not be saved.") from exc
    _rename_inside(destination, Path(old_video).stem, Path(new_video).stem, origin.name, destination.name)
    moved = m3u.contained(base, new_video)
    if not moved.is_file():
        raise ValueError("The corrected name could not be saved.")


def _rename_inside(folder: Path, old_stem: str, new_stem: str, old_folder: str, new_folder: str) -> None:
    paths = [path for path in folder.rglob("*") if path.is_file() and not path.is_symlink()]
    for path in paths:
        updated = _swap_prefix(path.name, old_stem, new_stem)
        if updated == path.name and old_folder and old_folder != new_folder:
            updated = _swap_prefix(path.name, old_folder, new_folder)
        if updated == path.name:
            continue
        target = path.with_name(updated)
        if target.exists():
            raise ValueError(updated + " is already in the library.")
        path.rename(target)


def _swap_prefix(name: str, old: str, new: str) -> str:
    if not old or old == new or not name.startswith(old):
        return name
    if len(name) == len(old):
        return new
    if name[len(old)].isalnum():
        return name
    return new + name[len(old):]


def _mapped_relative(relative: str, old_root: str, new_root: str, old_stem: str, new_stem: str, old_folder: str, new_folder: str) -> str:
    prefix = old_root + "/"
    if relative != old_root and not relative.startswith(prefix):
        return relative
    rest = relative[len(old_root):].lstrip("/")
    parts = rest.split("/") if rest else []
    if parts:
        updated = _swap_prefix(parts[-1], old_stem, new_stem)
        if updated == parts[-1]:
            updated = _swap_prefix(parts[-1], old_folder, new_folder)
        parts[-1] = updated
    return new_root + ("/" + "/".join(parts) if parts else "")


def saved_relative(job, index: int, source: str) -> str | None:
    if job.storage is not None and job.storage.published.get(index):
        return job.storage.published[index]
    from .rename import library_path
    layout = job.engine.library_layout()
    category = job.categories.get(index)
    return library_path(job.meta.name, source, "video", category, layout["profile"], {"movies": layout["movies"], "series": layout["series"]}, getattr(job, "save_as", "") or None)


@metadata
def _rewrite(store, base: Path, job, index: int, source: str) -> None:
    engine = job.engine
    config = getattr(engine, 'config', {})
    generation = config.get('metadata_generation', '')
    def enabled():
        current = getattr(engine, 'config', {})
        return (current.get('share_metadata', True) and
                current.get('metadata_generation', '') == generation and
                not getattr(job, 'removed', False))
    try:
        with work(enabled):
            _rewrite_active(store, base, job, index, source)
    except Cancelled:
        return


def _rewrite_active(store, base: Path, job, index: int, source: str) -> None:
    relative = saved_relative(job, index, source)
    if not relative:
        return
    from ... import m3u
    try:
        target = m3u.contained(base, relative)
    except (OSError, ValueError):
        return
    if not target.is_file():
        return
    choice = job.metadata_choice.get(index) or {}
    info = named_info(source, getattr(job, "save_as", ""))
    category = "series" if choice.get("media_type") == "tv" or job.categories.get(index) == "series" else "movie"
    if choice.get("mode") == "manual":
        write_manual(store, base, relative, choice.get("media_type") or "movie", choice.get("data") or {})
        return
    if choice.get("mode") == "tmdb" and str(choice.get("tmdb_id") or "").isdigit():
        info = dict(info, provider="tmdb", identity=str(choice["tmdb_id"]))
        category = "series" if choice.get("media_type") == "tv" else "movie"
    elif not store.get("m3u_tmdb_token"):
        return write_basic(store,base,relative,category,info)
    fetch(store, base, relative, category, info)


@metadata
def write_manual(store, base: Path, relative: str, media_type: str, data: dict) -> None:
    from ... import m3u
    from ...m3u_sidecars import owned_write, xml
    target = m3u.contained(base, relative)
    if not target.is_file():
        return
    payload = {
        "title": data.get("title") or "",
        "overview": data.get("description") or "",
        "year": data.get("year"),
        "vote_average": data.get("rating"),
        "genres": [{"name": name} for name in data.get("genres") or []],
        "credits": {"cast": [{"name": name, "character": ""} for name in data.get("cast") or []]},
    }
    folder = target.parent.parent if media_type == "tv" and target.parent.name.lower().startswith("season") else target.parent
    if media_type == "tv":
        _write_nfo(store, base, folder / "tvshow.nfo", xml("tvshow", payload))
        _write_nfo(store, base, target.with_suffix(".nfo"), xml("episodedetails", payload))
    else:
        _write_nfo(store, base, target.with_suffix(".nfo"), xml("movie", payload))
    poster = data.get("poster_url") or ""
    if not poster:
        return
    from ...m3u_overrides import fetch_poster
    raw = fetch_poster(poster)
    owned_write(store, base, (folder if media_type == "tv" else target.parent) / "poster.jpg", raw)
