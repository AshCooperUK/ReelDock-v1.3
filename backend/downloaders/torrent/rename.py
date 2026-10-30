"""Place finished torrent media with the selected media-server profile.

A video whose name has a season and episode goes under TV Shows. A video with
a year and no episode goes under Movies. Audio stays in Downloads/Torrents/Audio.
A name that could be either waits until someone chooses. Subtitles, artwork
and .nfo files are renamed beside that video.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from ...m3u_naming import describe

_OPTIONS = {
    "scrub": [],
    "replace": {},
    "cleaners": [],
    "numeric_ids": "auto",
    "remove": [],
    "episode_maps": [],
}
_PROFILES = {"emby", "plex", "kodi"}
_ARTWORK = {"poster", "fanart", "banner", "landscape", "logo", "clearlogo", "thumb", "folder", "cover", "backdrop"}
_LANG = {
    "en", "eng", "fr", "fre", "fra", "es", "spa", "de", "ger", "deu", "it", "ita",
    "pt", "por", "nl", "dut", "sv", "swe", "no", "nor", "da", "dan", "fi", "fin",
    "pl", "pol", "ru", "rus", "ja", "jpn", "zh", "chi", "zho", "ko", "kor", "cs",
    "hu", "tr", "ar", "he", "el", "hi",
}


def sanitize(name: str) -> str:
    cleaned = name.replace("\\", " ").replace("/", " ")
    cleaned = re.sub(r"[\x00-\x1f]", "", cleaned)
    cleaned = cleaned.replace(":", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if cleaned in {"", ".", ".."}:
        return "untitled"
    return cleaned[:180].rstrip(" .")


def _stem_ext(filename: str) -> tuple[str, str]:
    base = filename.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0:
        return sanitize(base), ""
    return sanitize(base[:dot]), base[dot:].lower()


def _spaced(stem: str) -> str:
    return re.sub(r"\s+", " ", stem.replace(".", " ")).strip()


def _describe(file_path: str) -> dict:
    stem, _ext = _stem_ext(file_path)
    spaced = _spaced(stem)
    identity = ""
    found = re.search(r"(?i)\b(tt\d{7,8})\b", spaced)
    if found:
        identity = found.group(1).lower()
        spaced = (spaced[: found.start()] + " " + spaced[found.end() :]).strip()
    info = describe(spaced, "0", "", "", _OPTIONS)
    if identity:
        info["provider"] = "imdb"
        info["identity"] = identity
    return info


def category_for(file_path: str) -> str:
    """Return movie, series, or unsure from the release name alone."""
    info = _describe(file_path)
    if info["kind"] in {"series", "tv"}:
        return "series"
    if info["kind"] == "movies" and info.get("year"):
        return "movie"
    return "unsure"


def release_save_as(value: str) -> str:
    """Turn a scene release into the library name. A chosen Title (Year) is left as typed."""
    text = str(value or "").strip()
    # A retry may receive a title which already contains its catalogue year.
    text = re.sub(r"(\s+\((?:19|20)\d{2}\))(?:\1)+$", r"\1", text)
    parsed = parse_save_as(text)
    if parsed.get("year") or parsed.get("episode"):
        return text
    if not re.search(r"[._]|(?:1080|720|2160)p|WEB|BluRay|HDTV", text, re.I):
        return text
    info = _describe(text if Path(text).suffix else text + ".mkv")
    name = str(info.get("name") or "").strip()
    if not name or name.casefold() == text.casefold():
        return text
    if info.get("season") and info.get("episode"):
        year = f" ({int(info['year'])})" if info.get("year") else ""
        return f"{name}{year} S{int(info['season']):02d}E{int(info['episode']):02d}"
    if info.get("year"):
        return f"{name} ({int(info['year'])})"
    return name


def suggested_title(file_path: str) -> str:
    """Clean movie title and year, or TV show name and season, from a release name."""
    info = _describe(file_path)
    name = str(info.get("name") or "").strip() or "Untitled"
    if info["kind"] in {"series", "tv"} and info.get("season"):
        return f"{name} Season {int(info['season'])}"
    if info.get("year"):
        return f"{name} ({int(info['year'])})"
    return name


def suggested_from_files(files) -> str:
    """Recommendation from the largest video in a scanned torrent."""
    videos = [item for item in files if getattr(item, "kind", "") == "video" and getattr(item, "selectable", False)]
    if not videos:
        return ""
    return suggested_title(max(videos, key=lambda item: item.size).path)


_ID_TAG = re.compile(r"(?i)\s+(?:\[[^\]]*\b(?:imdb|tmdb|tvdb)id[=\-][^\]]+\]|\{(?:imdb|tmdb|tvdb)-[^}]+\})\s*$")
_YEAR_SUFFIX = re.compile(r"\s+\(((?:19|20)\d{2})\)$")
_ARTICLE = re.compile(r"^(?:the|a|an)\s+")


def _bare_folder(value: str) -> str:
    text = str(value or "").strip()
    while True:
        updated = _ID_TAG.sub("", text).strip()
        if updated == text:
            return text
        text = updated


def _folder_year(value: str) -> int | None:
    found = _YEAR_SUFFIX.search(_bare_folder(value))
    return int(found.group(1)) if found else None


def _match_key(value: str) -> str:
    """Compare titles while ignoring case, punctuation, a leading article and a year."""
    text = unicodedata.normalize("NFKC", _bare_folder(value)).casefold().replace("'", "").replace("’", "")
    text = _YEAR_SUFFIX.sub("", text).strip()
    text = _ARTICLE.sub("", text)
    return "".join(character for character in text if character.isalnum())


def list_library_dirs(root: Path) -> list[str]:
    """Child folders in one library directory. Symlinks and hidden names are skipped."""
    if not root.is_dir() or root.is_symlink():
        return []
    found = []
    try:
        with os.scandir(root) as scan:
            for entry in scan:
                name = entry.name
                if name.startswith(".") or name in {".", ".."} or "/" in name or "\\" in name:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        found.append(name)
                except OSError:
                    continue
    except OSError:
        return []
    return found


def match_folder(name: str, folders: list[str], year: int | None = None) -> str | None:
    """Return the existing folder whose title matches, preferring the same year."""
    wanted = _match_key(name)
    if not wanted:
        return None
    hits = []
    for folder in folders:
        if _match_key(folder) != wanted:
            continue
        hits.append((folder, _folder_year(folder)))
    if not hits:
        return None
    if year is not None:
        dated = [folder for folder, folder_year in hits if folder_year == year]
        if len(dated) == 1:
            return dated[0]
        if dated:
            return None
    if len(hits) == 1:
        return hits[0][0]
    dated = [folder for folder, folder_year in hits if folder_year]
    if len(dated) == 1:
        return dated[0]
    exact = [folder for folder, _folder_year in hits if folder.casefold() == str(name).casefold()]
    if len(exact) == 1:
        return exact[0]
    return None


def _library_roots(base: Path, root_name: str) -> list[tuple[str, Path]]:
    roots = [(root_name, base / root_name)]
    downloads = base / "Downloads" / root_name
    if downloads != roots[0][1]:
        roots.append((f"Downloads/{root_name}", downloads))
    return roots


def find_library_folder(base: Path, root_name: str, name: str, year: int | None = None) -> tuple[str, str] | None:
    """Find a similar movie or show folder. The torrent library is checked before Downloads."""
    for label, root in _library_roots(base, root_name):
        matched = match_folder(name, list_library_dirs(root), year)
        if matched:
            return matched, f"{label}/{matched}"
    return None


def suggest_download(files, base: Path | None, layout: dict | None) -> dict:
    """Save-as text, using an existing library folder when the title already has one."""
    videos = [item for item in files if getattr(item, "kind", "") == "video" and getattr(item, "selectable", False)]
    if not videos:
        return {"name": "", "folder": ""}
    path = max(videos, key=lambda item: item.size).path
    info = _describe(path)
    name = str(info.get("name") or "").strip() or "Untitled"
    names = _folder_names(layout)
    plain = suggested_title(path)
    if base is None:
        return {"name": plain, "folder": ""}
    series = info["kind"] in {"series", "tv"} and (info.get("season") or info.get("episode") or info.get("day"))
    if series:
        found = find_library_folder(base, names["series"], name, info.get("year"))
        if not found:
            return {"name": plain, "folder": ""}
        folder_name, relative = found
        return {"name": folder_name, "folder": relative}
    if info.get("year") or info["kind"] == "movies":
        found = find_library_folder(base, names["movies"], name, info.get("year"))
        if found:
            return {"name": found[0], "folder": found[1]}
        return {"name": plain, "folder": ""}
    for root_name in (names["series"], names["movies"]):
        found = find_library_folder(base, root_name, name, info.get("year"))
        if not found:
            continue
        return {"name": found[0], "folder": found[1]}
    return {"name": plain, "folder": ""}


def align_existing_folder(base: Path, relative: str, cache: dict | None = None) -> str:
    """Use the existing show or movie directory when this path is only a spelling variant."""
    parts = relative.split("/")
    if len(parts) < 3 or parts[1] in {"", ".", ".."} or parts[1].startswith("Season "):
        return relative
    root, folder = parts[0], parts[1]
    if cache is None:
        cache = {}
    if root not in cache:
        cache[root] = list_library_dirs(base / root)
    matched = match_folder(folder, cache[root])
    if not matched or matched == folder:
        return relative
    parts[1] = matched
    leaf = parts[-1]
    if leaf.startswith(folder):
        parts[-1] = matched + leaf[len(folder):]
    return "/".join(parts)


def parse_save_as(title: str, *, _legacy=False) -> dict:
    """Read a Save as value. Season marks a TV show. A trailing (year) marks a movie."""
    label = sanitize(title)
    episode_pattern = r"(?i)\s+S(\d{1,4})E(\d{1,4})((?:E\d{1,4})*)(?:\s+-\s+(.+))?$" if _legacy else r"(?i)\s+S(\d{1,4})\s*[-._]?\s*E(\d{1,4})((?:E\d{1,4})*)(?:\s+-\s+(.+))?$"
    episode = re.search(episode_pattern, label)
    original_episode = re.search(episode_pattern, title.strip())
    episode_title = (original_episode.group(4) or '').strip() if original_episode else ''
    episode_number = None
    extra = ""
    if episode and episode.group(1) and episode.group(2):
        episode_number = int(episode.group(2))
        extra = "".join(f"E{int(part):02d}" for part in re.findall(r"(?i)E(\d+)", episode.group(3) or ""))
        label = label[: episode.start()].strip() if _legacy else label[: episode.start()].rstrip(' .-_')
        season_number = int(episode.group(1))
    else:
        season_number = None
    season = re.fullmatch(r"(?i)(.+?)\s+Season\s+(\d{1,4})", label)
    if season and season.group(1).strip():
        return {"name": season.group(1).strip(), "year": None, "season": int(season.group(2)), "episode": episode_number, "extra": extra, "episode_title": episode_title}
    year = re.fullmatch(r"(.+?)\s+\(((?:19|20)\d{2})\)", label)
    if year and year.group(1).strip():
        return {"name": year.group(1).strip(), "year": int(year.group(2)), "season": season_number, "episode": episode_number, "extra": extra, "episode_title": episode_title}
    return {"name": label, "year": None, "season": season_number, "episode": episode_number, "extra": extra, "episode_title": episode_title}


def named_info(file_path: str, title: str = "", *, _legacy=False) -> dict:
    """Describe a file, then replace the title with the name chosen at scan time."""
    info = dict(_describe(file_path))
    if not str(title or "").strip():
        return info
    parsed = parse_save_as(title,_legacy=_legacy)
    info["name"] = parsed["name"]
    if parsed.get('episode_title'):info['episode_title']=parsed['episode_title']
    series = info["kind"] in {"series", "tv"} and (info.get("season") or info.get("episode") or info.get("day"))
    if parsed.get("episode"):
        info["season"] = parsed["season"]
        info["episode"] = parsed["episode"]
        info["extra"] = parsed.get("extra") or ""
        info["kind"] = "series"
        series = True
    if parsed["season"] and not parsed.get("episode"):
        info["season"] = parsed["season"]
        info["kind"] = "series"
        embedded = re.fullmatch(r"(.+?)\s+\(((?:19|20)\d{2})\)", parsed["name"])
        if embedded and embedded.group(1).strip():
            info["name"] = embedded.group(1).strip()
            info["year"] = int(embedded.group(2))
            info["use_year"] = True
        else:
            info["year"] = None
    elif parsed["year"] and series:
        info["year"] = parsed["year"]
        info["kind"] = "series"
        info["use_year"] = True
    elif parsed["year"]:
        info["year"] = parsed["year"]
        info["kind"] = "movies"
        info["season"] = None
        info["episode"] = None
        info["extra"] = ""
    else:
        info["year"] = None
    return info


def _id_tag(info: dict, profile: str) -> str:
    provider, identity = info.get("provider"), info.get("identity")
    if provider not in {"imdb", "tmdb", "tvdb"} or not identity:
        return ""
    if profile == "plex":
        return f" {{{provider}-{identity}}}"
    separator = "=" if profile == "emby" else "-"
    return f" [{provider}id{separator}{identity}]"


def library_path(
    torrent_name: str,
    file_path: str,
    kind: str,
    category: str | None = None,
    profile: str = "emby",
    folders: dict | None = None,
    title: str | None = None,
    *, flat_episodes: bool = False, _legacy_save_as: bool = False,
) -> str | None:
    """Return a path relative to the ReelDock base, or None until a choice is made."""
    del torrent_name
    from ...m3u import filename

    stem, ext = _stem_ext(file_path)
    if kind == "audio":
        return f"Downloads/Torrents/Audio/{stem}{ext}"
    if kind != "video":
        return None
    if title and str(title).strip():
        return _named_video(file_path, ext, category, profile, _folder_names(folders), filename, title, flat_episodes, _legacy=_legacy_save_as)
    names = _folder_names(folders)
    chosen = category if category in {"movie", "series"} else category_for(file_path)
    if chosen not in {"movie", "series"}:
        return None
    info = _describe(file_path)
    naming = profile if profile in _PROFILES else "emby"
    tag = _id_tag(info, naming)
    title = filename(info.get("name") or stem)
    if chosen == "series" and info.get("season") and info.get("episode"):
        season = int(info["season"])
        episode = int(info["episode"])
        extra = str(info.get("extra") or "").lower()
        leaf = filename(f"{title} s{season:02d}e{episode:02d}{extra}")
        return f"{names['series']}/{title}{tag}/Season {season}/{leaf}{ext}"
    if chosen == "series" and info.get("day"):
        leaf = filename(f"{title} - {info['day']}")
        return f"{names['series']}/{title}{tag}/{leaf}{ext}"
    if chosen == "series":
        leaf = filename(title)
        return f"{names['series']}/{title}{tag}/{leaf}{ext}"
    if info.get("year"):
        title = filename(f"{title} ({info['year']})")
    return f"{names['movies']}/{title}/{title}{ext}"


def _folder_names(folders: dict | None) -> dict:
    names = {"movies": "Movies", "series": "TV Shows"}
    if isinstance(folders, dict):
        if folders.get("movies"):
            names["movies"] = str(folders["movies"])
        if folders.get("series"):
            names["series"] = str(folders["series"])
    return names


def previous_library_path(torrent_name,file_path,kind,category=None,profile='emby',folders=None,title=None):
    """Only the former deterministic path for a label affected by parsing fixes."""
    if not title or parse_save_as(title)==parse_save_as(title,_legacy=True):return None
    return library_path(torrent_name,file_path,kind,category,profile,folders,title,_legacy_save_as=True)


def existing_saved_path(base,current,previous,size):
    """Adopt an exact-size regular old file only when the new path is absent."""
    import stat
    from ...m3u import contained
    if not current:return None
    try:
        path=contained(base,current)
        if path.exists():
            state=path.stat(follow_symlinks=False)
            return current if stat.S_ISREG(state.st_mode) and state.st_size==size else None
        if previous:
            path=contained(base,previous);state=path.stat(follow_symlinks=False)
            if stat.S_ISREG(state.st_mode) and state.st_size==size:return previous
    except (OSError,ValueError):pass
    return None


def _named_video(file_path: str, ext: str, category: str | None, profile: str, names: dict, filename, title: str, flat_episodes: bool = False, *, _legacy=False) -> str | None:
    """Save a video under the name chosen after the scan, before metadata is looked up."""
    info = named_info(file_path, title,_legacy=_legacy)
    naming = profile if profile in _PROFILES else "emby"
    parsed = parse_save_as(title,_legacy=_legacy)
    series = bool(parsed["season"]) or (category == "series") or (category not in {"movie", "series"} and info["kind"] in {"series", "tv"})
    movie = bool(parsed["year"]) or category == "movie" or (not series and (info.get("year") or info["kind"] == "movies"))
    if series and (parsed["season"] or info.get("season") or info.get("episode") or info.get("day")):
        show = filename(f"{parsed['name']} ({parsed['year']})" if parsed.get("year") else parsed["name"])
        tag = _id_tag(info, naming)
        season = parsed["season"] or info.get("season")
        if season and info.get("episode"):
            extra = str(info.get("extra") or "").lower()
            episode_title = str(parsed.get('episode_title') or '').strip()
            suffix = ' - ' + episode_title if episode_title else ''
            separator = ' - ' if flat_episodes else ' '
            leaf = filename(f"{show}{separator}s{int(season):02d}e{int(info['episode']):02d}{extra}{suffix}")
            if flat_episodes:
                return f"{names['series']}/{show}{tag}/{leaf}{ext}"
            return f"{names['series']}/{show}{tag}/Season {int(season)}/{leaf}{ext}"
        if info.get("day") and not parsed["season"]:
            leaf = filename(f"{show} - {info['day']}")
            return f"{names['series']}/{show}{tag}/{leaf}{ext}"
        if season:
            leaf = filename(f"{show} Season {int(season)}")
            return f"{names['series']}/{show}{tag}/Season {int(season)}/{leaf}{ext}"
        leaf = filename(show)
        return f"{names['series']}/{show}{tag}/{leaf}{ext}"
    if not movie and category not in {"movie", "series"} and category_for(file_path) == "unsure" and not parsed["year"]:
        return None
    if parsed["year"]:
        movie_name = filename(f"{parsed['name']} ({parsed['year']})")
    else:
        movie_name = filename(parsed["name"])
    return f"{names['movies']}/{movie_name}/{movie_name}{ext}"


_DISC = re.compile(r"(?i)(?:^|[^a-z0-9])(?:cd|dvd|part|disc|disk|pt)[-._ ]?([1-9])(?:[^a-z0-9]|$)")


def disc_number(name: str) -> int | None:
    """Read a split-disc marker such as cd1 or disc2 from a file name."""
    stem = Path(str(name or "")).name
    stem = stem.rsplit(".", 1)[0]
    found = _DISC.search(stem)
    if not found:
        return None
    return int(found.group(1))


def stack_split_movies(entries: list[tuple[int, str, str]]) -> dict[int, str]:
    """Name split movies the way Emby stacks them.

    Several videos that would share one movie file become
    Title (Year)-cd1.ext and Title (Year)-cd2.ext in that folder.
    A single movie and separate episodes keep their own names.
    """
    groups: dict[str, list[tuple[int, str, str]]] = {}
    for index, source, relative in entries:
        if not relative or "/" not in relative or "." not in relative.rsplit("/", 1)[-1]:
            continue
        folder, leaf = relative.rsplit("/", 1)
        stem, ext = leaf.rsplit(".", 1)
        groups.setdefault(f"{folder}\0{stem}", []).append((index, source, ext))
    changed: dict[int, str] = {}
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        folder, stem = key.split("\0", 1)
        assigned: dict[int, int] = {}
        used: set[int] = set()
        pending: list[int] = []
        extensions: dict[int, str] = {}
        for index, source, ext in rows:
            extensions[index] = ext
            number = disc_number(source)
            if number and number not in used:
                assigned[index] = number
                used.add(number)
            else:
                pending.append(index)
        next_number = 1
        for index in pending:
            while next_number in used:
                next_number += 1
            if next_number > 9:
                continue
            assigned[index] = next_number
            used.add(next_number)
        if len(assigned) < 2:
            continue
        for index, part in assigned.items():
            changed[index] = f"{folder}/{stem}-cd{part}.{extensions[index]}"
    return changed


def companion_path(primary_relative: str, companion_file: str) -> str:
    filename = companion_file.rsplit("/", 1)[-1]
    stem, ext = _stem_ext(filename)
    video = primary_relative.rsplit("/", 1)[-1]
    video_stem, _video_ext = _stem_ext(video)
    folder = primary_relative.rsplit("/", 1)[0] if "/" in primary_relative else "Movies"
    if stem.casefold() in _ARTWORK:
        show = folder.rsplit("/", 1)[0] if folder.rsplit("/", 1)[-1].startswith("Season ") else folder
        return f"{show}/{stem}{ext}"
    code = ""
    found = re.search(r"(?i)[._-]([a-z]{2,3})$", stem)
    if found and found.group(1).lower() in _LANG:
        code = "." + found.group(1).lower()
    return f"{folder}/{video_stem}{code}{ext}"
