#!/usr/bin/env python3
"""
Smart Pinterest batch image downloader.

Fetches image results for a search query from Pinterest's internal search
resource, paginating with bookmark tokens, and saves the highest-resolution
image available for each pin. State is persisted per folder so a run can be
resumed and duplicates are skipped across sessions.

Usage:
    # Interactive (prompts for everything):
    python pinterest_downloader.py

    # Fully scripted:
    python pinterest_downloader.py -q "myspace aesthetics" -n 100 -o ./downloads

    # Tuning:
    python pinterest_downloader.py -q "brutalist architecture" -n 200 \
        --workers 8 --min-res 400 --verbose

For personal / research use. Respect Pinterest's Terms of Service and
robots.txt, and don't hammer the endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from io import BytesIO
from typing import Iterable, Optional

import requests
from PIL import Image, UnidentifiedImageError

log = logging.getLogger("pinterest")

SEARCH_URL = "https://www.pinterest.com/resource/BaseSearchResource/get/"
END_BOOKMARK = "-end-"  # Pinterest's sentinel meaning "no more results".
MAX_IMAGE_BYTES = 25 * 1024 * 1024  # Skip anything larger than 25 MB.

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*, q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.pinterest.com/",
}


# --------------------------------------------------------------------------- #
# Config & persistent state
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    query: str
    save_dir: str
    target_count: int
    workers: int = 6
    min_res: int = 0          # Minimum width/height in px (0 = no filter).
    page_size: int = 25
    request_delay: float = 1.0  # Polite delay between search pages.
    max_empty_pages: int = 5
    timeout: float = 15.0

    @property
    def tracker_path(self) -> str:
        return os.path.join(self.save_dir, ".download_session.json")


@dataclass
class State:
    """Persistent, resumable state for one download folder."""
    seen_ids: set[str] = field(default_factory=set)
    seen_hashes: set[str] = field(default_factory=set)
    bookmark: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: str) -> "State":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read tracker (%s); starting fresh.", exc)
            return cls()
        state = cls(
            seen_ids=set(data.get("seen_ids", [])),
            seen_hashes=set(data.get("seen_hashes", [])),
            bookmark=data.get("bookmark"),
        )
        log.info("Resumed session: %d pins already collected.", len(state.seen_ids))
        return state

    def save(self, path: str) -> None:
        """Atomically persist state so an interrupted write can't corrupt it."""
        with self.lock:
            payload = {
                "seen_ids": sorted(self.seen_ids),
                "seen_hashes": sorted(self.seen_hashes),
                "bookmark": self.bookmark,
            }
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Downloader
# --------------------------------------------------------------------------- #
class PinterestDownloader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = State.load(cfg.tracker_path)
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._downloaded = 0
        self._skipped_dupes = 0
        self._stop = threading.Event()

    # -- network helpers ---------------------------------------------------- #
    def _warm_up(self) -> None:
        """Load the homepage once so the session picks up cookies Pinterest
        expects on subsequent API calls (reduces empty/blocked responses)."""
        try:
            self.session.get("https://www.pinterest.com/", timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            log.debug("Warm-up request failed (continuing anyway): %s", exc)

    def _get_with_retry(self, url: str, *, params=None, stream=False,
                        max_retries: int = 4) -> Optional[requests.Response]:
        """GET with exponential backoff and explicit rate-limit handling."""
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            if self._stop.is_set():
                return None
            try:
                resp = self.session.get(
                    url, params=params, stream=stream, timeout=self.cfg.timeout
                )
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", backoff))
                    log.warning("Rate limited (429). Waiting %.1fs...", wait)
                    self._interruptible_sleep(wait)
                    backoff = min(backoff * 2, 60)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                log.debug("Attempt %d/%d failed for %s: %s",
                          attempt, max_retries, url, exc)
                if attempt == max_retries:
                    return None
                self._interruptible_sleep(backoff)
                backoff = min(backoff * 2, 60)
        return None

    def _interruptible_sleep(self, seconds: float) -> None:
        # Sleep in small slices so Ctrl+C is responsive.
        self._stop.wait(timeout=seconds)

    # -- search & parse ----------------------------------------------------- #
    def fetch_page(self, bookmark: Optional[str]) -> tuple[list[dict], Optional[str]]:
        options = {
            "isPrefetch": False,
            "query": self.cfg.query,
            "scope": "pins",
            "page_size": self.cfg.page_size,
        }
        if bookmark:
            options["bookmarks"] = [bookmark]

        params = {
            "source_url": f"/search/pins/?q={self.cfg.query}",
            "data": json.dumps({"options": options, "context": {}}),
        }

        resp = self._get_with_retry(SEARCH_URL, params=params)
        if resp is None:
            return [], None
        try:
            resource = resp.json().get("resource_response", {})
        except json.JSONDecodeError:
            log.warning("Search response was not valid JSON.")
            return [], None

        pins = resource.get("data", {}).get("results", []) or []
        next_bookmark = resource.get("bookmark")
        return pins, next_bookmark

    @staticmethod
    def best_image_url(pin: dict) -> Optional[str]:
        """Pick the largest available image variant for a pin."""
        images = pin.get("images", {}) or {}
        for key in ("orig", "1200x", "736x", "600x", "474x"):
            url = images.get(key, {}).get("url")
            if url:
                return url
        return None

    # -- single-pin download ----------------------------------------------- #
    def process_pin(self, pin: dict) -> bool:
        """Download one pin's image. Returns True if a new file was saved."""
        if self._stop.is_set():
            return False

        pin_id = pin.get("id")
        if not pin_id:
            return False
        with self.state.lock:
            if pin_id in self.state.seen_ids:
                self._skipped_dupes += 1
                return False

        url = self.best_image_url(pin)
        if not url:
            return False

        resp = self._get_with_retry(url, stream=True)
        if resp is None:
            return False

        # Read with a size cap to avoid pathological payloads.
        buf = BytesIO()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if self._stop.is_set():
                return False
            buf.write(chunk)
            if buf.tell() > MAX_IMAGE_BYTES:
                log.debug("Pin %s exceeds size cap; skipping.", pin_id)
                return False
        content = buf.getvalue()

        # Validate and inspect the image via PIL.
        try:
            image = Image.open(BytesIO(content))
            image.verify()  # integrity check
            image = Image.open(BytesIO(content))  # re-open after verify()
        except (UnidentifiedImageError, OSError):
            log.debug("Pin %s is not a valid image; skipping.", pin_id)
            return False

        width, height = image.size
        if self.cfg.min_res and (width < self.cfg.min_res or height < self.cfg.min_res):
            log.debug("Pin %s below min-res (%dx%d); skipping.", pin_id, width, height)
            return False

        # Content-hash dedup: the same image often appears under many pin IDs.
        digest = hashlib.sha256(content).hexdigest()
        with self.state.lock:
            if digest in self.state.seen_hashes:
                self.state.seen_ids.add(pin_id)  # remember the ID too
                self._skipped_dupes += 1
                return False

        ext = (image.format or "JPEG").lower().replace("jpeg", "jpg")
        with self.state.lock:
            file_number = len(self.state.seen_ids) + 1
        filename = f"pin_{file_number:04d}_{pin_id}.{ext}"
        filepath = os.path.join(self.cfg.save_dir, filename)

        tmp = f"{filepath}.part"
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, filepath)

        with self.state.lock:
            self.state.seen_ids.add(pin_id)
            self.state.seen_hashes.add(digest)
            self._downloaded += 1
            n = self._downloaded
        log.info("[%d/%d] %s  (%dx%d)",
                 n, self.cfg.target_count, filename, width, height)
        return True

    # -- main loop ---------------------------------------------------------- #
    def run(self) -> None:
        self._install_signal_handler()
        self._warm_up()

        log.info("Query        : %s", self.cfg.query)
        log.info("Destination  : %s", self.cfg.save_dir)
        log.info("Target (new) : %d images", self.cfg.target_count)
        log.info("Resuming     : %s",
                 "yes" if self.state.bookmark else "no (fresh search)")

        bookmark = self.state.bookmark
        empty_pages = 0

        try:
            with ThreadPoolExecutor(max_workers=self.cfg.workers) as pool:
                while self._downloaded < self.cfg.target_count and not self._stop.is_set():
                    pins, next_bookmark = self.fetch_page(bookmark)

                    if not pins:
                        empty_pages += 1
                        log.warning("Empty page (%d/%d).",
                                    empty_pages, self.cfg.max_empty_pages)
                        if empty_pages >= self.cfg.max_empty_pages:
                            log.info("No more results (or blocked). Stopping.")
                            break
                        self._interruptible_sleep(2)
                        continue
                    empty_pages = 0

                    remaining = self.cfg.target_count - self._downloaded
                    futures = [pool.submit(self.process_pin, pin)
                               for pin in pins[: max(remaining * 2, len(pins))]]
                    for _ in as_completed(futures):
                        if self._downloaded >= self.cfg.target_count:
                            break

                    self.state.bookmark = next_bookmark
                    self.state.save(self.cfg.tracker_path)

                    # End-of-results detection.
                    if not next_bookmark or next_bookmark == END_BOOKMARK:
                        log.info("Reached the end of available results.")
                        break
                    if next_bookmark == bookmark:
                        log.info("Pagination token stopped advancing. Stopping.")
                        break
                    bookmark = next_bookmark

                    self._interruptible_sleep(self.cfg.request_delay)
        finally:
            self.state.save(self.cfg.tracker_path)
            self._print_summary()

    # -- misc --------------------------------------------------------------- #
    def _install_signal_handler(self) -> None:
        def handler(signum, frame):
            if not self._stop.is_set():
                log.warning("Interrupt received — finishing in-flight work and saving...")
                self._stop.set()
        signal.signal(signal.SIGINT, handler)

    def _print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("                  DOWNLOAD SUMMARY")
        print("=" * 60)
        print(f"  New images this session : {self._downloaded}")
        print(f"  Duplicates skipped      : {self._skipped_dupes}")
        print(f"  Total in collection     : {len(self.state.seen_ids)}")
        print(f"  Folder                  : {self.cfg.save_dir}")
        print("=" * 60 + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _prompt(msg: str, *, validate=None, default: Optional[str] = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{msg}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if validate is None:
            if value:
                return value
        else:
            ok, cleaned = validate(value)
            if ok:
                return cleaned
        print("  [!] Invalid input, try again.")


def _interactive_config() -> Config:
    print("=" * 60)
    print("      SMART PINTEREST BATCH DOWNLOADER")
    print("=" * 60 + "\n")

    query = _prompt("[?] Search query (e.g. 'myspace aesthetics')")
    default_dir = os.path.join(os.getcwd(), "downloads", query.replace(" ", "_"))
    save_dir = _prompt("[?] Save folder", default=default_dir)

    def _int_gt0(v):
        try:
            n = int(v)
            return (n > 0, str(n))
        except ValueError:
            return (False, v)

    count = int(_prompt("[?] How many NEW images this session", validate=_int_gt0))
    return Config(query=query, save_dir=save_dir, target_count=count)


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smart Pinterest batch image downloader.")
    p.add_argument("-q", "--query", help="Search query.")
    p.add_argument("-n", "--count", type=int, help="Number of NEW images to fetch.")
    p.add_argument("-o", "--output", help="Save folder (default: ./downloads/<query>).")
    p.add_argument("--workers", type=int, default=6, help="Concurrent downloads (default 6).")
    p.add_argument("--min-res", type=int, default=0,
                   help="Skip images smaller than this width/height in px.")
    p.add_argument("--page-size", type=int, default=25, help="Results per API page.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between search pages (default 1.0).")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p.parse_args(list(argv))


def _config_from_args(args: argparse.Namespace) -> Config:
    if not args.query or not args.count:
        # Missing required scripting args → fall back to interactive mode.
        return _interactive_config()
    save_dir = args.output or os.path.join(
        os.getcwd(), "downloads", args.query.replace(" ", "_")
    )
    return Config(
        query=args.query,
        save_dir=save_dir,
        target_count=args.count,
        workers=max(1, args.workers),
        min_res=max(0, args.min_res),
        page_size=max(1, args.page_size),
        request_delay=max(0.0, args.delay),
    )


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    cfg = _config_from_args(args)
    if cfg.target_count <= 0:
        log.error("Target count must be greater than 0.")
        return 2

    os.makedirs(cfg.save_dir, exist_ok=True)
    PinterestDownloader(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
