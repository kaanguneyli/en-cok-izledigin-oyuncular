#!/usr/bin/env python3
"""Rank actors from a public Letterboxd profile's Films and Diary pages."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import queue
import random
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPSHandler, HTTPCookieProcessor, Request, build_opener


BASE_URL = "https://letterboxd.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
FILM_HREF_RE = re.compile(r"^/film/([^/]+)/$")
ACTOR_HREF_RE = re.compile(r"^/actor/([^/]+)/$")
PROFILE_URL_RE = re.compile(r"^https?://(?:www\.)?letterboxd\.com/([^/?#]+)/?", re.I)
UI_RESULT_PREFIX = "LETTERBOXD_UI_RESULT="


class ScrapeError(RuntimeError):
    """Raised when Letterboxd cannot be read reliably."""


@dataclass(frozen=True)
class Film:
    slug: str
    title: str

    @property
    def url(self) -> str:
        return f"{BASE_URL}/film/{self.slug}/"


@dataclass(frozen=True)
class Actor:
    slug: str
    name: str

    @property
    def url(self) -> str:
        return f"{BASE_URL}/actor/{self.slug}/"


@dataclass
class ModeCollection:
    films: dict[str, Film] = field(default_factory=dict)
    weights: Counter[str] = field(default_factory=Counter)
    reported_total: int | None = None
    next_url: str | None = None
    pages_read: int = 0
    seen_pages: set[str] = field(default_factory=set, repr=False)


@dataclass
class ActorTotal:
    actor: Actor
    appearances: int = 0
    films: dict[str, tuple[str, int]] = field(default_factory=dict)


class ListingParser(HTMLParser):
    """Extract film entries and the next-page URL from Films or Diary HTML."""

    def __init__(self, mode: str) -> None:
        super().__init__(convert_charrefs=True)
        self.mode = mode
        self.entries: list[Film] = []
        self.next_href: str | None = None
        self.reported_total: int | None = None
        self._diary_row_depth = 0
        self._diary_row_film: Film | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())

        count_match = re.fullmatch(
            r"\s*([\d.,\s\u00a0]+)\s+films?\s*",
            attributes.get("title", ""),
            re.I,
        )
        if count_match:
            digits = re.sub(r"\D", "", count_match.group(1))
            if digits:
                self.reported_total = int(digits)

        if tag == "a" and "next" in classes and attributes.get("href"):
            self.next_href = attributes["href"]

        if self.mode == "films":
            self._handle_films_tag(tag, attributes, classes)
        else:
            self._handle_diary_tag(tag, attributes, classes)

    def handle_endtag(self, tag: str) -> None:
        if self.mode != "diary" or not self._diary_row_depth:
            return
        if tag == "tr":
            if self._diary_row_film is not None:
                self.entries.append(self._diary_row_film)
            self._diary_row_depth = 0
            self._diary_row_film = None

    def _handle_films_tag(
        self, tag: str, attributes: dict[str, str], classes: set[str]
    ) -> None:
        href = ""
        if tag == "a" and "frame" in classes:
            href = attributes.get("href", "")
        elif attributes.get("data-component-class") == "LazyPoster":
            href = attributes.get("data-item-link", "")

        match = FILM_HREF_RE.fullmatch(href)
        if not match:
            return
        title = (
            attributes.get("data-original-title", "").strip()
            or attributes.get("data-item-full-display-name", "").strip()
            or attributes.get("data-item-name", "").strip()
            or match.group(1)
        )
        self.entries.append(Film(match.group(1), title))

    def _handle_diary_tag(
        self, tag: str, attributes: dict[str, str], classes: set[str]
    ) -> None:
        if tag == "tr" and "diary-entry-row" in classes:
            self._diary_row_depth = 1
            self._diary_row_film = None
            return
        if not self._diary_row_depth or self._diary_row_film is not None:
            return

        match = FILM_HREF_RE.fullmatch(attributes.get("data-item-link", ""))
        if not match:
            return
        title = (
            attributes.get("data-item-full-display-name", "").strip()
            or attributes.get("data-item-name", "").strip()
            or match.group(1)
        )
        self._diary_row_film = Film(match.group(1), title)


class CastParser(HTMLParser):
    """Extract actor links, preferring Letterboxd's explicit cast panel."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._panel_depth = 0
        self._panel_seen = False
        self._current: tuple[str, bool, list[str]] | None = None
        self._panel_actors: list[Actor] = []
        self._all_actors: list[Actor] = []

    @property
    def actors(self) -> list[Actor]:
        source = self._panel_actors if self._panel_seen else self._all_actors
        deduplicated: dict[str, Actor] = {}
        for actor in source:
            deduplicated.setdefault(actor.slug, actor)
        return list(deduplicated.values())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}

        if tag == "div":
            if self._panel_depth:
                self._panel_depth += 1
            elif attributes.get("id") == "tab-panel-cast":
                self._panel_depth = 1
                self._panel_seen = True

        if tag != "a":
            return
        match = ACTOR_HREF_RE.fullmatch(attributes.get("href", ""))
        if match:
            self._current = (match.group(1), self._panel_depth > 0, [])

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            slug, in_panel, parts = self._current
            name = " ".join("".join(parts).split())
            if name:
                actor = Actor(slug, name)
                self._all_actors.append(actor)
                if in_panel:
                    self._panel_actors.append(actor)
            self._current = None

        if tag == "div" and self._panel_depth:
            self._panel_depth -= 1


class RateLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self._next_request = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + self.interval


class HttpClient:
    def __init__(self, timeout: float, retries: int, delay: float) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.rate_limiter = RateLimiter(delay)
        self.ssl_context = build_ssl_context()
        self.cookie_jar = CookieJar()
        self.opener = build_opener(
            HTTPCookieProcessor(self.cookie_jar),
            HTTPSHandler(context=self.ssl_context),
        )

    def get(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.rate_limiter.wait()
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "gzip",
                    "Accept-Language": "en-US,en;q=0.8",
                },
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        body = gzip.decompress(body)
                    charset = response.headers.get_content_charset() or "utf-8"
                    html = body.decode(charset, errors="replace")
                    self._check_for_challenge(html, url)
                    return html
            except HTTPError as error:
                if error.code == 404:
                    raise ScrapeError(f"Sayfa bulunamadı: {url}") from error
                last_error = error
                if error.code not in {408, 425, 429, 500, 502, 503, 504}:
                    break
                retry_after = error.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
                wait = 2**attempt

            if attempt < self.retries:
                time.sleep(wait + random.uniform(0.0, 0.3))

        raise ScrapeError(f"İndirilemedi: {url} ({last_error})")

    @staticmethod
    def _check_for_challenge(html: str, url: str) -> None:
        sample = html[:100_000].lower()
        if "<title>just a moment" in sample or "cf-chl-" in sample:
            raise ScrapeError(f"Letterboxd erişim doğrulaması gösterdi: {url}")


def build_ssl_context() -> ssl.SSLContext:
    """Use Python's CA bundle, certifi, or the standard macOS CA bundle."""
    candidates: list[Path] = []
    env_cafile = os.environ.get("SSL_CERT_FILE")
    if env_cafile:
        candidates.append(Path(env_cafile))

    default_cafile = ssl.get_default_verify_paths().cafile
    if default_cafile:
        candidates.append(Path(default_cafile))

    try:
        import certifi  # type: ignore

        candidates.append(Path(certifi.where()))
    except ImportError:
        pass

    candidates.extend(
        [
            Path("/etc/ssl/cert.pem"),
            Path("/private/etc/ssl/cert.pem"),
        ]
    )
    cafile = next((candidate for candidate in candidates if candidate.is_file()), None)
    return ssl.create_default_context(cafile=str(cafile) if cafile else None)


class CastCache:
    VERSION = 1

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self.data: dict[str, list[dict[str, str]]] = {}
        if enabled:
            self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") == self.VERSION:
                self.data = payload.get("films", {})
        except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
            self.data = {}

    def get(self, slug: str) -> list[Actor] | None:
        if not self.enabled or slug not in self.data:
            return None
        try:
            return [Actor(item["slug"], item["name"]) for item in self.data[slug]]
        except (KeyError, TypeError):
            return None

    def put(self, slug: str, actors: Iterable[Actor]) -> None:
        if self.enabled:
            self.data[slug] = [
                {"slug": actor.slug, "name": actor.name} for actor in actors
            ]

    def save(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        payload = {"version": self.VERSION, "films": self.data}
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def close(self) -> None:
        return


class PostgresCastCache:
    """Persist film casts across ephemeral web-service restarts."""

    def __init__(self, database_url: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.connection = None
        self.lock = threading.Lock()
        if not enabled:
            return
        try:
            import psycopg  # type: ignore

            self.connection = psycopg.connect(database_url)
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS letterboxd_cast_cache (
                        film_slug TEXT PRIMARY KEY,
                        actors_json TEXT NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            self.connection.commit()
        except Exception as error:
            raise ScrapeError(f"PostgreSQL önbelleğine bağlanılamadı: {error}") from error

    def get(self, slug: str) -> list[Actor] | None:
        if not self.enabled or self.connection is None:
            return None
        try:
            with self.lock:
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT actors_json FROM letterboxd_cast_cache WHERE film_slug = %s",
                        (slug,),
                    )
                    row = cursor.fetchone()
            if row is None:
                return None
            payload = json.loads(row[0])
            return [Actor(item["slug"], item["name"]) for item in payload]
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
        except Exception as error:
            raise ScrapeError(f"PostgreSQL önbelleği okunamadı: {error}") from error

    def put(self, slug: str, actors: Iterable[Actor]) -> None:
        if not self.enabled or self.connection is None:
            return
        payload = json.dumps(
            [{"slug": actor.slug, "name": actor.name} for actor in actors],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            with self.lock:
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO letterboxd_cast_cache (film_slug, actors_json, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (film_slug) DO UPDATE
                        SET actors_json = EXCLUDED.actors_json, updated_at = NOW()
                        """,
                        (slug, payload),
                    )
        except Exception as error:
            raise ScrapeError(f"PostgreSQL önbelleği yazılamadı: {error}") from error

    def save(self) -> None:
        if self.connection is not None:
            try:
                with self.lock:
                    self.connection.commit()
            except Exception as error:
                raise ScrapeError(f"PostgreSQL önbelleği kaydedilemedi: {error}") from error

    def close(self) -> None:
        if self.connection is not None:
            with self.lock:
                self.connection.close()
                self.connection = None


def create_cast_cache(path: Path, enabled: bool = True):
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        return PostgresCastCache(database_url, enabled=enabled)
    return CastCache(path, enabled=enabled)


def normalize_username(value: str) -> str:
    value = value.strip()
    match = PROFILE_URL_RE.match(value)
    username = match.group(1) if match else value.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", username):
        raise ValueError("Geçerli bir Letterboxd kullanıcı adı veya profil URL'si girin.")
    return username


def collect_listing(
    client: HttpClient,
    username: str,
    mode: str,
    quiet: bool,
    max_pages: int | None = None,
    collection: ModeCollection | None = None,
) -> ModeCollection:
    first_path = f"/{username}/films/" if mode == "films" else f"/{username}/diary/"
    collection = collection or ModeCollection()
    next_url = (
        collection.next_url
        if collection.pages_read
        else urljoin(BASE_URL, first_path)
    )

    while next_url and next_url not in collection.seen_pages:
        if max_pages is not None and collection.pages_read >= max_pages:
            break
        collection.seen_pages.add(next_url)
        collection.pages_read += 1
        if not quiet:
            print(
                f"[{mode}] sayfa {collection.pages_read} okunuyor...",
                file=sys.stderr,
            )
        parser = ListingParser(mode)
        parser.feed(client.get(next_url))
        if parser.reported_total is not None:
            collection.reported_total = parser.reported_total

        for film in parser.entries:
            collection.films.setdefault(film.slug, film)
            if mode == "films":
                collection.weights[film.slug] = 1
            else:
                collection.weights[film.slug] += 1

        collection.next_url = (
            urljoin(BASE_URL, parser.next_href) if parser.next_href else None
        )
        next_url = collection.next_url

    return collection


def collect_profile_listings(
    client: HttpClient,
    username: str,
    quiet: bool,
    max_pages: int | None = None,
) -> dict[str, ModeCollection]:
    """Read both listings, skipping redundant Films pages when Diary is complete."""
    modes = ("films", "diary")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(
                collect_listing,
                client,
                username,
                mode,
                quiet,
                1,
            ): mode
            for mode in modes
        }
        collections = {futures[future]: future.result() for future in as_completed(futures)}

    films_collection = collections["films"]
    diary_collection = collections["diary"]
    can_try_diary_only = (
        max_pages is None
        and films_collection.reported_total is not None
        and diary_collection.reported_total is not None
        and diary_collection.reported_total >= films_collection.reported_total
    )

    if can_try_diary_only:
        diary_collection = collect_listing(
            client,
            username,
            "diary",
            quiet,
            collection=diary_collection,
        )
        if len(diary_collection.films) == films_collection.reported_total:
            films_collection = ModeCollection(
                films=dict(diary_collection.films),
                weights=Counter({slug: 1 for slug in diary_collection.films}),
                reported_total=films_collection.reported_total,
                pages_read=1,
            )
            if not quiet:
                print(
                    "[films] Diary tüm izlenen filmleri kapsıyor; "
                    "kalan Films sayfaları atlandı.",
                    file=sys.stderr,
                )
        else:
            films_collection = collect_listing(
                client,
                username,
                "films",
                quiet,
                collection=films_collection,
            )
    else:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(
                    collect_listing,
                    client,
                    username,
                    mode,
                    quiet,
                    max_pages,
                    collections[mode],
                ): mode
                for mode in modes
            }
            collections = {
                futures[future]: future.result() for future in as_completed(futures)
            }
        films_collection = collections["films"]
        diary_collection = collections["diary"]

    collections = {"films": films_collection, "diary": diary_collection}
    for mode in modes:
        collection = collections[mode]
        if not quiet:
            print(
                f"[{mode}] {sum(collection.weights.values())} kayıt, "
                f"{len(collection.films)} benzersiz film.",
                file=sys.stderr,
            )

    return {mode: collections[mode] for mode in modes}


def fetch_cast(client: HttpClient, film: Film) -> list[Actor]:
    parser = CastParser()
    parser.feed(client.get(film.url))
    return parser.actors


def collect_casts(
    client: HttpClient,
    films: dict[str, Film],
    cache: CastCache,
    workers: int,
    refresh: bool,
    quiet: bool,
) -> tuple[dict[str, list[Actor]], list[tuple[Film, str]]]:
    casts: dict[str, list[Actor]] = {}
    pending: list[Film] = []

    for film in films.values():
        cached = None if refresh else cache.get(film.slug)
        if cached is None:
            pending.append(film)
        else:
            casts[film.slug] = cached

    if not quiet:
        print(
            f"Cast: {len(casts)} önbellekte, {len(pending)} indirilecek.",
            file=sys.stderr,
        )

    errors: list[tuple[Film, str]] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_cast, client, film): film for film in pending}
        for future in as_completed(futures):
            film = futures[future]
            try:
                actors = future.result()
                casts[film.slug] = actors
                cache.put(film.slug, actors)
            except Exception as error:  # Keep useful partial results for large profiles.
                errors.append((film, str(error)))
            completed += 1
            if not quiet and (completed == len(pending) or completed % 10 == 0):
                print(
                    f"Cast: {completed}/{len(pending)} tamamlandı, "
                    f"{len(errors)} hata.",
                    file=sys.stderr,
                )
            if completed % 50 == 0:
                cache.save()

    cache.save()
    return casts, errors


def rank_actors(
    collection: ModeCollection, casts: dict[str, list[Actor]]
) -> list[ActorTotal]:
    totals: dict[str, ActorTotal] = {}
    for film_slug, weight in collection.weights.items():
        film = collection.films[film_slug]
        for actor in casts.get(film_slug, []):
            total = totals.setdefault(actor.slug, ActorTotal(actor=actor))
            total.appearances += weight
            total.films[film_slug] = (film.title, weight)

    return sorted(
        totals.values(),
        key=lambda item: (
            -item.appearances,
            -len(item.films),
            item.actor.name.casefold(),
            item.actor.slug,
        ),
    )


def rank_combined_actors(
    films_collection: ModeCollection,
    diary_collection: ModeCollection,
    casts: dict[str, list[Actor]],
) -> list[ActorTotal]:
    """Combine watched films and diary logs into one actor ranking."""
    all_films = dict(films_collection.films)
    all_films.update(diary_collection.films)
    totals: dict[str, ActorTotal] = {}

    for film_slug, film in all_films.items():
        weight = max(
            films_collection.weights.get(film_slug, 0),
            diary_collection.weights.get(film_slug, 0),
        )
        if weight == 0:
            continue
        for actor in casts.get(film_slug, []):
            total = totals.setdefault(actor.slug, ActorTotal(actor=actor))
            total.appearances += weight
            total.films[film_slug] = (film.title, weight)

    return sorted(
        totals.values(),
        key=lambda item: (
            -item.appearances,
            -len(item.films),
            item.actor.name.casefold(),
            item.actor.slug,
        ),
    )


def rankings_payload(
    rankings: list[ActorTotal], top: int | None
) -> list[dict[str, object]]:
    selected = rankings[:top] if top else rankings
    rows: list[dict[str, object]] = []
    for rank, total in enumerate(selected, start=1):
        film_parts = []
        film_entries = []
        sorted_films = sorted(
            total.films.items(),
            key=lambda item: (-item[1][1], item[1][0].casefold()),
        )
        for slug, (title, weight) in sorted_films:
            suffix = f" x{weight}" if weight > 1 else ""
            film_parts.append(f"{title}{suffix}")
            film_entries.append(
                {
                    "slug": slug,
                    "title": spreadsheet_safe(title),
                    "views": weight,
                }
            )
        rows.append(
            {
                "rank": rank,
                "actor": spreadsheet_safe(total.actor.name),
                "appearances": total.appearances,
                "uniqueFilms": len(total.films),
                "rewatches": total.appearances - len(total.films),
                "actorUrl": total.actor.url,
                "films": spreadsheet_safe("; ".join(film_parts)),
                "filmEntries": film_entries,
            }
        )
    return rows


def film_catalog_payload(
    films: dict[str, Film],
    weights: dict[str, int],
    casts: dict[str, list[Actor]],
) -> list[dict[str, object]]:
    """Build the UI film list in viewing-count and cast-size order."""
    return [
        {
            "slug": film_slug,
            "title": spreadsheet_safe(film.title),
            "views": weights[film_slug],
            "actorCount": len(casts.get(film_slug, [])),
        }
        for film_slug, film in sorted(
            films.items(),
            key=lambda item: (
                -weights[item[0]],
                -len(casts.get(item[0], [])),
                item[1].title.casefold(),
                item[0],
            ),
        )
    ]


def ui_export_payload(
    result: dict[str, object], options: dict[str, object]
) -> dict[str, object]:
    """Apply the active UI filters and ordering to an on-demand workbook export."""
    excluded_value = options.get("excludedFilms", [])
    if not isinstance(excluded_value, list):
        excluded_value = []
    excluded = {
        str(slug)
        for slug in excluded_value
        if isinstance(slug, str)
    }
    query = str(options.get("query", "")).strip().casefold()
    sort_key = str(options.get("sortKey", "appearances"))
    if sort_key not in {"appearances", "uniqueFilms", "rewatches"}:
        sort_key = "appearances"
    sort_direction = "asc" if options.get("sortDirection") == "asc" else "desc"

    export_rows: list[dict[str, object]] = []
    source_rows = result.get("rows", [])
    if isinstance(source_rows, list):
        for source_row in source_rows:
            if not isinstance(source_row, dict):
                continue
            source_entries = source_row.get("filmEntries", [])
            entries: list[dict[str, object]] = []
            if isinstance(source_entries, list):
                for entry in source_entries:
                    if not isinstance(entry, dict):
                        continue
                    slug = str(entry.get("slug", ""))
                    if not slug or slug in excluded:
                        continue
                    try:
                        views = max(1, int(entry.get("views", 1)))
                    except (TypeError, ValueError):
                        views = 1
                    entries.append(
                        {
                            "slug": slug,
                            "title": spreadsheet_safe(str(entry.get("title", slug))),
                            "views": views,
                        }
                    )
            if not entries:
                continue

            appearances = sum(int(entry["views"]) for entry in entries)
            film_parts = []
            for entry in entries:
                suffix = f" x{entry['views']}" if int(entry["views"]) > 1 else ""
                film_parts.append(f"{entry['title']}{suffix}")
            films_text = "; ".join(film_parts)
            actor = spreadsheet_safe(str(source_row.get("actor", "")))
            if query and query not in f"{actor} {films_text}".casefold():
                continue
            export_rows.append(
                {
                    "rank": 0,
                    "actor": actor,
                    "appearances": appearances,
                    "uniqueFilms": len(entries),
                    "rewatches": appearances - len(entries),
                    "actorUrl": str(source_row.get("actorUrl", "")),
                    "films": spreadsheet_safe(films_text),
                    "filmEntries": entries,
                }
            )

    export_rows.sort(
        key=lambda row: (
            int(row[sort_key]) if sort_direction == "asc" else -int(row[sort_key]),
            -int(row["appearances"]),
            -int(row["uniqueFilms"]),
            str(row["actor"]).casefold(),
        )
    )
    for rank, row in enumerate(export_rows, start=1):
        row["rank"] = rank

    included_films = []
    source_films = result.get("films", [])
    if isinstance(source_films, list):
        included_films = [
            film
            for film in source_films
            if isinstance(film, dict) and str(film.get("slug", "")) not in excluded
        ]
    total_views = 0
    for film in included_films:
        try:
            total_views += max(1, int(film.get("views", 1)))
        except (TypeError, ValueError):
            total_views += 1

    errors = result.get("errors", [])
    sort_labels = {
        "appearances": "izlenme",
        "uniqueFilms": "benzersiz film",
        "rewatches": "tekrar",
    }
    direction_label = "artan" if sort_direction == "asc" else "azalan"
    return {
        "username": spreadsheet_safe(str(result.get("username", "letterboxd"))),
        "summary": {
            "totalViews": total_views,
            "uniqueFilms": len(included_films),
            "rewatches": total_views - len(included_films),
        },
        "sortDescription": (
            f"Sıralama {sort_labels[sort_key]} sayısına göre {direction_label}."
        ),
        "rows": export_rows,
        "errors": errors if isinstance(errors, list) else [],
    }


def spreadsheet_safe(value: str) -> str:
    """Prevent imported profile data from becoming a spreadsheet formula."""
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


def workbook_runtime() -> tuple[Path, Path]:
    node_override = os.environ.get("LETTERBOXD_ACTORS_NODE")
    modules_override = os.environ.get("LETTERBOXD_ARTIFACT_NODE_MODULES")
    bundled_root = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
    )

    node_candidates = [Path(node_override)] if node_override else []
    node_candidates.append(bundled_root / "bin" / "node")
    system_node = shutil.which("node")
    if system_node:
        node_candidates.append(Path(system_node))

    module_candidates = [Path(modules_override)] if modules_override else []
    module_candidates.append(bundled_root / "node_modules")

    node = next((candidate for candidate in node_candidates if candidate.is_file()), None)
    modules = next(
        (
            candidate
            for candidate in module_candidates
            if (candidate / "@oai" / "artifact-tool").exists()
        ),
        None,
    )
    if node is None or modules is None:
        raise ScrapeError(
            "Excel oluşturma bileşeni bulunamadı. Bu aracı Codex çalışma alanında "
            "çalıştırın veya LETTERBOXD_ACTORS_NODE ve "
            "LETTERBOXD_ARTIFACT_NODE_MODULES değişkenlerini ayarlayın."
        )
    return node, modules


def write_portable_workbook(path: Path, payload: dict[str, object]) -> None:
    """Write the hosted XLSX without relying on the local Codex runtime."""
    try:
        import xlsxwriter  # type: ignore
    except ImportError as error:
        raise ScrapeError(
            "Excel bileşeni kurulu değil; requirements.txt bağımlılıklarını yükleyin."
        ) from error

    colors = {
        "ink": "#172126",
        "muted": "#5C6B73",
        "paper": "#F7F9F8",
        "green": "#00A56A",
        "green_light": "#DDF5EA",
        "line": "#D8E0DC",
        "white": "#FFFFFF",
        "red": "#B42318",
    }
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        workbook = xlsxwriter.Workbook(str(path))
        workbook.set_properties(
            {
                "title": f"{payload.get('username', 'Letterboxd')} oyuncu sıralaması",
                "subject": "Letterboxd izleme geçmişine göre oyuncu analizi",
            }
        )
        title_format = workbook.add_format(
            {
                "bg_color": colors["ink"],
                "font_color": colors["white"],
                "bold": True,
                "font_size": 16,
                "valign": "vcenter",
            }
        )
        summary_format = workbook.add_format(
            {
                "bg_color": colors["green_light"],
                "font_color": colors["ink"],
                "font_size": 10,
                "valign": "vcenter",
            }
        )
        header_format = workbook.add_format(
            {
                "bg_color": colors["green"],
                "font_color": colors["white"],
                "bold": True,
                "border": 1,
                "border_color": colors["green"],
                "valign": "vcenter",
            }
        )
        text_format = workbook.add_format(
            {
                "font_color": colors["ink"],
                "font_size": 10,
                "bottom": 1,
                "bottom_color": colors["line"],
                "valign": "vcenter",
            }
        )
        number_format = workbook.add_format(
            {
                "font_color": colors["ink"],
                "font_size": 10,
                "bottom": 1,
                "bottom_color": colors["line"],
                "num_format": "#,##0",
                "align": "right",
                "valign": "vcenter",
            }
        )
        link_format = workbook.add_format(
            {
                "font_color": "#006B47",
                "underline": True,
                "font_size": 10,
                "bottom": 1,
                "bottom_color": colors["line"],
                "valign": "vcenter",
            }
        )

        sheet = workbook.add_worksheet("Oyuncular")
        sheet.hide_gridlines(2)
        sheet.set_column("A:A", 9)
        sheet.set_column("B:B", 28)
        sheet.set_column("C:C", 14)
        sheet.set_column("D:D", 18)
        sheet.set_column("E:E", 12)
        sheet.set_column("F:F", 48)
        sheet.set_column("G:G", 110)
        sheet.set_row(0, 34)
        sheet.merge_range(
            "A1:G1", f"{payload.get('username', 'letterboxd')} - oyuncu sıralaması", title_format
        )
        summary = payload.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}
        description = str(
            payload.get("sortDescription", "Sıralama toplam izlenmeye göredir.")
        )
        sheet.set_row(1, 25)
        sheet.merge_range(
            "A2:G2",
            f"{summary.get('totalViews', 0)} izlenme; "
            f"{summary.get('uniqueFilms', 0)} benzersiz film; "
            f"{summary.get('rewatches', 0)} tekrar. {description}",
            summary_format,
        )
        headers = [
            "Sıra",
            "Oyuncu",
            "İzlenme",
            "Benzersiz film",
            "Tekrar",
            "Letterboxd",
            "Filmler",
        ]
        sheet.set_row(3, 24)
        sheet.write_row(3, 0, headers, header_format)

        rows = payload.get("rows", [])
        if not isinstance(rows, list):
            rows = []
        for offset, row in enumerate(rows, start=4):
            if not isinstance(row, dict):
                continue
            sheet.set_row(offset, 24)
            sheet.write_number(offset, 0, int(row.get("rank", 0)), number_format)
            sheet.write(offset, 1, str(row.get("actor", "")), text_format)
            sheet.write_number(offset, 2, int(row.get("appearances", 0)), number_format)
            sheet.write_number(offset, 3, int(row.get("uniqueFilms", 0)), number_format)
            sheet.write_number(offset, 4, int(row.get("rewatches", 0)), number_format)
            actor_url = str(row.get("actorUrl", ""))
            if actor_url:
                sheet.write_url(offset, 5, actor_url, link_format, actor_url)
            else:
                sheet.write(offset, 5, "", text_format)
            sheet.write(offset, 6, str(row.get("films", "")), text_format)

        if rows:
            sheet.autofilter(3, 0, len(rows) + 3, 6)
        else:
            empty_format = workbook.add_format(
                {
                    "bg_color": colors["paper"],
                    "font_color": colors["muted"],
                    "italic": True,
                }
            )
            sheet.merge_range("A5:G5", "Kayıt bulunamadı.", empty_format)
        sheet.freeze_panes(4, 0)

        errors = payload.get("errors", [])
        if isinstance(errors, list) and errors:
            error_sheet = workbook.add_worksheet("Hatalar")
            error_sheet.hide_gridlines(2)
            error_header = workbook.add_format(
                {
                    "bg_color": colors["red"],
                    "font_color": colors["white"],
                    "bold": True,
                }
            )
            error_text = workbook.add_format({"text_wrap": True, "valign": "top"})
            error_sheet.write_row(0, 0, ["Film", "Film bağlantısı", "Hata"], error_header)
            for row_index, item in enumerate(errors, start=1):
                if not isinstance(item, dict):
                    continue
                error_sheet.write(row_index, 0, str(item.get("film", "")), error_text)
                film_url = str(item.get("filmUrl", ""))
                if film_url:
                    error_sheet.write_url(row_index, 1, film_url, link_format, film_url)
                error_sheet.write(row_index, 2, str(item.get("error", "")), error_text)
            error_sheet.set_column("A:A", 36)
            error_sheet.set_column("B:B", 48)
            error_sheet.set_column("C:C", 70)
            error_sheet.freeze_panes(1, 0)

        workbook.close()
    except Exception as error:
        raise ScrapeError(f"Excel dosyası oluşturulamadı: {error}") from error


def write_workbook(path: Path, payload: dict[str, object]) -> None:
    if os.environ.get("LETTERBOXD_XLSX_BACKEND", "").lower() == "xlsxwriter":
        write_portable_workbook(path, payload)
        return

    path = path.resolve()
    helper = Path(__file__).with_name("letterboxd_workbook.mjs")
    if not helper.is_file():
        raise ScrapeError(f"Excel oluşturucu bulunamadı: {helper}")

    node, node_modules = workbook_runtime()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="letterboxd-actors-") as directory:
        workdir = Path(directory)
        temporary_helper = workdir / helper.name
        shutil.copy2(str(helper), str(temporary_helper))
        (workdir / "node_modules").symlink_to(node_modules, target_is_directory=True)
        input_path = workdir / "rankings.json"
        input_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        result = subprocess.run(
            [str(node), str(temporary_helper), str(input_path), str(path)],
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=120,
        )
    inspect_sidecar = Path(f"{path}.inspect.ndjson")
    if inspect_sidecar.is_file():
        inspect_sidecar.unlink()
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        message = detail[-1] if detail else "bilinmeyen oluşturma hatası"
        raise ScrapeError(f"Excel dosyası oluşturulamadı: {message}")
    if not path.is_file():
        raise ScrapeError("Excel oluşturucu tamamlandı ancak çıktı dosyası bulunamadı.")


def print_top(rankings: list[ActorTotal], count: int) -> None:
    if count <= 0:
        return
    print(f"\nOYUNCULAR - ilk {min(count, len(rankings))}")
    for index, total in enumerate(rankings[:count], start=1):
        print(
            f"{index:>3}. {total.actor.name} - {total.appearances} "
            f"(benzersiz film: {len(total.films)})"
        )


def build_ui_command(account: str, output_dir: Path, refresh: bool) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        normalize_username(account),
        "--output-dir",
        str(output_dir.expanduser().resolve()),
        "--display",
        "0",
        "--ui-result",
    ]
    if refresh:
        command.append("--refresh")
    return command


def open_path(path: Path, reveal: bool = False) -> None:
    path = path.expanduser().resolve()
    if sys.platform == "darwin":
        command = ["open", "-R", str(path)] if reveal else ["open", str(path)]
    elif os.name == "nt":
        if reveal:
            command = ["explorer", f"/select,{path}"]
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
    else:
        command = ["xdg-open", str(path.parent if reveal else path)]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _launch_native_ui(
    initial_account: str = "", initial_output_dir: Path | None = None
) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as error:
        raise ScrapeError(
            "Arayüz için Python'un tkinter bileşeni gerekli."
        ) from error

    class LetterboxdActorsUI:
        def __init__(self, root: "tk.Tk") -> None:
            self.root = root
            self.process: subprocess.Popen[str] | None = None
            self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
            self.output_path: Path | None = None
            self.previous_output_mtime: int | None = None
            self.cancel_requested = False

            root.title("Letterboxd Oyuncu Analizi")
            root.geometry("860x680")
            root.minsize(720, 570)
            root.configure(background="#F7F9F8")
            root.protocol("WM_DELETE_WINDOW", self.close)

            style = ttk.Style(root)
            style.theme_use("clam")
            style.configure(
                "TEntry",
                fieldbackground="#FFFFFF",
                foreground="#172126",
                bordercolor="#B8C5BE",
                lightcolor="#B8C5BE",
                darkcolor="#B8C5BE",
                padding=9,
            )
            style.map("TEntry", bordercolor=[("focus", "#00A56A")])
            style.configure(
                "Primary.TButton",
                background="#00A56A",
                foreground="#FFFFFF",
                bordercolor="#00A56A",
                padding=(18, 10),
                font=("Helvetica", 12, "bold"),
            )
            style.map(
                "Primary.TButton",
                background=[("active", "#008C5A"), ("disabled", "#9CB7AA")],
            )
            style.configure(
                "Secondary.TButton",
                background="#FFFFFF",
                foreground="#172126",
                bordercolor="#B8C5BE",
                padding=(14, 9),
                font=("Helvetica", 11),
            )
            style.map("Secondary.TButton", background=[("active", "#EAF1ED")])
            style.configure(
                "TCheckbutton",
                background="#F7F9F8",
                foreground="#33434B",
                font=("Helvetica", 11),
            )
            style.configure(
                "Green.Horizontal.TProgressbar",
                background="#00A56A",
                troughcolor="#DDE6E1",
                bordercolor="#DDE6E1",
                lightcolor="#00A56A",
                darkcolor="#00A56A",
            )

            header = tk.Frame(root, background="#172126", height=86)
            header.pack(fill="x")
            header.pack_propagate(False)
            tk.Label(
                header,
                text="Letterboxd Oyuncu Analizi",
                background="#172126",
                foreground="#FFFFFF",
                font=("Helvetica", 21, "bold"),
            ).pack(side="left", padx=28)

            content = tk.Frame(root, background="#F7F9F8", padx=28, pady=24)
            content.pack(fill="both", expand=True)
            content.grid_columnconfigure(0, weight=1)
            content.grid_rowconfigure(7, weight=1)

            self.account_var = tk.StringVar(value=initial_account)
            self.output_dir_var = tk.StringVar(
                value=str(
                    (initial_output_dir or Path(__file__).resolve().parent).expanduser()
                )
            )
            self.refresh_var = tk.BooleanVar(value=False)
            self.status_var = tk.StringVar(value="Hazır")

            tk.Label(
                content,
                text="Letterboxd hesabı",
                background="#F7F9F8",
                foreground="#172126",
                font=("Helvetica", 11, "bold"),
            ).grid(row=0, column=0, sticky="w")
            self.account_entry = ttk.Entry(
                content, textvariable=self.account_var, font=("Helvetica", 13)
            )
            self.account_entry.grid(row=1, column=0, sticky="ew", pady=(6, 17))

            tk.Label(
                content,
                text="Çıktı klasörü",
                background="#F7F9F8",
                foreground="#172126",
                font=("Helvetica", 11, "bold"),
            ).grid(row=2, column=0, sticky="w")
            folder_row = tk.Frame(content, background="#F7F9F8")
            folder_row.grid(row=3, column=0, sticky="ew", pady=(6, 13))
            folder_row.grid_columnconfigure(0, weight=1)
            self.output_entry = ttk.Entry(
                folder_row,
                textvariable=self.output_dir_var,
                font=("Helvetica", 11),
            )
            self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 9))
            self.browse_button = ttk.Button(
                folder_row,
                text="Klasör seç",
                command=self.choose_output_dir,
                style="Secondary.TButton",
            )
            self.browse_button.grid(row=0, column=1)

            self.refresh_check = ttk.Checkbutton(
                content,
                text="Oyuncu önbelleğini yenile",
                variable=self.refresh_var,
            )
            self.refresh_check.grid(row=4, column=0, sticky="w", pady=(0, 17))

            action_row = tk.Frame(content, background="#F7F9F8")
            action_row.grid(row=5, column=0, sticky="ew", pady=(0, 14))
            self.run_button = ttk.Button(
                action_row,
                text="Excel oluştur",
                command=self.start,
                style="Primary.TButton",
            )
            self.run_button.pack(side="left")
            self.cancel_button = ttk.Button(
                action_row,
                text="İptal",
                command=self.cancel,
                style="Secondary.TButton",
                state="disabled",
            )
            self.cancel_button.pack(side="left", padx=(9, 0))
            self.open_button = ttk.Button(
                action_row,
                text="Excel'i aç",
                command=self.open_output,
                style="Secondary.TButton",
                state="disabled",
            )
            self.open_button.pack(side="right")
            self.reveal_button = ttk.Button(
                action_row,
                text="Klasörde göster",
                command=self.reveal_output,
                style="Secondary.TButton",
                state="disabled",
            )
            self.reveal_button.pack(side="right", padx=(0, 9))

            progress_row = tk.Frame(content, background="#F7F9F8")
            progress_row.grid(row=6, column=0, sticky="ew", pady=(0, 10))
            progress_row.grid_columnconfigure(0, weight=1)
            self.progress = ttk.Progressbar(
                progress_row, mode="indeterminate", style="Green.Horizontal.TProgressbar"
            )
            self.progress.grid(row=0, column=0, sticky="ew")
            tk.Label(
                progress_row,
                textvariable=self.status_var,
                background="#F7F9F8",
                foreground="#5C6B73",
                font=("Helvetica", 10),
                width=16,
                anchor="e",
            ).grid(row=0, column=1, padx=(12, 0))

            log_frame = tk.Frame(
                content,
                background="#172126",
                highlightbackground="#D8E0DC",
                highlightthickness=1,
            )
            log_frame.grid(row=7, column=0, sticky="nsew")
            log_frame.grid_rowconfigure(0, weight=1)
            log_frame.grid_columnconfigure(0, weight=1)
            self.log = tk.Text(
                log_frame,
                background="#172126",
                foreground="#DDF5EA",
                insertbackground="#FFFFFF",
                relief="flat",
                borderwidth=0,
                padx=14,
                pady=12,
                wrap="word",
                font=("Menlo", 10),
                state="disabled",
            )
            self.log.grid(row=0, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            self.log.configure(yscrollcommand=scrollbar.set)

            self.account_entry.focus_set()
            self.account_entry.bind("<Return>", lambda _event: self.start())

        def choose_output_dir(self) -> None:
            selected = filedialog.askdirectory(
                title="Çıktı klasörünü seç",
                initialdir=self.output_dir_var.get() or str(Path.home()),
            )
            if selected:
                self.output_dir_var.set(selected)

        def append_log(self, message: str) -> None:
            self.log.configure(state="normal")
            self.log.insert("end", f"{message}\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        def set_running(self, running: bool) -> None:
            input_state = "disabled" if running else "normal"
            self.account_entry.configure(state=input_state)
            self.output_entry.configure(state=input_state)
            self.browse_button.configure(state=input_state)
            self.refresh_check.configure(state=input_state)
            self.run_button.configure(state="disabled" if running else "normal")
            self.cancel_button.configure(state="normal" if running else "disabled")
            if running:
                self.open_button.configure(state="disabled")
                self.reveal_button.configure(state="disabled")
                self.progress.start(11)
            else:
                self.progress.stop()

        def start(self) -> None:
            if self.process is not None:
                return
            try:
                username = normalize_username(self.account_var.get())
                output_dir = Path(self.output_dir_var.get()).expanduser().resolve()
                output_dir.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError) as error:
                messagebox.showerror("Başlatılamadı", str(error), parent=self.root)
                return

            self.output_path = output_dir / f"{username}-actors.xlsx"
            self.previous_output_mtime = (
                self.output_path.stat().st_mtime_ns if self.output_path.exists() else None
            )
            self.cancel_requested = False
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
            self.append_log(f"@{username} için analiz başlatıldı.")
            self.status_var.set("Çalışıyor")
            self.set_running(True)

            command = build_ui_command(username, output_dir, self.refresh_var.get())
            try:
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1,
                )
            except OSError as error:
                self.process = None
                self.set_running(False)
                self.status_var.set("Başlatılamadı")
                messagebox.showerror("Başlatılamadı", str(error), parent=self.root)
                return

            threading.Thread(target=self.read_process, daemon=True).start()
            self.root.after(100, self.poll_messages)

        def read_process(self) -> None:
            process = self.process
            if process is None:
                return
            assert process.stdout is not None
            for line in process.stdout:
                self.messages.put(("line", line.rstrip()))
            self.messages.put(("done", process.wait()))

        def poll_messages(self) -> None:
            finished = False
            while True:
                try:
                    kind, value = self.messages.get_nowait()
                except queue.Empty:
                    break
                if kind == "line":
                    self.append_log(str(value))
                elif kind == "done":
                    self.finish(int(value))
                    finished = True
            if not finished and self.process is not None:
                self.root.after(100, self.poll_messages)

        def finish(self, return_code: int) -> None:
            self.process = None
            self.set_running(False)
            if self.cancel_requested:
                self.status_var.set("İptal edildi")
                self.append_log("İşlem iptal edildi.")
                return

            output_is_new = False
            if self.output_path is not None and self.output_path.exists():
                current_mtime = self.output_path.stat().st_mtime_ns
                output_is_new = (
                    self.previous_output_mtime is None
                    or current_mtime != self.previous_output_mtime
                )

            if return_code in (0, 2) and output_is_new:
                self.status_var.set("Tamamlandı" if return_code == 0 else "Uyarıyla tamamlandı")
                self.open_button.configure(state="normal")
                self.reveal_button.configure(state="normal")
                self.append_log("Excel dosyası hazır.")
                if return_code == 2:
                    messagebox.showwarning(
                        "Uyarıyla tamamlandı",
                        "Bazı filmlerin oyuncu bilgisi alınamadı. Ayrıntılar Excel'deki "
                        "Hatalar sayfasında.",
                        parent=self.root,
                    )
                return

            self.status_var.set("Hata")
            messagebox.showerror(
                "İşlem tamamlanamadı",
                "Excel oluşturulamadı. Ayrıntılar işlem günlüğünde.",
                parent=self.root,
            )

        def cancel(self) -> None:
            if self.process is None or self.process.poll() is not None:
                return
            self.cancel_requested = True
            self.status_var.set("İptal ediliyor")
            self.cancel_button.configure(state="disabled")
            self.process.terminate()

        def open_output(self) -> None:
            if self.output_path is not None and self.output_path.exists():
                open_path(self.output_path)

        def reveal_output(self) -> None:
            if self.output_path is not None and self.output_path.exists():
                open_path(self.output_path, reveal=True)

        def close(self) -> None:
            if self.process is not None and self.process.poll() is None:
                if not messagebox.askyesno(
                    "İşlem sürüyor",
                    "Çalışan işlemi iptal edip pencereyi kapatmak istiyor musunuz?",
                    parent=self.root,
                ):
                    return
                self.process.terminate()
            self.root.destroy()

    try:
        root = tk.Tk()
    except tk.TclError as error:
        raise ScrapeError(f"Arayüz açılamadı: {error}") from error
    LetterboxdActorsUI(root)
    root.mainloop()
    return 0


UI_HTML = r"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Letterboxd Oyuncu Analizi</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #161d21;
      --ink-soft: #273137;
      --muted: #617078;
      --paper: #f3f5f4;
      --white: #ffffff;
      --green: #00a66b;
      --green-dark: #00895a;
      --green-light: #dff5eb;
      --blue: #2878c8;
      --line: #d7deda;
      --line-strong: #bec9c3;
      --amber: #a15c00;
      --amber-light: #fff1d6;
      --red: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-width: 320px;
      background: var(--paper);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 15px;
      line-height: 1.45;
      letter-spacing: 0;
      min-height: 100vh;
    }
    header { background: var(--ink); color: var(--white); border-bottom: 3px solid var(--green); }
    .header-inner {
      width: min(1240px, calc(100% - 48px));
      min-height: 66px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .brand-mark { position: relative; width: 36px; height: 24px; flex: 0 0 36px; }
    .brand-mark i { position: absolute; top: 3px; width: 18px; height: 18px; border-radius: 50%; mix-blend-mode: screen; }
    .brand-mark i:nth-child(1) { left: 0; background: #ff8000; }
    .brand-mark i:nth-child(2) { left: 9px; background: #00c030; }
    .brand-mark i:nth-child(3) { left: 18px; background: #40bcf4; }
    h1 { margin: 0; font-size: 19px; line-height: 1.2; font-weight: 720; letter-spacing: 0; }
    main { width: min(1240px, calc(100% - 48px)); margin: 0 auto; padding: 24px 0 40px; }
    .workspace-bar {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) auto auto;
      align-items: end;
      gap: 18px;
      padding: 18px 20px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--white);
      box-shadow: 0 1px 2px rgba(22, 29, 33, 0.05);
    }
    .fields { min-width: 0; }
    label { display: block; margin: 0 0 7px; color: #354148; font-size: 12px; font-weight: 720; }
    input[type="text"] {
      width: 100%;
      height: 42px;
      padding: 0 13px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: var(--white);
      color: var(--ink);
      font: inherit;
      outline: none;
      transition: border-color 120ms ease, box-shadow 120ms ease;
    }
    input[type="text"]:focus { border-color: var(--green); box-shadow: 0 0 0 3px rgba(0, 165, 106, 0.13); }
    input:disabled { background: #edf1ef; color: #718079; }
    .options { min-height: 42px; display: flex; align-items: center; }
    .check { display: inline-flex; align-items: center; gap: 9px; margin: 0; color: #354148; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }
    .check input { position: absolute; opacity: 0; pointer-events: none; }
    .switch {
      position: relative;
      width: 38px;
      height: 22px;
      flex: 0 0 38px;
      border: 1px solid #aebbb4;
      border-radius: 11px;
      background: #dce3df;
      transition: background 140ms ease, border-color 140ms ease;
    }
    .switch::after {
      content: "";
      position: absolute;
      top: 2px;
      left: 2px;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: var(--white);
      box-shadow: 0 1px 2px rgba(22, 29, 33, 0.25);
      transition: transform 140ms ease;
    }
    .check input:checked + .switch { border-color: var(--green); background: var(--green); }
    .check input:checked + .switch::after { transform: translateX(16px); }
    .check input:focus-visible + .switch { outline: 3px solid rgba(0, 165, 106, 0.22); outline-offset: 2px; }
    .actions { display: flex; align-items: center; gap: 9px; min-height: 42px; }
    button {
      height: 42px;
      padding: 0 16px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: var(--white);
      color: var(--ink);
      font: inherit;
      font-weight: 650;
      letter-spacing: 0;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      white-space: nowrap;
      transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
    }
    button:hover { background: #eaf1ed; }
    button:focus-visible { outline: 3px solid rgba(0, 165, 106, 0.25); outline-offset: 2px; }
    button:disabled { cursor: default; opacity: 0.45; }
    .primary { min-width: 112px; border-color: var(--green); background: var(--green); color: var(--white); }
    .primary:hover { background: var(--green-dark); }
    .cancel { color: var(--red); }
    .quiet-dark { height: 34px; border-color: #526168; background: transparent; color: #dfe6e3; font-size: 12px; font-weight: 620; }
    .quiet-dark:hover { background: #26343a; }
    .status-row { display: grid; grid-template-columns: 1fr auto; gap: 14px; align-items: center; margin: 14px 2px 0; }
    .track { height: 4px; overflow: hidden; border-radius: 2px; background: #dbe2de; }
    .bar { width: 0; height: 100%; background: var(--green); }
    .running .bar { width: 38%; animation: progress 1.4s ease-in-out infinite; }
    @keyframes progress { 0% { transform: translateX(-110%); } 100% { transform: translateX(290%); } }
    .status {
      min-width: 106px;
      padding: 5px 9px;
      border-radius: 4px;
      background: #e8eeeb;
      color: var(--muted);
      font-size: 11px;
      font-weight: 750;
      text-align: center;
    }
    .status.success { background: var(--green-light); color: #006842; }
    .status.warning { background: var(--amber-light); color: var(--amber); }
    .status.error { background: #fee4e2; color: var(--red); }
    .results { margin-top: 24px; }
    .metrics { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--white); box-shadow: 0 1px 2px rgba(22, 29, 33, 0.04); }
    .metric { position: relative; min-height: 80px; padding: 15px 18px; border-right: 1px solid var(--line); }
    .metric::before { content: ""; position: absolute; inset: 0 0 auto; height: 3px; background: var(--ink-soft); }
    .metric:nth-child(1)::before { background: var(--green); }
    .metric:nth-child(2)::before { background: var(--blue); }
    .metric:nth-child(3)::before { background: #d78612; }
    .metric:last-child { border-right: 0; }
    .metric-value { display: block; font-size: 24px; line-height: 1.2; font-weight: 760; font-variant-numeric: tabular-nums; }
    .metric-label { display: block; margin-top: 5px; color: var(--muted); font-size: 11px; font-weight: 680; text-transform: uppercase; }
    .result-toolbar { display: flex; align-items: end; justify-content: space-between; gap: 16px; margin: 24px 0 10px; }
    .result-title { display: flex; align-items: baseline; gap: 10px; }
    .result-toolbar h2 { margin: 0; font-size: 17px; letter-spacing: 0; }
    .result-tools { display: flex; align-items: center; gap: 10px; }
    .result-count { color: var(--muted); font-size: 12px; white-space: nowrap; }
    .search { width: min(340px, 44vw) !important; height: 38px !important; }
    .filter-button {
      position: relative;
      height: 38px;
      flex: 0 0 auto;
      padding: 0 11px;
      gap: 7px;
      color: #415048;
      font-size: 12px;
    }
    .filter-button:hover { border-color: #8fa29a; background: #edf4f0; color: #006b47; }
    .filter-button.active { border-color: var(--green); background: var(--green-light); color: #006b47; }
    .filter-icon { width: 15px; height: 15px; flex: 0 0 15px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
    .filter-label { font-weight: 680; }
    .export-button { height: 38px; padding: 0 12px; gap: 7px; border-color: #83b8a0; color: #006b47; font-size: 12px; }
    .export-button:hover { border-color: var(--green); background: var(--green-light); }
    .export-icon { width: 15px; height: 15px; flex: 0 0 15px; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
    .export-label { font-weight: 680; }
    .filter-count {
      min-width: 19px;
      height: 19px;
      padding: 0 6px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 10px;
      background: var(--ink-soft);
      color: var(--white);
      font-size: 10px;
      font-weight: 750;
      line-height: 1;
      font-variant-numeric: tabular-nums;
    }
    .table-wrap { overflow-x: auto; overflow-y: visible; border: 1px solid var(--line); border-radius: 8px; background: var(--white); box-shadow: 0 1px 2px rgba(22, 29, 33, 0.04); scrollbar-color: #aebbb4 transparent; }
    .pagination { min-height: 42px; display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: 10px; }
    .page-size { margin: 0; display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; font-weight: 620; }
    .page-size select { height: 34px; padding: 0 28px 0 10px; border: 1px solid var(--line-strong); border-radius: 6px; background: var(--white); color: var(--ink); font: inherit; font-weight: 650; outline: none; }
    .page-size select:focus-visible { border-color: var(--green); outline: 3px solid rgba(0, 165, 106, 0.18); }
    .page-navigation { display: flex; align-items: center; gap: 8px; }
    .page-button { width: 34px; height: 34px; padding: 0; color: #354148; font-size: 20px; line-height: 1; }
    .page-status { min-width: 82px; color: #354148; font-size: 12px; font-weight: 680; text-align: center; font-variant-numeric: tabular-nums; }
    table { width: 100%; min-width: 920px; border-collapse: collapse; table-layout: fixed; }
    col.rank { width: 58px; }
    col.actor { width: 190px; }
    col.number { width: 96px; }
    col.films { width: auto; }
    th { position: sticky; top: 0; z-index: 1; height: 42px; padding: 0 12px; background: var(--ink-soft); color: #eaf0ed; border-bottom: 1px solid #39454b; font-size: 11px; font-weight: 720; text-align: left; text-transform: uppercase; }
    th.numeric { text-align: right; }
    th.sortable { padding: 0; }
    .sort-button {
      width: 100%;
      height: 41px;
      padding: 0 10px;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: #eaf0ed;
      font-size: 11px;
      font-weight: 720;
      text-transform: uppercase;
      justify-content: flex-end;
      gap: 5px;
    }
    .sort-button:hover { background: #344047; color: var(--white); }
    .sort-button:focus-visible { outline: 2px solid var(--green); outline-offset: -3px; }
    .sort-arrow { width: 13px; color: #91a099; font-size: 12px; text-align: center; }
    th[aria-sort="ascending"] .sort-arrow,
    th[aria-sort="descending"] .sort-arrow { color: #56d39f; }
    td { padding: 10px 12px; border-bottom: 1px solid #e2e7e4; vertical-align: top; font-size: 13px; line-height: 1.45; }
    tbody tr:last-child td { border-bottom: 0; }
    tbody tr:nth-child(even) { background: #fafcfb; }
    tbody tr:hover { background: #edf7f2; }
    td.numeric { text-align: right; font-variant-numeric: tabular-nums; }
    .actor-link { color: #006b47; font-weight: 700; text-decoration: none; }
    .actor-link:hover { text-decoration: underline; }
    .film-list, .film-list-plain { color: #3e4d54; overflow-wrap: anywhere; }
    .film-list summary { cursor: pointer; color: #3e4d54; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; transition: color 120ms ease; }
    .film-list summary:hover { color: #006b47; }
    .film-list[open] summary { color: #3e4d54; font-weight: 400; }
    .film-list.complete summary { cursor: default; list-style: none; }
    .film-list.complete summary::-webkit-details-marker { display: none; }
    .film-list.complete summary:hover { color: #3e4d54; }
    .film-list-full { margin-top: 2px; padding-left: 16px; color: #3e4d54; }
    .empty { padding: 32px; color: var(--muted); text-align: center; }
    .activity-panel { margin-top: 18px; overflow: hidden; border: 1px solid var(--line); border-left: 3px solid var(--green); border-radius: 6px; background: var(--white); }
    .activity-heading { min-height: 40px; padding: 0 13px; display: flex; align-items: center; justify-content: space-between; gap: 16px; color: #354148; font-size: 12px; font-weight: 700; }
    .activity-meta { color: var(--muted); font-weight: 560; }
    .log-tool { max-height: 150px; overflow: auto; border-top: 1px solid var(--line); background: #fafcfb; }
    dialog {
      width: min(620px, calc(100% - 32px));
      max-height: min(760px, calc(100vh - 48px));
      padding: 0;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: var(--white);
      color: var(--ink);
      box-shadow: 0 18px 50px rgba(22, 29, 33, 0.22);
    }
    dialog::backdrop { background: rgba(12, 18, 21, 0.58); }
    .dialog-header { min-height: 62px; padding: 0 18px; display: flex; align-items: center; justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--line); }
    .dialog-header h2 { margin: 0; font-size: 17px; letter-spacing: 0; }
    .icon-button { width: 34px; height: 34px; min-height: 34px; padding: 0; border: 0; background: transparent; color: var(--muted); font-size: 24px; font-weight: 400; }
    .dialog-body { padding: 16px 18px 0; }
    .dialog-search { height: 40px !important; }
    .filter-actions { min-height: 46px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .filter-actions button { height: 32px; min-height: 32px; padding: 0; border: 0; background: transparent; color: #006b47; font-size: 12px; }
    .filter-actions button:hover { background: transparent; text-decoration: underline; }
    .film-options { height: min(460px, 52vh); overflow-y: auto; border: 1px solid var(--line); border-radius: 6px; scrollbar-color: #aebbb4 transparent; }
    .film-filter-row { min-height: 48px; margin: 0; padding: 7px 11px; display: grid; grid-template-columns: 18px minmax(0, 1fr) auto; align-items: center; gap: 10px; border-bottom: 1px solid #e4e9e6; cursor: pointer; }
    .film-filter-row:last-child { border-bottom: 0; }
    .film-filter-row:hover { background: #f2f8f5; }
    .film-filter-row input { width: 16px; height: 16px; margin: 0; accent-color: var(--green); }
    .film-filter-title { min-width: 0; color: var(--ink); font-size: 13px; font-weight: 620; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .film-filter-meta { color: var(--muted); font-size: 11px; font-weight: 560; white-space: nowrap; }
    .film-filter-empty { padding: 30px 16px; color: var(--muted); text-align: center; }
    .dialog-footer { min-height: 66px; margin-top: 16px; padding: 0 18px; display: flex; align-items: center; justify-content: space-between; gap: 12px; border-top: 1px solid var(--line); background: #fafcfb; }
    .dialog-selection { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
    .dialog-buttons { display: flex; gap: 9px; }
    pre {
      margin: 0;
      padding: 12px 14px;
      color: #53625b;
      font: 12px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .hidden { display: none !important; }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; }
    }
    @media (max-width: 720px) {
      .header-inner, main { width: calc(100% - 28px); }
      .header-inner { min-height: 62px; }
      h1 { font-size: 17px; }
      .brand-mark { transform: scale(0.9); transform-origin: left center; }
      main { padding-top: 16px; }
      .workspace-bar { grid-template-columns: 1fr; align-items: stretch; gap: 13px; padding: 15px; }
      .options { min-height: 30px; }
      .actions { align-items: stretch; }
      .actions button { flex: 1 1 auto; }
      .status-row { grid-template-columns: 1fr; gap: 9px; }
      .status { justify-self: start; }
      .metrics { grid-template-columns: repeat(2, 1fr); }
      .metric:nth-child(2) { border-right: 0; }
      .metric:nth-child(-n + 2) { border-bottom: 1px solid var(--line); }
      .result-toolbar { align-items: stretch; flex-direction: column; }
      .result-tools { align-items: center; flex-wrap: wrap; }
      .search { width: 100% !important; flex: 1 1 100%; order: -1; }
      .pagination { align-items: flex-start; flex-wrap: wrap; }
      .page-navigation { margin-left: auto; }
      .quiet-dark { width: auto; }
      .dialog-footer { align-items: stretch; flex-direction: column; padding-top: 12px; padding-bottom: 12px; }
      .dialog-buttons { width: 100%; }
      .dialog-buttons button { flex: 1; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <h1>Letterboxd Oyuncu Analizi</h1>
      </div>
      <button id="shutdown" class="quiet-dark __SHUTDOWN_CLASS__" type="button">Arayüzü kapat</button>
    </div>
  </header>
  <main>
    <form id="form" class="workspace-bar">
      <div class="fields">
        <div>
          <label for="account">Letterboxd hesabı</label>
          <input id="account" name="account" type="text" autocomplete="off" placeholder="kullanıcı adı veya profil URL'si" required>
        </div>
      </div>
      <div class="options">
        <label class="check"><input id="refresh" type="checkbox"><span class="switch" aria-hidden="true"></span><span>Önbelleği yenile</span></label>
      </div>
      <div class="actions">
        <button id="run" class="primary" type="submit">Analiz et</button>
        <button id="cancel" class="cancel" type="button" disabled>İptal</button>
      </div>
    </form>
    <div id="statusRow" class="status-row">
      <div class="track"><div class="bar"></div></div>
      <div id="status" class="status" aria-live="polite">Hazır</div>
    </div>
    <section id="results" class="results hidden">
      <div class="metrics">
        <div class="metric"><strong id="totalViews" class="metric-value">0</strong><span class="metric-label">İzlenme</span></div>
        <div class="metric"><strong id="uniqueFilms" class="metric-value">0</strong><span class="metric-label">Benzersiz film</span></div>
        <div class="metric"><strong id="rewatches" class="metric-value">0</strong><span class="metric-label">Tekrar</span></div>
        <div class="metric"><strong id="actorCount" class="metric-value">0</strong><span class="metric-label">Oyuncu</span></div>
      </div>
      <div class="result-toolbar">
        <div class="result-title"><h2>Oyuncular</h2><span id="resultCount" class="result-count"></span></div>
        <div class="result-tools">
          <button id="filmFilterButton" class="filter-button" type="button" aria-label="Filmleri filtrele" aria-haspopup="dialog" aria-pressed="false"><svg class="filter-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"></path></svg><span class="filter-label">Filtrele</span><span id="filmFilterCount" class="filter-count hidden"></span></button>
          <input id="search" class="search" type="text" autocomplete="off" placeholder="Oyuncu veya film ara">
          <button id="exportExcel" class="export-button" type="button"><svg class="export-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><path d="m7 10 5 5 5-5"></path><path d="M12 15V3"></path></svg><span id="exportLabel" class="export-label">Excel indir</span></button>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <colgroup>
            <col class="rank"><col class="actor"><col class="number"><col class="number"><col class="number"><col class="films">
          </colgroup>
          <thead><tr><th class="numeric">Sıra</th><th>Oyuncu</th><th class="numeric sortable" aria-sort="descending"><button class="sort-button" type="button" data-sort="appearances">İzlenme <span class="sort-arrow" aria-hidden="true">↓</span></button></th><th class="numeric sortable" aria-sort="none"><button class="sort-button" type="button" data-sort="uniqueFilms">Benzersiz <span class="sort-arrow" aria-hidden="true">↕</span></button></th><th class="numeric sortable" aria-sort="none"><button class="sort-button" type="button" data-sort="rewatches">Tekrar <span class="sort-arrow" aria-hidden="true">↕</span></button></th><th>Filmler</th></tr></thead>
          <tbody id="resultBody"></tbody>
        </table>
      </div>
      <div class="pagination">
        <label class="page-size" for="pageSize">Sayfa başına <select id="pageSize"><option value="20" selected>20</option><option value="50">50</option><option value="100">100</option><option value="200">200</option><option value="all">Hepsi</option></select></label>
        <div class="page-navigation">
          <button id="previousPage" class="page-button" type="button" aria-label="Önceki sayfa" title="Önceki sayfa">‹</button>
          <span id="pageStatus" class="page-status" aria-live="polite">1 / 1</span>
          <button id="nextPage" class="page-button" type="button" aria-label="Sonraki sayfa" title="Sonraki sayfa">›</button>
        </div>
      </div>
    </section>
    <section id="activityPanel" class="activity-panel hidden" aria-live="polite">
      <div class="activity-heading"><span>İşlem durumu</span><span id="activityMeta" class="activity-meta">Hazır</span></div>
      <div class="log-tool"><pre id="log">Hazır.</pre></div>
    </section>
  </main>
  <dialog id="filmFilterDialog">
    <div class="dialog-header">
      <h2>Film filtresi</h2>
      <button id="closeFilmFilter" class="icon-button" type="button" aria-label="Kapat" title="Kapat">×</button>
    </div>
    <div class="dialog-body">
      <input id="filmFilterSearch" class="dialog-search" type="text" autocomplete="off" placeholder="Film ara">
      <div class="filter-actions">
        <button id="includeAllFilms" type="button">Tümünü dahil et</button>
        <button id="excludeAllFilms" type="button">Tümünü hariç tut</button>
      </div>
      <div id="filmOptions" class="film-options"></div>
    </div>
    <div class="dialog-footer">
      <span id="filmSelection" class="dialog-selection"></span>
      <div class="dialog-buttons">
        <button id="cancelFilmFilter" type="button">Vazgeç</button>
        <button id="applyFilmFilter" class="primary" type="button">Uygula</button>
      </div>
    </div>
  </dialog>
  <script>
    const initialAccount = __INITIAL_ACCOUNT__;
    const form = document.querySelector("#form");
    const account = document.querySelector("#account");
    const refresh = document.querySelector("#refresh");
    const run = document.querySelector("#run");
    const cancel = document.querySelector("#cancel");
    const statusRow = document.querySelector("#statusRow");
    const status = document.querySelector("#status");
    const log = document.querySelector("#log");
    const activityPanel = document.querySelector("#activityPanel");
    const activityMeta = document.querySelector("#activityMeta");
    const results = document.querySelector("#results");
    const resultBody = document.querySelector("#resultBody");
    const resultCount = document.querySelector("#resultCount");
    const sortButtons = [...document.querySelectorAll(".sort-button")];
    const search = document.querySelector("#search");
    const pageSize = document.querySelector("#pageSize");
    const previousPage = document.querySelector("#previousPage");
    const nextPage = document.querySelector("#nextPage");
    const pageStatus = document.querySelector("#pageStatus");
    const exportExcel = document.querySelector("#exportExcel");
    const exportLabel = document.querySelector("#exportLabel");
    const filmFilterButton = document.querySelector("#filmFilterButton");
    const filmFilterCount = document.querySelector("#filmFilterCount");
    const filmFilterDialog = document.querySelector("#filmFilterDialog");
    const filmFilterSearch = document.querySelector("#filmFilterSearch");
    const filmOptions = document.querySelector("#filmOptions");
    const filmSelection = document.querySelector("#filmSelection");
    let sourceRows = [];
    let currentRows = [];
    let filmCatalog = [];
    let excludedFilms = new Set();
    let draftExcludedFilms = new Set();
    let currentPage = 1;
    let pageCount = 1;
    let rowsPerPage = 20;
    let polling = false;
    let currentJobId = sessionStorage.getItem("letterboxdJobId") || "";
    let sortKey = "appearances";
    let sortDirection = "desc";
    const numberFormatter = new Intl.NumberFormat("tr-TR");
    const filmMeasureCanvas = document.createElement("canvas");
    const filmMeasureContext = filmMeasureCanvas.getContext("2d");
    account.value = initialAccount;
    if (!initialAccount) account.focus();

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: {"Content-Type": "application/json"},
        ...options,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "İstek tamamlanamadı.");
      return payload;
    }

    function addCell(rowElement, value, className = "") {
      const cell = document.createElement("td");
      cell.textContent = String(value ?? "");
      if (className) cell.className = className;
      rowElement.appendChild(cell);
      return cell;
    }

    function formatNumber(value) {
      return numberFormatter.format(Number(value) || 0);
    }

    function prepareFilmPreview(details, summary, fullList, filmParts) {
      let visibleCount = 1;
      const fit = () => {
        if (details.open || !summary.clientWidth) return;
        const availableWidth = Math.max(80, summary.clientWidth - 26);
        filmMeasureContext.font = getComputedStyle(summary).font;
        let bestFit = 1;
        for (let count = 1; count <= filmParts.length; count += 1) {
          const remainingCount = filmParts.length - count;
          const suffix = remainingCount ? `; +${remainingCount} film` : "";
          const candidate = `${filmParts.slice(0, count).join("; ")}${suffix}`;
          if (count === 1 || filmMeasureContext.measureText(candidate).width <= availableWidth) {
            bestFit = count;
          } else {
            break;
          }
        }
        visibleCount = bestFit;
        const remaining = filmParts.slice(visibleCount);
        details.classList.toggle("complete", remaining.length === 0);
        summary.textContent = remaining.length
          ? `${filmParts.slice(0, visibleCount).join("; ")}; +${remaining.length} film`
          : filmParts.join("; ");
        fullList.textContent = remaining.join("; ");
      };
      details._fitFilmPreview = fit;
      summary.addEventListener("click", (event) => {
        if (details.classList.contains("complete")) event.preventDefault();
      });
      details.addEventListener("toggle", () => {
        if (details.open) {
          summary.textContent = `${filmParts.slice(0, visibleCount).join("; ")};`;
        } else {
          fit();
        }
      });
      return fit;
    }

    function updateSortHeaders() {
      for (const button of sortButtons) {
        const active = button.dataset.sort === sortKey;
        const header = button.closest("th");
        const arrow = button.querySelector(".sort-arrow");
        header.setAttribute(
          "aria-sort",
          active ? (sortDirection === "desc" ? "descending" : "ascending") : "none",
        );
        arrow.textContent = active ? (sortDirection === "desc" ? "↓" : "↑") : "↕";
      }
    }

    function filmEntriesText(entries) {
      return entries.map((entry) => {
        const views = Number(entry.views) || 1;
        return `${entry.title}${views > 1 ? ` x${views}` : ""}`;
      }).join("; ");
    }

    function updateFilmSelection() {
      const excludedCount = draftExcludedFilms.size;
      filmSelection.textContent = `${formatNumber(filmCatalog.length - excludedCount)} dahil · ${formatNumber(excludedCount)} hariç`;
    }

    function renderFilmOptions() {
      const query = filmFilterSearch.value.trim().toLocaleLowerCase("tr-TR");
      const filteredFilms = filmCatalog.filter((film) =>
        !query || String(film.title).toLocaleLowerCase("tr-TR").includes(query)
      );
      filmOptions.replaceChildren();
      const fragment = document.createDocumentFragment();
      for (const film of filteredFilms) {
        const row = document.createElement("label");
        row.className = "film-filter-row";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = !draftExcludedFilms.has(film.slug);
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) draftExcludedFilms.delete(film.slug);
          else draftExcludedFilms.add(film.slug);
          updateFilmSelection();
        });
        const title = document.createElement("span");
        title.className = "film-filter-title";
        title.textContent = film.title;
        const meta = document.createElement("span");
        meta.className = "film-filter-meta";
        meta.textContent = `${formatNumber(film.views)} izlenme · ${formatNumber(film.actorCount)} oyuncu`;
        row.append(checkbox, title, meta);
        fragment.appendChild(row);
      }
      if (!filteredFilms.length) {
        const empty = document.createElement("div");
        empty.className = "film-filter-empty";
        empty.textContent = "Film bulunamadı.";
        fragment.appendChild(empty);
      }
      filmOptions.appendChild(fragment);
      updateFilmSelection();
    }

    function applySelectedFilms() {
      currentRows = sourceRows.map((row) => {
        const entries = (row.filmEntries || []).filter(
          (entry) => !excludedFilms.has(entry.slug),
        );
        if (!entries.length) return null;
        const appearances = entries.reduce(
          (total, entry) => total + (Number(entry.views) || 1),
          0,
        );
        return {
          ...row,
          appearances,
          uniqueFilms: entries.length,
          rewatches: appearances - entries.length,
          films: filmEntriesText(entries),
          filmEntries: entries,
        };
      }).filter(Boolean);

      const includedFilms = filmCatalog.filter(
        (film) => !excludedFilms.has(film.slug),
      );
      const totalViews = includedFilms.reduce(
        (total, film) => total + (Number(film.views) || 1),
        0,
      );
      document.querySelector("#totalViews").textContent = formatNumber(totalViews);
      document.querySelector("#uniqueFilms").textContent = formatNumber(includedFilms.length);
      document.querySelector("#rewatches").textContent = formatNumber(totalViews - includedFilms.length);
      document.querySelector("#actorCount").textContent = formatNumber(currentRows.length);
      const excludedCount = excludedFilms.size;
      filmFilterCount.textContent = formatNumber(excludedCount);
      filmFilterCount.classList.toggle("hidden", excludedCount === 0);
      filmFilterButton.classList.toggle("active", excludedCount > 0);
      filmFilterButton.setAttribute("aria-pressed", String(excludedCount > 0));
      const filterLabel = excludedCount
        ? `Filmleri filtrele, ${formatNumber(excludedCount)} film hariç`
        : "Filmleri filtrele";
      filmFilterButton.setAttribute("aria-label", filterLabel);
      filmFilterButton.title = filterLabel;
      currentPage = 1;
      renderRows();
    }

    function sortedFilteredRows() {
      const query = search.value.trim().toLocaleLowerCase("tr-TR");
      const filtered = currentRows.filter((row) => {
        if (!query) return true;
        return `${row.actor} ${row.films}`.toLocaleLowerCase("tr-TR").includes(query);
      });
      const sorted = [...filtered].sort((left, right) => {
        const primary = Number(left[sortKey] || 0) - Number(right[sortKey] || 0);
        if (primary) return sortDirection === "asc" ? primary : -primary;
        const views = Number(right.appearances || 0) - Number(left.appearances || 0);
        if (views) return views;
        const unique = Number(right.uniqueFilms || 0) - Number(left.uniqueFilms || 0);
        if (unique) return unique;
        return String(left.actor).localeCompare(String(right.actor), "tr-TR");
      });
      return sorted;
    }

    function renderRows() {
      const sorted = sortedFilteredRows();
      const filtered = sorted;
      const effectivePageSize = rowsPerPage === null
        ? Math.max(1, filtered.length)
        : rowsPerPage;
      pageCount = Math.max(1, Math.ceil(filtered.length / effectivePageSize));
      currentPage = Math.min(Math.max(1, currentPage), pageCount);
      const pageStart = (currentPage - 1) * effectivePageSize;
      const visible = sorted.slice(pageStart, pageStart + effectivePageSize);
      resultBody.replaceChildren();
      const fragment = document.createDocumentFragment();
      const filmPreviewFitters = [];
      for (const [index, row] of visible.entries()) {
        const tr = document.createElement("tr");
        addCell(tr, formatNumber(pageStart + index + 1), "numeric");
        const actorCell = document.createElement("td");
        const actorLink = document.createElement("a");
        actorLink.className = "actor-link";
        actorLink.href = row.actorUrl;
        actorLink.target = "_blank";
        actorLink.rel = "noopener noreferrer";
        actorLink.textContent = row.actor;
        actorCell.appendChild(actorLink);
        tr.appendChild(actorCell);
        addCell(tr, formatNumber(row.appearances), "numeric");
        addCell(tr, formatNumber(row.uniqueFilms), "numeric");
        addCell(tr, formatNumber(row.rewatches), "numeric");
        const filmCell = document.createElement("td");
        const films = String(row.films || "");
        const filmParts = films ? films.split("; ") : [];
        if (filmParts.length > 1) {
          const details = document.createElement("details");
          details.className = "film-list";
          const summary = document.createElement("summary");
          summary.textContent = filmParts.join("; ");
          const fullList = document.createElement("div");
          fullList.className = "film-list-full";
          details.append(summary, fullList);
          filmPreviewFitters.push(
            prepareFilmPreview(details, summary, fullList, filmParts),
          );
          filmCell.appendChild(details);
        } else {
          const filmList = document.createElement("div");
          filmList.className = "film-list-plain";
          filmList.textContent = films;
          filmCell.appendChild(filmList);
        }
        tr.appendChild(filmCell);
        fragment.appendChild(tr);
      }
      if (!filtered.length) {
        const tr = document.createElement("tr");
        const cell = addCell(tr, "Sonuç bulunamadı.", "empty");
        cell.colSpan = 6;
        fragment.appendChild(tr);
      }
      resultBody.appendChild(fragment);
      requestAnimationFrame(() => filmPreviewFitters.forEach((fit) => fit()));
      const firstVisible = filtered.length ? pageStart + 1 : 0;
      const lastVisible = pageStart + visible.length;
      resultCount.textContent = `${formatNumber(firstVisible)}-${formatNumber(lastVisible)} gösteriliyor · ${formatNumber(filtered.length)} eşleşme · ${formatNumber(currentRows.length)} toplam`;
      pageStatus.textContent = filtered.length
        ? `${formatNumber(currentPage)} / ${formatNumber(pageCount)}`
        : "0 / 0";
      previousPage.disabled = currentPage <= 1 || !filtered.length;
      nextPage.disabled = currentPage >= pageCount || !filtered.length;
    }

    async function downloadExcel() {
      exportExcel.disabled = true;
      exportLabel.textContent = "Hazırlanıyor";
      try {
        const response = await fetch("/api/export", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            jobId: currentJobId,
            excludedFilms: [...excludedFilms],
            query: search.value,
            sortKey,
            sortDirection,
          }),
        });
        if (!response.ok) {
          let message = "Excel dosyası oluşturulamadı.";
          try {
            const payload = await response.json();
            message = payload.error || message;
          } catch (_error) {}
          throw new Error(message);
        }
        const blob = await response.blob();
        const disposition = response.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : "letterboxd-actors.xlsx";
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        status.textContent = "Excel hazır";
        status.className = "status success";
      } catch (error) {
        status.textContent = "Excel hatası";
        status.className = "status error";
        activityMeta.textContent = "Excel oluşturulamadı";
        log.textContent = error.message;
        activityPanel.classList.remove("hidden");
      } finally {
        exportExcel.disabled = false;
        exportLabel.textContent = "Excel indir";
      }
    }

    function renderResult(result) {
      if (result.username) account.value = result.username;
      sourceRows = Array.isArray(result.rows) ? result.rows : [];
      filmCatalog = Array.isArray(result.films) ? [...result.films].sort((left, right) => {
        const views = Number(right.views || 0) - Number(left.views || 0);
        if (views) return views;
        const actors = Number(right.actorCount || 0) - Number(left.actorCount || 0);
        if (actors) return actors;
        return String(left.title).localeCompare(String(right.title), "tr-TR");
      }) : [];
      excludedFilms = new Set();
      draftExcludedFilms = new Set();
      search.value = "";
      currentPage = 1;
      sortKey = "appearances";
      sortDirection = "desc";
      updateSortHeaders();
      results.classList.remove("hidden");
      applySelectedFilms();
    }

    function applyState(state) {
      const running = state.status === "running";
      const finished = state.status === "success" || state.status === "warning";
      account.disabled = running;
      refresh.disabled = running;
      run.disabled = running;
      cancel.disabled = !running;
      run.textContent = running ? "Analiz ediliyor" : "Analiz et";
      statusRow.classList.toggle("running", running);
      status.textContent = state.label;
      status.className = `status ${state.status}`;
      activityMeta.textContent = state.label;
      const showActivity = running || state.status === "error";
      activityPanel.classList.toggle("hidden", !showActivity);
      log.textContent = state.log || "Hazır.";
      log.scrollTop = log.scrollHeight;
      if (finished && state.result) {
        renderResult(state.result);
        activityPanel.classList.add("hidden");
      }
      return running;
    }

    async function poll() {
      if (polling || !currentJobId) return;
      polling = true;
      try {
        const state = await api(`/api/status?job=${encodeURIComponent(currentJobId)}`);
        const running = applyState(state);
        if (running) setTimeout(poll, 650);
      } catch (error) {
        if (error.message === "İş bulunamadı veya süresi doldu.") {
          currentJobId = "";
          sessionStorage.removeItem("letterboxdJobId");
          status.textContent = "Oturum sona erdi";
          status.className = "status idle";
          activityPanel.classList.add("hidden");
          return;
        }
        status.textContent = "Bağlantı kesildi";
        status.className = "status error";
        activityMeta.textContent = "Bağlantı kesildi";
        activityPanel.classList.remove("hidden");
      } finally {
        polling = false;
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      results.classList.add("hidden");
      currentRows = [];
      try {
        const state = await api("/api/run", {
          method: "POST",
          body: JSON.stringify({account: account.value, refresh: refresh.checked}),
        });
        currentJobId = state.jobId || "";
        if (currentJobId) sessionStorage.setItem("letterboxdJobId", currentJobId);
        applyState(state);
        setTimeout(poll, 200);
      } catch (error) {
        log.textContent = error.message;
        status.textContent = "Hata";
        status.className = "status error";
        activityMeta.textContent = "Hata";
        activityPanel.classList.remove("hidden");
      }
    });
    search.addEventListener("input", () => { currentPage = 1; renderRows(); });
    for (const button of sortButtons) {
      button.addEventListener("click", () => {
        const selectedKey = button.dataset.sort;
        if (sortKey === selectedKey) {
          sortDirection = sortDirection === "desc" ? "asc" : "desc";
        } else {
          sortKey = selectedKey;
          sortDirection = "desc";
        }
        currentPage = 1;
        updateSortHeaders();
        renderRows();
      });
    }
    pageSize.addEventListener("change", () => {
      rowsPerPage = pageSize.value === "all" ? null : (Number(pageSize.value) || 20);
      currentPage = 1;
      renderRows();
    });
    previousPage.addEventListener("click", () => {
      if (currentPage <= 1) return;
      currentPage -= 1;
      renderRows();
      results.scrollIntoView({behavior: "smooth", block: "start"});
    });
    nextPage.addEventListener("click", () => {
      if (currentPage >= pageCount) return;
      currentPage += 1;
      renderRows();
      results.scrollIntoView({behavior: "smooth", block: "start"});
    });
    exportExcel.addEventListener("click", downloadExcel);
    filmFilterButton.addEventListener("click", () => {
      draftExcludedFilms = new Set(excludedFilms);
      filmFilterSearch.value = "";
      renderFilmOptions();
      filmFilterDialog.showModal();
      filmFilterSearch.focus();
    });
    filmFilterSearch.addEventListener("input", renderFilmOptions);
    document.querySelector("#includeAllFilms").addEventListener("click", () => {
      draftExcludedFilms.clear();
      renderFilmOptions();
    });
    document.querySelector("#excludeAllFilms").addEventListener("click", () => {
      draftExcludedFilms = new Set(filmCatalog.map((film) => film.slug));
      renderFilmOptions();
    });
    document.querySelector("#closeFilmFilter").addEventListener("click", () => filmFilterDialog.close());
    document.querySelector("#cancelFilmFilter").addEventListener("click", () => filmFilterDialog.close());
    document.querySelector("#applyFilmFilter").addEventListener("click", () => {
      excludedFilms = new Set(draftExcludedFilms);
      filmFilterDialog.close();
      applySelectedFilms();
    });
    let filmResizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(filmResizeTimer);
      filmResizeTimer = setTimeout(() => {
        document.querySelectorAll(".film-list").forEach((details) => {
          if (details._fitFilmPreview) details._fitFilmPreview();
        });
      }, 120);
    });
    cancel.addEventListener("click", async () => {
      await api("/api/cancel", {
        method: "POST",
        body: JSON.stringify({jobId: currentJobId}),
      });
      poll();
    });
    const shutdown = document.querySelector("#shutdown");
    if (shutdown && !shutdown.classList.contains("hidden")) {
      shutdown.addEventListener("click", async () => {
        await api("/api/shutdown", {method: "POST", body: "{}"});
        document.body.innerHTML = "<main><h1>Arayüz kapatıldı.</h1></main>";
      });
    }
    if (currentJobId) poll();
  </script>
</body>
</html>
"""


class UIJobManager:
    def __init__(
        self,
        cache_dir: Path,
        job_id: str = "",
        on_finish=None,
    ) -> None:
        self.lock = threading.Lock()
        self.cache_dir = cache_dir.expanduser().resolve()
        self.job_id = job_id
        self.on_finish = on_finish
        self.process: subprocess.Popen[str] | None = None
        self.logs: list[str] = []
        self.state = "idle"
        self.label = "Hazır"
        self.result: dict[str, object] | None = None
        self.cancel_requested = False
        self.last_touched = time.monotonic()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            self.last_touched = time.monotonic()
            return {
                "jobId": self.job_id,
                "status": self.state,
                "label": self.label,
                "log": "\n".join(self.logs),
                "result": self.result,
            }

    def is_running(self) -> bool:
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def retention_state(self) -> tuple[bool, float]:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return running, self.last_touched

    def start(self, account: str, refresh: bool) -> dict[str, object]:
        username = normalize_username(account)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise ValueError("Bir analiz zaten çalışıyor.")
            self.logs = [f"@{username} için analiz başlatıldı."]
            self.state = "running"
            self.label = "Çalışıyor"
            self.result = None
            self.cancel_requested = False
            self.last_touched = time.monotonic()
            try:
                self.process = subprocess.Popen(
                    build_ui_command(username, self.cache_dir, refresh),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                    bufsize=1,
                )
            except OSError:
                self.state = "error"
                self.label = "Başlatılamadı"
                raise
            process = self.process

        threading.Thread(target=self._read_process, args=(process,), daemon=True).start()
        return self.snapshot()

    def _read_process(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        assert process.stderr is not None
        log_thread = threading.Thread(
            target=self._read_logs, args=(process.stderr,), daemon=True
        )
        log_thread.start()
        result_output = process.stdout.read()
        return_code = process.wait()
        log_thread.join()

        for line in result_output.splitlines():
            with self.lock:
                stripped = line.strip()
                if stripped.startswith(UI_RESULT_PREFIX):
                    try:
                        result = json.loads(stripped[len(UI_RESULT_PREFIX) :])
                        if isinstance(result, dict):
                            self.result = result
                    except json.JSONDecodeError:
                        self.logs.append("Sonuç verisi okunamadı.")
                else:
                    self.logs.append(stripped)
                self.logs = self.logs[-1000:]

        try:
            with self.lock:
                if self.cancel_requested:
                    self.state = "cancelled"
                    self.label = "İptal edildi"
                    self.logs.append("İşlem iptal edildi.")
                elif return_code in (0, 2) and self.result is not None:
                    self.state = "success" if return_code == 0 else "warning"
                    self.label = (
                        "Tamamlandı" if return_code == 0 else "Uyarıyla tamamlandı"
                    )
                    self.logs.append("Sonuçlar hazır.")
                else:
                    self.state = "error"
                    self.label = "Hata"
                self.process = None
                self.last_touched = time.monotonic()
        finally:
            if self.on_finish is not None:
                self.on_finish(self.job_id)

    def _read_logs(self, stream: Iterable[str]) -> None:
        for line in stream:
            with self.lock:
                self.logs.append(line.rstrip())
                self.logs = self.logs[-1000:]

    def cancel(self) -> dict[str, object]:
        with self.lock:
            process = self.process
            if process is not None and process.poll() is None:
                self.cancel_requested = True
                self.label = "İptal ediliyor"
                process.terminate()
        return self.snapshot()

    def export_workbook(self, options: dict[str, object]) -> tuple[str, bytes]:
        with self.lock:
            result = self.result
        if result is None:
            raise ValueError("Önce analizi tamamlayın.")

        payload = ui_export_payload(result, options)
        username = normalize_username(str(result.get("username", "")))
        filename = f"{username}-actors.xlsx"
        with tempfile.TemporaryDirectory(prefix="letterboxd-ui-export-") as directory:
            path = Path(directory) / filename
            write_workbook(path, payload)
            return filename, path.read_bytes()


class UIJobRegistry:
    def __init__(self, cache_dir: Path, max_jobs: int = 8, ttl: float = 1800.0) -> None:
        self.lock = threading.Lock()
        self.cache_dir = cache_dir.expanduser().resolve()
        self.max_jobs = max_jobs
        self.ttl = ttl
        self.jobs: dict[str, UIJobManager] = {}
        self.active_job_id = ""

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        finished: list[tuple[str, float]] = []
        for job_id, manager in self.jobs.items():
            running, last_touched = manager.retention_state()
            if not running:
                finished.append((job_id, last_touched))
        expired = {
            job_id for job_id, touched in finished if now - touched > self.ttl
        }
        remaining = len(self.jobs) - len(expired)
        if remaining > self.max_jobs:
            for job_id, _touched in sorted(finished, key=lambda item: item[1]):
                if job_id not in expired:
                    expired.add(job_id)
                    remaining -= 1
                    if remaining <= self.max_jobs:
                        break
        for job_id in expired:
            self.jobs.pop(job_id, None)

    def _manager(self, job_id: object) -> UIJobManager:
        token = str(job_id or "")
        with self.lock:
            self._cleanup_locked()
            manager = self.jobs.get(token)
        if manager is None:
            raise ValueError("İş bulunamadı veya süresi doldu.")
        return manager

    def _release(self, job_id: str) -> None:
        with self.lock:
            if self.active_job_id == job_id:
                self.active_job_id = ""

    def start(self, account: str, refresh: bool) -> dict[str, object]:
        username = normalize_username(account)
        with self.lock:
            self._cleanup_locked()
            active = self.jobs.get(self.active_job_id)
            if active is not None and active.is_running():
                raise ValueError(
                    "Sunucu şu anda başka bir analizi tamamlıyor. Kısa süre sonra tekrar deneyin."
                )
            job_id = secrets.token_urlsafe(18)
            manager = UIJobManager(self.cache_dir, job_id, self._release)
            self.jobs[job_id] = manager
            self.active_job_id = job_id
            self._cleanup_locked()
        try:
            return manager.start(username, refresh)
        except Exception:
            with self.lock:
                self.jobs.pop(job_id, None)
                if self.active_job_id == job_id:
                    self.active_job_id = ""
            raise

    def snapshot(self, job_id: object) -> dict[str, object]:
        return self._manager(job_id).snapshot()

    def cancel(self, job_id: object) -> dict[str, object]:
        return self._manager(job_id).cancel()

    def export_workbook(
        self, job_id: object, options: dict[str, object]
    ) -> tuple[str, bytes]:
        return self._manager(job_id).export_workbook(options)

    def cancel_all(self) -> None:
        with self.lock:
            managers = list(self.jobs.values())
        for manager in managers:
            manager.cancel()


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]


def ui_document(initial_account: str, hosted: bool = False) -> bytes:
    def js_value(value: str) -> str:
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")

    return (
        UI_HTML.replace("__INITIAL_ACCOUNT__", js_value(initial_account))
        .replace("__SHUTDOWN_CLASS__", "hidden" if hosted else "")
        .encode("utf-8")
    )


def launch_ui(
    initial_account: str = "",
    initial_output_dir: Path | None = None,
    port: int = 8765,
    open_browser: bool = True,
    host: str = "127.0.0.1",
    hosted: bool = False,
) -> int:
    output_dir = initial_output_dir or Path(__file__).resolve().parent
    document = ui_document(initial_account, hosted=hosted)
    registry = UIJobRegistry(output_dir)

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, payload: dict[str, object], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def send_security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

        def send_excel(self, filename: str, body: bytes) -> None:
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                raise ValueError("İstek çok büyük.")
            if length == 0:
                return {}
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Geçersiz istek.")
            return payload

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            route = parsed.path
            if route == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(document)))
                self.send_header("Cache-Control", "no-store")
                self.send_security_headers()
                self.end_headers()
                self.wfile.write(document)
            elif route == "/healthz":
                self.send_json({"ok": True})
            elif route == "/api/status":
                query = parse_qs(parsed.query)
                try:
                    self.send_json(registry.snapshot(query.get("job", [""])[0]))
                except ValueError as error:
                    self.send_json({"error": str(error)}, 404)
            else:
                self.send_json({"error": "Bulunamadı."}, 404)

        def do_POST(self) -> None:
            route = self.path.split("?", 1)[0]
            try:
                payload = self.read_json()
                if route == "/api/run":
                    state = registry.start(
                        str(payload.get("account", "")),
                        bool(payload.get("refresh", False)),
                    )
                    self.send_json(state, 202)
                elif route == "/api/cancel":
                    self.send_json(registry.cancel(payload.get("jobId")))
                elif route == "/api/export":
                    filename, body = registry.export_workbook(
                        payload.get("jobId"), payload
                    )
                    self.send_excel(filename, body)
                elif route == "/api/shutdown":
                    if hosted:
                        self.send_json({"error": "Bu işlem hosted sürümde kapalı."}, 404)
                    else:
                        registry.cancel_all()
                        self.send_json({"ok": True})
                        threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    self.send_json({"error": "Bulunamadı."}, 404)
            except (
                json.JSONDecodeError,
                OSError,
                ValueError,
                ScrapeError,
                subprocess.SubprocessError,
            ) as error:
                self.send_json({"error": str(error)}, 400)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    try:
        server = LocalHTTPServer((host, port), Handler)
    except OSError:
        if port == 0 or hosted or host not in {"127.0.0.1", "localhost"}:
            raise
        server = LocalHTTPServer((host, 0), Handler)
    actual_port = server.server_address[1]
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{actual_port}/"
    print(f"Arayüz açıldı: {url}", flush=True)
    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url, new=2)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        registry.cancel_all()
        server.server_close()
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Letterboxd profilindeki en çok izlenen oyuncuları tek bir Excel "
            "sayfasında sıralar; tekrar izlemeleri ve film adlarını gösterir."
        )
    )
    parser.add_argument(
        "account",
        nargs="?",
        help="Letterboxd kullanıcı adı veya profil URL'si; verilmezse arayüz açılır",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="tarayıcı arayüzünü aç",
    )
    parser.add_argument("--ui-port", type=int, default=8765, help=argparse.SUPPRESS)
    parser.add_argument("--ui-host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--hosted", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-result", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="Excel ve önbellek klasörü (varsayılan: mevcut klasör)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="sayfada yalnızca ilk N oyuncu; 0 tümü (varsayılan: 0)",
    )
    parser.add_argument(
        "--display",
        type=int,
        default=20,
        help="terminalde gösterilecek oyuncu sayısı (varsayılan: 20)",
    )
    parser.add_argument(
        "--workers", type=int, default=4, help="eşzamanlı indirme sayısı (varsayılan: 4)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="istekler arasındaki en az saniye (varsayılan: 0.25)",
    )
    parser.add_argument(
        "--timeout", type=float, default=20.0, help="istek zaman aşımı (varsayılan: 20)"
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="başarısız istek tekrarları (varsayılan: 3)"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="cast önbelleğini kullanmadan yeniden indir"
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="cast önbelleğini okuma ve yazma"
    )
    parser.add_argument("--quiet", action="store_true", help="ilerleme mesajlarını gizle")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if not args.account:
        raise ValueError("Letterboxd kullanıcı adı veya profil URL'si gerekli.")
    username = normalize_username(args.account)
    if args.top < 0 or args.display < 0 or args.workers < 1 or args.delay < 0:
        raise ValueError("Sayısal seçenekler negatif olamaz; --workers en az 1 olmalı.")

    output_dir: Path = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    client = HttpClient(args.timeout, args.retries, args.delay)
    collections = collect_profile_listings(
        client,
        username,
        args.quiet,
        max_pages=args.max_pages,
    )
    all_films: dict[str, Film] = {}
    all_films.update(collections["films"].films)
    all_films.update(collections["diary"].films)
    if not args.quiet:
        shared_count = len(
            collections["films"].films.keys() & collections["diary"].films.keys()
        )
        print(
            f"Listeler: {shared_count} ortak film tekilleştirildi, "
            f"{len(all_films)} cast sayfası işlenecek.",
            file=sys.stderr,
        )

    cache = create_cast_cache(
        output_dir / f".{username}-letterboxd-cast-cache.json",
        enabled=not args.no_cache,
    )
    try:
        casts, errors = collect_casts(
            client,
            all_films,
            cache,
            args.workers,
            args.refresh,
            args.quiet,
        )
    finally:
        cache.close()

    films_collection = collections["films"]
    diary_collection = collections["diary"]
    rankings = rank_combined_actors(films_collection, diary_collection, casts)
    combined_weights = {
        film_slug: max(
            films_collection.weights.get(film_slug, 0),
            diary_collection.weights.get(film_slug, 0),
        )
        for film_slug in all_films
    }
    total_views = sum(combined_weights.values())
    unique_films = len(combined_weights)
    print_top(rankings, args.display)

    workbook_path = output_dir / f"{username}-actors.xlsx"
    payload: dict[str, object] = {
        "username": username,
        "summary": {
            "totalViews": total_views,
            "uniqueFilms": unique_films,
            "rewatches": total_views - unique_films,
        },
        "films": film_catalog_payload(all_films, combined_weights, casts),
        "rows": rankings_payload(rankings, args.top or None),
        "errors": [
            {
                "film": spreadsheet_safe(film.title),
                "filmUrl": film.url,
                "error": spreadsheet_safe(error),
            }
            for film, error in sorted(errors, key=lambda item: item[0].title.casefold())
        ],
    }
    if args.ui_result:
        print(f"{UI_RESULT_PREFIX}{json.dumps(payload, ensure_ascii=False)}")
        return 2 if errors else 0

    write_workbook(workbook_path, payload)

    print("\nOluşturulan dosya:")
    print(workbook_path)
    if errors:
        print(
            f"\nUyarı: {len(errors)} filmin cast bilgisi alınamadı; "
            "ayrıntılar Hatalar sayfasında.",
            file=sys.stderr,
        )
        return 2
    return 0


def main() -> int:
    parser = build_argument_parser()
    try:
        args = parser.parse_args()
        if args.ui or not args.account:
            hosted = args.hosted or os.environ.get("LETTERBOXD_HOSTED", "").lower() in {
                "1",
                "true",
                "yes",
            }
            host = args.ui_host or ("0.0.0.0" if hosted else "127.0.0.1")
            port = args.ui_port
            if hosted and "--ui-port" not in sys.argv and os.environ.get("PORT"):
                try:
                    port = int(os.environ["PORT"])
                except ValueError as error:
                    raise ValueError("PORT geçerli bir sayı olmalı.") from error
            output_dir = args.output_dir
            if "--output-dir" not in sys.argv:
                output_dir = Path(__file__).resolve().parent
            return launch_ui(
                args.account or "",
                output_dir,
                port=port,
                open_browser=not args.no_browser and not hosted,
                host=host,
                hosted=hosted,
            )
        return run(args)
    except (ScrapeError, ValueError) as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        print("\nİptal edildi.", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
