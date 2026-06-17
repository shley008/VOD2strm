#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VOD2strm - Dispatcharr VOD to STRM exporter.

This version is intentionally incremental:
- It never removes the whole Movies or Series library directory.
- It only writes STRM files whose content changed.
- It deletes stale STRM files that no longer exist in Dispatcharr when DELETE_OLD=true.
- It can run in fast STRM-only mode by leaving ENABLE_NFO=false.

Required config may be provided in VOD2strm_vars.sh or environment variables.
Environment variables override the vars file.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
VARS_FILE = SCRIPT_DIR / "VOD2strm_vars.sh"


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
def load_vars(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


VARS = load_vars(VARS_FILE)


def setting(name: str, default: str = "", *aliases: str) -> str:
    for key in (name, *aliases):
        if os.getenv(key) is not None:
            return os.getenv(key, "")
        if key in VARS:
            return VARS[key]
    return default


def bool_setting(name: str, default: bool = False, *aliases: str) -> bool:
    raw_default = "true" if default else "false"
    return setting(name, raw_default, *aliases).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    dispatcharr_url: str
    dispatcharr_api_key: str
    dispatcharr_api_user: str
    dispatcharr_api_pass: str
    movies_dir_template: str
    series_dir_template: str
    cache_dir: Path
    log_file: Path
    xc_names: list[str]
    export_movies: bool
    export_series: bool
    delete_old: bool
    clear_cache: bool
    dry_run: bool
    enable_nfo: bool
    overwrite_nfo: bool
    log_level: str
    page_size: int
    limit_movies: int | None
    limit_series: int | None
    user_agent: str


def optional_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def build_config() -> Config:
    # New preferred names are DISPATCHARR_URL, DISPATCHARR_API_KEY, MOVIES_DIR, SERIES_DIR.
    # Old names are still accepted for compatibility.
    dispatcharr_url = setting("DISPATCHARR_URL", "http://127.0.0.1:9191", "DISPATCHARR_BASE_URL").strip().rstrip("/")
    if not dispatcharr_url.startswith(("http://", "https://")):
        dispatcharr_url = "http://" + dispatcharr_url

    xc_names_raw = setting("XC_NAMES", "*", "ACCOUNT_FILTERS")
    xc_names = [item.strip() for item in xc_names_raw.split(",") if item.strip()] or ["*"]

    return Config(
        dispatcharr_url=dispatcharr_url,
        dispatcharr_api_key=setting("DISPATCHARR_API_KEY", "", "API_TOKEN", "DISPATCHARR_API_TOKEN").strip(),
        dispatcharr_api_user=setting("DISPATCHARR_API_USER", "admin", "DISPATCHARR_USER").strip(),
        dispatcharr_api_pass=setting("DISPATCHARR_API_PASS", "", "DISPATCHARR_PASS").strip(),
        movies_dir_template=setting("MOVIES_DIR", "/mnt/Share-VOD/{XC_NAME}/Movies", "MOVIES_LIBRARY_PATH"),
        series_dir_template=setting("SERIES_DIR", "/mnt/Share-VOD/{XC_NAME}/Series", "TV_LIBRARY_PATH", "SERIES_LIBRARY_PATH"),
        cache_dir=Path(setting("CACHE_DIR", str(SCRIPT_DIR / "cache"))),
        log_file=Path(setting("LOG_FILE", str(SCRIPT_DIR / "VOD2strm.log"))),
        xc_names=xc_names,
        export_movies=bool_setting("EXPORT_MOVIES", True),
        export_series=bool_setting("EXPORT_SERIES", True),
        delete_old=bool_setting("DELETE_OLD", True),
        clear_cache=bool_setting("CLEAR_CACHE", False),
        dry_run=bool_setting("DRY_RUN", False),
        enable_nfo=bool_setting("ENABLE_NFO", False),
        overwrite_nfo=bool_setting("OVERWRITE_NFO", False),
        log_level=setting("LOG_LEVEL", "INFO").strip().upper(),
        page_size=optional_int(setting("PAGE_SIZE", "250")) or 250,
        limit_movies=optional_int(setting("LIMIT_MOVIES", "")),
        limit_series=optional_int(setting("LIMIT_SERIES", "")),
        user_agent=setting("HTTP_USER_AGENT", "VOD2strm/1.1"),
    )


CONFIG = build_config()
_CURRENT_TOKEN: str | None = None


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
def log(message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line)
    CONFIG.log_file.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG.log_file.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def debug(message: str) -> None:
    if CONFIG.log_level in {"DEBUG", "VERBOSE"}:
        log(message)


def progress(message: str) -> None:
    if CONFIG.log_level in {"DEBUG", "VERBOSE", "INFO"}:
        log(message)


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------
TAG_PATTERN = re.compile(r"(\b(4K|8K|1080p|720p|HDR10|HDR|H.264|H\.265|HEVC)\b|\[[^\]]+\])", re.IGNORECASE)
FS_SAFE_PATTERN = re.compile(r'[\\/:*?"<>|]+')
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def normalize_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title or "")
    title = TAG_PATTERN.sub("", title)
    title = re.sub(r"\s+", " ", title).strip(" -._")
    return title or "Untitled"


def fs_safe(name: str) -> str:
    cleaned = FS_SAFE_PATTERN.sub("_", (name or "").strip()).strip(" .")
    return cleaned or "_"


def account_matches(name: str) -> bool:
    return any(fnmatch.fnmatch(name or "", pattern) for pattern in CONFIG.xc_names)


def mkdir(path: Path) -> None:
    if CONFIG.dry_run:
        debug(f"[dry-run] Would create directory: {path}")
        return
    path.mkdir(parents=True, exist_ok=True)


def write_text_if_changed(path: Path, content: str) -> str:
    """Return added, updated, unchanged, or dry-run."""
    old = None
    if path.exists():
        try:
            old = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            old = None
    if old == content:
        return "unchanged"

    if CONFIG.dry_run:
        log(f"[dry-run] Would {'update' if path.exists() else 'create'}: {path}")
        return "dry-run"

    mkdir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return "updated" if old is not None else "added"


def remove_empty_dirs(root: Path) -> int:
    if not root.exists():
        return 0
    removed = 0
    for directory in sorted((p for p in root.glob("**/*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            if CONFIG.dry_run:
                debug(f"[dry-run] Would remove empty directory: {directory}")
            else:
                directory.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def find_first_key(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in keys and value not in (None, ""):
                return value
        for value in obj.values():
            found = find_first_key(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_key(item, keys)
            if found not in (None, ""):
                return found
    return None


def find_uuid(obj: Any, preferred_keys: set[str]) -> str | None:
    found = find_first_key(obj, preferred_keys)
    if found:
        return str(found)

    # Last-resort recursive UUID search.
    if isinstance(obj, dict):
        for value in obj.values():
            result = find_uuid(value, preferred_keys)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = find_uuid(item, preferred_keys)
            if result:
                return result
    elif isinstance(obj, str) and UUID_PATTERN.match(obj):
        return obj
    return None


def cache_path(account_name: str, name: str) -> Path:
    return CONFIG.cache_dir / fs_safe(account_name) / name


def load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"Failed reading cache {path}: {exc}")
        return None


def save_json(path: Path, data: Any) -> None:
    if CONFIG.dry_run:
        debug(f"[dry-run] Would write cache: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# -----------------------------------------------------------------------------
# Dispatcharr API
# -----------------------------------------------------------------------------
def headers(token: str | None = None) -> dict[str, str]:
    result = {"Accept": "application/json", "User-Agent": CONFIG.user_agent}
    auth_token = token or CONFIG.dispatcharr_api_key
    if auth_token:
        result["Authorization"] = f"Bearer {auth_token}"
    return result


def api_login() -> str | None:
    if CONFIG.dispatcharr_api_key:
        return CONFIG.dispatcharr_api_key
    if not CONFIG.dispatcharr_api_user or not CONFIG.dispatcharr_api_pass:
        return None

    url = f"{CONFIG.dispatcharr_url}/api/accounts/token/"
    response = requests.post(
        url,
        json={"username": CONFIG.dispatcharr_api_user, "password": CONFIG.dispatcharr_api_pass},
        headers={"User-Agent": CONFIG.user_agent},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Dispatcharr login failed ({response.status_code}): {response.text[:250]}")
    token = response.json().get("access")
    if not token:
        raise RuntimeError("Dispatcharr login succeeded but response did not include an access token")
    return token


def api_get(path: str, token: str | None = None, params: dict[str, Any] | None = None) -> Any | None:
    global _CURRENT_TOKEN

    url = f"{CONFIG.dispatcharr_url}{path}"
    response = requests.get(url, headers=headers(_CURRENT_TOKEN or token), params=params or {}, timeout=60)

    if response.status_code == 401 and not CONFIG.dispatcharr_api_key:
        log("WARNING: Dispatcharr returned 401; trying one re-login.")
        _CURRENT_TOKEN = api_login()
        response = requests.get(url, headers=headers(_CURRENT_TOKEN), params=params or {}, timeout=60)

    if not response.ok:
        log(f"HTTP {response.status_code} from {url}: {response.text[:250]}")
        return None
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def paginate(path: str, token: str | None, page_size: int, limit: int | None = None) -> Iterable[dict[str, Any]]:
    seen = 0
    page = 1
    while True:
        if limit is not None and seen >= limit:
            return
        params = {"page": page, "page_size": page_size}
        data = api_get(path, token, params)
        if data is None:
            return

        if isinstance(data, dict):
            items = data.get("results") or data.get("data") or data.get("items") or []
            has_next = bool(data.get("next"))
        elif isinstance(data, list):
            items = data
            has_next = len(items) >= page_size
        else:
            return

        if not items:
            return
        for item in items:
            if limit is not None and seen >= limit:
                return
            if isinstance(item, dict):
                yield item
                seen += 1
        if not has_next:
            return
        page += 1


def get_accounts(token: str | None) -> list[dict[str, Any]]:
    data = api_get("/api/m3u/accounts/", token)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("results") or data.get("data") or data.get("items") or []
    return []


def get_movie_provider_info(movie_id: Any, token: str | None) -> dict[str, Any]:
    if not movie_id:
        return {}
    data = api_get(f"/api/vod/movies/{movie_id}/provider-info/", token)
    return data if isinstance(data, dict) else {}


def get_series_provider_info(series_id: Any, token: str | None) -> dict[str, Any]:
    if not series_id:
        return {}
    data = api_get(f"/api/vod/series/{series_id}/provider-info/", token, {"include_episodes": "true"})
    return data if isinstance(data, dict) else {}


# -----------------------------------------------------------------------------
# STRM URL builders - required formats
# -----------------------------------------------------------------------------
def movie_strm_url(movie_uuid: str, stream_id: Any) -> str:
    query = urlencode({"stream_id": stream_id})
    return f"{CONFIG.dispatcharr_url}/proxy/vod/movie/{movie_uuid}?{query}"


def episode_strm_url(episode_uuid: str, account_id: Any) -> str:
    query = urlencode({"m3u_account_id": account_id})
    return f"{CONFIG.dispatcharr_url}/proxy/vod/episode/{episode_uuid}?{query}"


# -----------------------------------------------------------------------------
# Normalization
# -----------------------------------------------------------------------------
def movie_identity(movie: dict[str, Any], provider: dict[str, Any]) -> tuple[str | None, Any | None]:
    merged = {"movie": movie, "provider": provider}
    uuid = find_uuid(merged, {"uuid", "movie_uuid", "vod_uuid", "content_uuid"})
    stream_id = find_first_key(merged, {"stream_id", "streamid", "provider_stream_id", "providerstreamid"})
    return uuid, stream_id


def iter_normalized_episodes(provider: dict[str, Any]) -> Iterable[tuple[int, int, str, str]]:
    """Yield season, episode, title, episode_uuid."""
    episodes_obj = provider.get("episodes")

    if isinstance(episodes_obj, dict):
        for season_key, episodes in episodes_obj.items():
            try:
                season_num = int(season_key)
            except Exception:
                season_num = 1
            if isinstance(episodes, list):
                for episode in episodes:
                    normalized = normalize_episode(episode, season_num)
                    if normalized:
                        yield normalized
        return

    if isinstance(episodes_obj, list):
        for episode in episodes_obj:
            normalized = normalize_episode(episode, None)
            if normalized:
                yield normalized
        return

    seasons_obj = provider.get("seasons") or provider.get("Seasons") or []
    if isinstance(seasons_obj, list):
        for season in seasons_obj:
            if not isinstance(season, dict):
                continue
            season_num = as_int(season.get("number") or season.get("season_number") or season.get("season"), 1)
            for episode in season.get("episodes") or season.get("Episodes") or []:
                normalized = normalize_episode(episode, season_num)
                if normalized:
                    yield normalized


def normalize_episode(episode: Any, fallback_season: int | None) -> tuple[int, int, str, str] | None:
    if not isinstance(episode, dict):
        return None
    season_num = as_int(
        episode.get("season_number") or episode.get("season") or episode.get("season_num") or fallback_season,
        1,
    )
    episode_num = as_int(
        episode.get("episode_number") or episode.get("episode_num") or episode.get("num") or episode.get("episode"),
        0,
    )
    if episode_num <= 0:
        return None
    title = episode.get("title") or episode.get("name") or episode.get("episode_name") or f"Episode {episode_num}"
    uuid = find_uuid(episode, {"uuid", "episode_uuid", "content_uuid"})
    if not uuid:
        debug(f"Skipping S{season_num:02d}E{episode_num:02d}; no episode UUID found in provider-info")
        return None
    return season_num, episode_num, normalize_title(str(title)), uuid


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def category_for(item: dict[str, Any]) -> str:
    return fs_safe(str(item.get("genre") or item.get("category_name") or "Unsorted"))


# -----------------------------------------------------------------------------
# Exporters
# -----------------------------------------------------------------------------
def export_movies_for_account(token: str | None, account: dict[str, Any]) -> None:
    if not CONFIG.export_movies:
        log("EXPORT_MOVIES=false: skipping movies")
        return

    account_id = account.get("id")
    account_name = account.get("name") or f"Account-{account_id}"
    root = Path(CONFIG.movies_dir_template.replace("{XC_NAME}", str(account_name)))
    mkdir(root)
    log(f"=== Movies: {account_name} -> {root} ===")

    movies_cache = cache_path(str(account_name), "movies.json")
    movies = None if CONFIG.clear_cache else load_json(movies_cache)
    if movies is None:
        movies = list(paginate(f"/api/vod/movies/?m3u_account={account_id}", token, CONFIG.page_size, CONFIG.limit_movies))
        save_json(movies_cache, movies)
    log(f"Movies found for {account_name}: {len(movies)}")

    expected: set[Path] = set()
    stats = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}

    for index, movie in enumerate(movies, start=1):
        provider = get_movie_provider_info(movie.get("id"), token)
        movie_uuid, stream_id = movie_identity(movie, provider)
        if not movie_uuid or stream_id in (None, ""):
            stats["skipped"] += 1
            log(f"WARNING: Skipping movie without UUID/stream_id: {movie.get('name') or movie.get('title') or movie.get('id')}")
            continue

        title = normalize_title(str(movie.get("name") or movie.get("title") or "Untitled"))
        year = movie.get("year") or provider.get("year") or ""
        folder_name = fs_safe(f"{title} ({year})" if year else title)
        movie_dir = root / category_for(movie) / folder_name
        strm_path = movie_dir / f"{folder_name}.strm"
        expected.add(strm_path)

        result = write_text_if_changed(strm_path, movie_strm_url(movie_uuid, stream_id) + "\n")
        if result in stats:
            stats[result] += 1

        if index == 1 or index == len(movies) or index % 250 == 0:
            progress(f"Movies {account_name}: {index}/{len(movies)} processed")

    removed = cleanup_stale_strms(root, expected, "movie")
    log(f"Movies summary for {account_name}: {stats['added']} added, {stats['updated']} updated, {stats['unchanged']} unchanged, {removed} removed, {stats['skipped']} skipped")


def export_series_for_account(token: str | None, account: dict[str, Any]) -> None:
    if not CONFIG.export_series:
        log("EXPORT_SERIES=false: skipping series")
        return

    account_id = account.get("id")
    account_name = account.get("name") or f"Account-{account_id}"
    root = Path(CONFIG.series_dir_template.replace("{XC_NAME}", str(account_name)))
    mkdir(root)
    log(f"=== Series: {account_name} -> {root} ===")

    series_cache = cache_path(str(account_name), "series.json")
    series_list = None if CONFIG.clear_cache else load_json(series_cache)
    if series_list is None:
        series_list = list(paginate(f"/api/vod/series/?m3u_account={account_id}", token, CONFIG.page_size, CONFIG.limit_series))
        save_json(series_cache, series_list)
    log(f"Series found for {account_name}: {len(series_list)}")

    expected: set[Path] = set()
    stats = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}

    for index, series in enumerate(series_list, start=1):
        provider = get_series_provider_info(series.get("id"), token)
        title = normalize_title(str(series.get("name") or series.get("title") or "Untitled"))
        show_dir = root / category_for(series) / fs_safe(title)

        episode_count = 0
        for season_num, episode_num, ep_title, ep_uuid in iter_normalized_episodes(provider):
            episode_count += 1
            season_dir = show_dir / f"Season {season_num:02d}"
            base_name = fs_safe(f"S{season_num:02d}E{episode_num:02d} - {ep_title}")
            strm_path = season_dir / f"{base_name}.strm"
            expected.add(strm_path)
            result = write_text_if_changed(strm_path, episode_strm_url(ep_uuid, account_id) + "\n")
            if result in stats:
                stats[result] += 1

        if episode_count == 0:
            stats["skipped"] += 1
            debug(f"No usable episodes found for series: {title}")

        if index == 1 or index == len(series_list) or index % 100 == 0:
            progress(f"Series {account_name}: {index}/{len(series_list)} processed")

    removed = cleanup_stale_strms(root, expected, "series")
    log(f"Series summary for {account_name}: {stats['added']} added, {stats['updated']} updated, {stats['unchanged']} unchanged, {removed} removed, {stats['skipped']} skipped")


def cleanup_stale_strms(root: Path, expected: set[Path], label: str) -> int:
    if not CONFIG.delete_old or not root.exists():
        return 0

    removed = 0
    for existing in root.glob("**/*.strm"):
        if existing not in expected:
            if CONFIG.dry_run:
                log(f"[dry-run] Would delete stale {label} STRM: {existing}")
            else:
                existing.unlink()
            removed += 1
    empty_dirs = remove_empty_dirs(root)
    debug(f"Removed {empty_dirs} empty directories under {root}")
    return removed


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> int:
    log("=== VOD2strm incremental STRM sync started ===")
    log(f"Dispatcharr URL: {CONFIG.dispatcharr_url}")
    log(f"STRM-only fast mode: {'yes' if not CONFIG.enable_nfo else 'no, ENABLE_NFO=true'}")

    if CONFIG.enable_nfo:
        log("ENABLE_NFO=true is set, but this rewrite currently focuses on STRM generation only. No TMDB queries are performed.")
    if CONFIG.dry_run:
        log("DRY_RUN=true: no files or caches will be written or deleted.")

    if CONFIG.clear_cache and CONFIG.cache_dir.exists():
        if CONFIG.dry_run:
            log(f"[dry-run] Would clear cache directory only: {CONFIG.cache_dir}")
        else:
            shutil.rmtree(CONFIG.cache_dir, ignore_errors=True)
            log(f"Cleared cache directory only: {CONFIG.cache_dir}")

    token = api_login()
    global _CURRENT_TOKEN
    _CURRENT_TOKEN = token

    accounts = [account for account in get_accounts(token) if account_matches(account.get("name") or "")]
    if not accounts:
        log(f"No Dispatcharr M3U/XC accounts matched: {CONFIG.xc_names}")
        return 1

    log(f"Matched {len(accounts)} account(s): {', '.join(str(a.get('name') or a.get('id')) for a in accounts)}")
    for account in accounts:
        export_movies_for_account(token, account)
        export_series_for_account(token, account)

    log("=== VOD2strm incremental STRM sync finished ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
