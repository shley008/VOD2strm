#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Incremental Dispatcharr VOD -> STRM exporter.

Safety rules:
- Normal mode always reads the full Dispatcharr catalog; LIMIT_MOVIES/LIMIT_SERIES are ignored.
- TEST_MODE may limit processing, but stale deletion is automatically disabled.
- Stale cleanup only runs after successful catalog fetches and never when expected files are empty.
- The Movies/Series root directories are never removed.
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
from typing import Any
from urllib.parse import urlencode

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
VARS_FILE = SCRIPT_DIR / "VOD2strm_vars.sh"
API_ERRORS = 0
_CURRENT_TOKEN: str | None = None


def load_vars(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


VARS = load_vars(VARS_FILE)


def setting(name: str, default: str = "", *aliases: str) -> str:
    for key in (name, *aliases):
        val = os.getenv(key)
        if val is not None:
            return val
        if key in VARS:
            return VARS[key]
    return default


def as_bool(name: str, default: bool = False, *aliases: str) -> bool:
    raw = setting(name, "true" if default else "false", *aliases).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def as_int(name: str, default: int | None = None, *aliases: str) -> int | None:
    raw = setting(name, "" if default is None else str(default), *aliases).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def safe_path(name: str, default: Path, *aliases: str) -> Path:
    raw = setting(name, str(default), *aliases).strip()
    if not raw or raw in {".", "./"}:
        return default
    return Path(raw)


@dataclass(frozen=True)
class Config:
    dispatcharr_url: str
    api_key: str
    api_user: str
    api_pass: str
    movies_dir: str
    series_dir: str
    cache_dir: Path
    log_file: Path
    account_filters: list[str]
    export_movies: bool
    export_series: bool
    delete_old: bool
    clear_cache: bool
    dry_run: bool
    test_mode: bool
    test_limit_movies: int
    test_limit_series: int
    page_size: int
    log_level: str
    user_agent: str


def build_config() -> Config:
    url = setting("DISPATCHARR_URL", "http://127.0.0.1:9191", "DISPATCHARR_BASE_URL").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    filters_raw = setting("XC_NAMES", "*", "ACCOUNT_FILTERS")
    filters = [x.strip() for x in filters_raw.split(",") if x.strip()] or ["*"]
    test_mode = as_bool("TEST_MODE", False)
    delete_old = as_bool("DELETE_OLD", True) and not test_mode
    return Config(
        dispatcharr_url=url,
        api_key=setting("DISPATCHARR_API_KEY", "", "API_TOKEN", "DISPATCHARR_API_TOKEN").strip(),
        api_user=setting("DISPATCHARR_API_USER", "admin", "DISPATCHARR_USER").strip(),
        api_pass=setting("DISPATCHARR_API_PASS", "", "DISPATCHARR_PASS").strip(),
        movies_dir=setting("MOVIES_DIR", "/mnt/Share-VOD/{XC_NAME}/Movies", "MOVIES_LIBRARY_PATH"),
        series_dir=setting("SERIES_DIR", "/mnt/Share-VOD/{XC_NAME}/Series", "TV_LIBRARY_PATH", "SERIES_LIBRARY_PATH"),
        cache_dir=safe_path("CACHE_DIR", SCRIPT_DIR / "cache"),
        log_file=safe_path("LOG_FILE", SCRIPT_DIR / "VOD2strm.log"),
        account_filters=filters,
        export_movies=as_bool("EXPORT_MOVIES", True),
        export_series=as_bool("EXPORT_SERIES", True),
        delete_old=delete_old,
        clear_cache=as_bool("CLEAR_CACHE", False),
        dry_run=as_bool("DRY_RUN", False),
        test_mode=test_mode,
        test_limit_movies=as_int("TEST_LIMIT_MOVIES", 20) or 20,
        test_limit_series=as_int("TEST_LIMIT_SERIES", 20) or 20,
        page_size=as_int("PAGE_SIZE", 250) or 250,
        log_level=setting("LOG_LEVEL", "INFO").strip().upper(),
        user_agent=setting("HTTP_USER_AGENT", "VOD2strm/1.2"),
    )


CFG = build_config()


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    path = CFG.log_file if not (CFG.log_file.exists() and CFG.log_file.is_dir()) else SCRIPT_DIR / "VOD2strm.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def progress(msg: str) -> None:
    if CFG.log_level in {"INFO", "DEBUG", "VERBOSE"}:
        log(msg)


def debug(msg: str) -> None:
    if CFG.log_level in {"DEBUG", "VERBOSE"}:
        log(msg)


def headers(token: str | None = None) -> dict[str, str]:
    h = {"Accept": "application/json", "User-Agent": CFG.user_agent}
    tok = token or CFG.api_key
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def api_login() -> str | None:
    if CFG.api_key:
        return CFG.api_key
    if not (CFG.api_user and CFG.api_pass):
        return None
    r = requests.post(
        f"{CFG.dispatcharr_url}/api/accounts/token/",
        json={"username": CFG.api_user, "password": CFG.api_pass},
        headers={"User-Agent": CFG.user_agent},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Dispatcharr login failed ({r.status_code}): {r.text[:250]}")
    token = r.json().get("access")
    if not token:
        raise RuntimeError("Dispatcharr login returned no access token")
    return token


def api_get(path: str, token: str | None = None, params: dict[str, Any] | None = None) -> Any | None:
    global _CURRENT_TOKEN, API_ERRORS
    url = f"{CFG.dispatcharr_url}{path}"
    try:
        r = requests.get(url, headers=headers(_CURRENT_TOKEN or token), params=params or {}, timeout=60)
        if r.status_code == 401 and not CFG.api_key:
            log("WARNING: Dispatcharr returned 401; retrying once after login")
            _CURRENT_TOKEN = api_login()
            r = requests.get(url, headers=headers(_CURRENT_TOKEN), params=params or {}, timeout=60)
    except requests.RequestException as exc:
        API_ERRORS += 1
        log(f"API request failed: {url}: {exc}")
        return None
    if not r.ok:
        API_ERRORS += 1
        log(f"HTTP {r.status_code} from {url}: {r.text[:250]}")
        return None
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text


def get_list(path: str, token: str | None) -> tuple[list[dict[str, Any]], bool]:
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        data = api_get(path, token, {"page": page, "page_size": CFG.page_size})
        if data is None:
            return out, False
        if isinstance(data, dict):
            items = data.get("results") or data.get("data") or data.get("items") or []
            has_next = bool(data.get("next"))
        elif isinstance(data, list):
            items = data
            has_next = len(items) >= CFG.page_size
        else:
            return out, False
        if not items:
            return out, True
        out.extend(x for x in items if isinstance(x, dict))
        if not has_next:
            return out, True
        page += 1


def get_accounts(token: str | None) -> tuple[list[dict[str, Any]], bool]:
    data = api_get("/api/m3u/accounts/", token)
    if isinstance(data, list):
        return data, True
    if isinstance(data, dict):
        return data.get("results") or data.get("data") or data.get("items") or [], True
    return [], False


def get_provider(kind: str, item_id: Any, token: str | None) -> dict[str, Any]:
    if not item_id:
        return {}
    if kind == "movie":
        data = api_get(f"/api/vod/movies/{item_id}/provider-info/", token)
    else:
        data = api_get(f"/api/vod/series/{item_id}/provider-info/", token, {"include_episodes": "true"})
    return data if isinstance(data, dict) else {}


TAG_RE = re.compile(r"(\b(4K|8K|1080p|720p|HDR10|HDR|H.264|H\.265|HEVC)\b|\[[^\]]+\])", re.I)
BAD_FS_RE = re.compile(r'[\\/:*?"<>|]+')
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def clean_title(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = TAG_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" -._")
    return s or "Untitled"


def fs_safe(s: str) -> str:
    return BAD_FS_RE.sub("_", (s or "").strip()).strip(" .") or "_"


def first_key(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in keys and v not in (None, ""):
                return v
        for v in obj.values():
            x = first_key(v, keys)
            if x not in (None, ""):
                return x
    elif isinstance(obj, list):
        for v in obj:
            x = first_key(v, keys)
            if x not in (None, ""):
                return x
    return None


def find_uuid(obj: Any, keys: set[str]) -> str | None:
    x = first_key(obj, keys)
    if x:
        return str(x)
    if isinstance(obj, dict):
        for v in obj.values():
            y = find_uuid(v, keys)
            if y:
                return y
    elif isinstance(obj, list):
        for v in obj:
            y = find_uuid(v, keys)
            if y:
                return y
    elif isinstance(obj, str) and UUID_RE.match(obj):
        return obj
    return None


def item_category(item: dict[str, Any]) -> str:
    return fs_safe(str(item.get("genre") or item.get("category_name") or "Unsorted"))


def write_if_changed(path: Path, text: str) -> str:
    old = None
    if path.exists():
        try:
            old = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            pass
    if old == text:
        return "unchanged"
    if CFG.dry_run:
        log(f"[dry-run] Would {'update' if path.exists() else 'create'}: {path}")
        return "dry-run"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return "updated" if old is not None else "added"


def remove_empty_dirs(root: Path) -> None:
    for d in sorted((p for p in root.glob("**/*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            if not CFG.dry_run:
                d.rmdir()
        except OSError:
            pass


def cleanup(root: Path, expected: set[Path], label: str, catalog_ok: bool) -> int:
    if not CFG.delete_old:
        debug(f"Skipping stale cleanup for {label}: DELETE_OLD=false or TEST_MODE=true")
        return 0
    if CFG.test_mode or not catalog_ok or API_ERRORS or not expected:
        log(f"WARNING: Refusing stale cleanup for {label}; test/catalog/API/expected safety check failed")
        return 0
    removed = 0
    for path in root.glob("**/*.strm") if root.exists() else []:
        if path not in expected:
            if CFG.dry_run:
                log(f"[dry-run] Would delete stale {label} STRM: {path}")
            else:
                path.unlink()
            removed += 1
    remove_empty_dirs(root)
    return removed


def cache_file(account: str, name: str) -> Path:
    return CFG.cache_dir / fs_safe(account) / name


def load_cache(path: Path) -> Any | None:
    if not path.exists() or CFG.clear_cache or CFG.test_mode:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cache(path: Path, data: Any) -> None:
    if CFG.dry_run or CFG.test_mode:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def export_movies(token: str | None, account: dict[str, Any]) -> None:
    if not CFG.export_movies:
        return
    aid = account.get("id")
    aname = account.get("name") or f"Account-{aid}"
    root = Path(CFG.movies_dir.replace("{XC_NAME}", str(aname)))
    root.mkdir(parents=True, exist_ok=True) if not CFG.dry_run else None
    log(f"=== Movies: {aname} -> {root} ===")
    cfile = cache_file(str(aname), "movies.json")
    movies = load_cache(cfile)
    ok = True
    if movies is None:
        movies, ok = get_list(f"/api/vod/movies/?m3u_account={aid}", token)
        if ok:
            save_cache(cfile, movies)
    if CFG.test_mode:
        movies = movies[: CFG.test_limit_movies]
        log(f"TEST_MODE=true: movies limited to {len(movies)} and cleanup disabled")
    stats = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    expected: set[Path] = set()
    for i, movie in enumerate(movies, 1):
        prov = get_provider("movie", movie.get("id"), token)
        merged = {"movie": movie, "provider": prov}
        uuid = find_uuid(merged, {"uuid", "movie_uuid", "vod_uuid", "content_uuid"})
        sid = first_key(merged, {"stream_id", "streamid", "provider_stream_id", "providerstreamid"})
        if not uuid or sid in (None, ""):
            stats["skipped"] += 1
            log(f"WARNING: Skipping movie without UUID/stream_id: {movie.get('name') or movie.get('title') or movie.get('id')}")
            continue
        title = clean_title(str(movie.get("name") or movie.get("title") or "Untitled"))
        year = movie.get("year") or prov.get("year") or ""
        folder = fs_safe(f"{title} ({year})" if year else title)
        path = root / item_category(movie) / folder / f"{folder}.strm"
        expected.add(path)
        url = f"{CFG.dispatcharr_url}/proxy/vod/movie/{uuid}?{urlencode({'stream_id': sid})}\n"
        res = write_if_changed(path, url)
        if res in stats:
            stats[res] += 1
        if i == 1 or i == len(movies) or i % 250 == 0:
            progress(f"Movies {aname}: {i}/{len(movies)} processed")
    removed = cleanup(root, expected, "movie", ok)
    log(f"Movies summary for {aname}: {stats['added']} added, {stats['updated']} updated, {stats['unchanged']} unchanged, {removed} removed, {stats['skipped']} skipped")


def ep_iter(provider: dict[str, Any]):
    eps = provider.get("episodes")
    if isinstance(eps, dict):
        for skey, arr in eps.items():
            snum = int(skey) if str(skey).isdigit() else 1
            for ep in arr if isinstance(arr, list) else []:
                yield norm_ep(ep, snum)
    elif isinstance(eps, list):
        for ep in eps:
            yield norm_ep(ep, None)
    else:
        for season in provider.get("seasons") or provider.get("Seasons") or []:
            if isinstance(season, dict):
                snum = int(season.get("number") or season.get("season_number") or season.get("season") or 1)
                for ep in season.get("episodes") or season.get("Episodes") or []:
                    yield norm_ep(ep, snum)


def norm_ep(ep: Any, fallback_season: int | None):
    if not isinstance(ep, dict):
        return None
    snum = int(ep.get("season_number") or ep.get("season") or ep.get("season_num") or fallback_season or 1)
    enum = int(ep.get("episode_number") or ep.get("episode_num") or ep.get("num") or ep.get("episode") or 0)
    if enum <= 0:
        return None
    uuid = find_uuid(ep, {"uuid", "episode_uuid", "content_uuid"})
    if not uuid:
        return None
    title = clean_title(str(ep.get("title") or ep.get("name") or ep.get("episode_name") or f"Episode {enum}"))
    return snum, enum, title, uuid


def export_series(token: str | None, account: dict[str, Any]) -> None:
    if not CFG.export_series:
        return
    aid = account.get("id")
    aname = account.get("name") or f"Account-{aid}"
    root = Path(CFG.series_dir.replace("{XC_NAME}", str(aname)))
    root.mkdir(parents=True, exist_ok=True) if not CFG.dry_run else None
    log(f"=== Series: {aname} -> {root} ===")
    cfile = cache_file(str(aname), "series.json")
    shows = load_cache(cfile)
    ok = True
    if shows is None:
        shows, ok = get_list(f"/api/vod/series/?m3u_account={aid}", token)
        if ok:
            save_cache(cfile, shows)
    if CFG.test_mode:
        shows = shows[: CFG.test_limit_series]
        log(f"TEST_MODE=true: series limited to {len(shows)} and cleanup disabled")
    stats = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    expected: set[Path] = set()
    for i, show in enumerate(shows, 1):
        prov = get_provider("series", show.get("id"), token)
        show_title = clean_title(str(show.get("name") or show.get("title") or "Untitled"))
        show_dir = root / item_category(show) / fs_safe(show_title)
        count = 0
        for item in ep_iter(prov):
            if not item:
                continue
            snum, enum, etitle, uuid = item
            count += 1
            base = fs_safe(f"S{snum:02d}E{enum:02d} - {etitle}")
            path = show_dir / f"Season {snum:02d}" / f"{base}.strm"
            expected.add(path)
            url = f"{CFG.dispatcharr_url}/proxy/vod/episode/{uuid}?{urlencode({'m3u_account_id': aid})}\n"
            res = write_if_changed(path, url)
            if res in stats:
                stats[res] += 1
        if count == 0:
            stats["skipped"] += 1
        if i == 1 or i == len(shows) or i % 100 == 0:
            progress(f"Series {aname}: {i}/{len(shows)} processed")
    removed = cleanup(root, expected, "series", ok)
    log(f"Series summary for {aname}: {stats['added']} added, {stats['updated']} updated, {stats['unchanged']} unchanged, {removed} removed, {stats['skipped']} skipped")


def main() -> int:
    log("=== VOD2strm incremental STRM sync started ===")
    log(f"Dispatcharr URL: {CFG.dispatcharr_url}")
    if CFG.test_mode:
        log("TEST_MODE=true: processing is limited and DELETE_OLD is forced off")
    if CFG.dry_run:
        log("DRY_RUN=true: no files will be written or deleted")
    if CFG.clear_cache and CFG.cache_dir.exists():
        if CFG.dry_run:
            log(f"[dry-run] Would clear cache directory: {CFG.cache_dir}")
        else:
            shutil.rmtree(CFG.cache_dir, ignore_errors=True)
            log(f"Cleared cache directory: {CFG.cache_dir}")
    global _CURRENT_TOKEN
    _CURRENT_TOKEN = api_login()
    accounts, ok = get_accounts(_CURRENT_TOKEN)
    if not ok:
        log("ERROR: Could not fetch Dispatcharr accounts")
        return 1
    accounts = [a for a in accounts if any(fnmatch.fnmatch(a.get("name") or "", p) for p in CFG.account_filters)]
    if not accounts:
        log(f"No M3U/XC accounts matched: {CFG.account_filters}")
        return 1
    log("Matched accounts: " + ", ".join(str(a.get("name") or a.get("id")) for a in accounts))
    for account in accounts:
        export_movies(_CURRENT_TOKEN, account)
        export_series(_CURRENT_TOKEN, account)
    log("=== VOD2strm incremental STRM sync finished ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
