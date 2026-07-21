# Smart Pinterest Downloader

A small command-line tool that searches Pinterest for a query and downloads the
highest-resolution image for each result. It paginates through results,
remembers what it has already saved (per folder), and can be stopped and resumed
at any time.

- **No stored credentials** — reads your existing browser login at runtime
- **Concurrent downloads** for speed
- **Resumable** — re-run the same query/folder to pick up where you left off
- **Deduplication** by pin ID *and* by image content hash (same picture served
  under different pin IDs is only saved once)
- **Filters**: minimum resolution, max file size
- **Polite**: retries with backoff, honors rate limits, configurable delay
- **Safe**: atomic writes, graceful `Ctrl+C` (saves progress before exiting)

> ⚠️ This uses Pinterest's **undocumented internal search endpoint**. It can
> change or rate-limit without notice. Use it for personal/research purposes,
> keep the worker count and delay modest, and respect Pinterest's Terms of
> Service and `robots.txt`.

---

## Requirements

- Python 3.9+
- `requests` and `Pillow` (see `requirements.txt`)

## Install

```bash
# from inside the project folder
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Authentication

Pinterest's search endpoint generally requires a **logged-in session**, so the
script authenticates automatically at startup:

1. It reads the Pinterest session cookie from the browser you're **already
   logged into** (Chrome, Firefox, Safari, Edge, Brave, Chromium, or Opera).
2. If it can't find a logged-in session, it **opens Pinterest's login page in
   your default browser**. Log in there, come back to the terminal, and press
   `ENTER` — it then reads the fresh cookie and continues.

**No credentials or tokens are ever written to disk or stored in the source.**
Cookies are read from your browser's cookie store and kept only in memory for
the duration of the run.

By default it tries all supported browsers. To point at a specific one:

```bash
python pinterest_downloader.py -q "ceramics" -n 50 --browser firefox
```

> **macOS note:** reading Chrome/Safari cookies may trigger a Keychain prompt
> the first time (`browser_cookie3` needs it to decrypt the cookie store).
> Allow it. If Chrome cookies can't be read, `--browser firefox` is the easiest
> fallback.

## Usage

### Interactive (easiest)

Run it with no arguments and it will prompt you for the query, folder, and count:

```bash
python pinterest_downloader.py
```

```
[?] Search query (e.g. 'myspace aesthetics'): brutalist architecture
[?] Save folder [/…/downloads/brutalist_architecture]:
[?] How many NEW images this session: 100
```

### Scripted (flags)

```bash
python pinterest_downloader.py -q "brutalist architecture" -n 200 -o ./downloads/brutalist
```

### Common options

| Flag             | Default              | Description                                        |
|------------------|----------------------|----------------------------------------------------|
| `-q`, `--query`  | *(prompted)*         | Search query.                                      |
| `-n`, `--count`  | *(prompted)*         | Number of **new** images to fetch this run.        |
| `-o`, `--output` | `./downloads/<query>`| Destination folder.                                |
| `--workers`      | `6`                  | Concurrent downloads.                              |
| `--min-res`      | `0` (off)            | Skip images smaller than this many px (w or h).    |
| `--page-size`    | `25`                 | Results requested per API page.                    |
| `--delay`        | `1.0`                | Seconds to wait between search pages.              |
| `--browser`      | `auto`               | Browser to read the Pinterest login cookie from.   |
| `-v`, `--verbose`| off                  | Debug logging (shows skips/retries).               |

> If you pass `-q` **and** `-n`, it runs fully non-interactively. Omit either one
> and it drops back to the interactive prompts.

### Examples

```bash
# High-res only, more parallelism, verbose
python pinterest_downloader.py -q "film photography" -n 300 --min-res 800 --workers 10 -v

# Gentle on the endpoint
python pinterest_downloader.py -q "ceramics" -n 50 --workers 3 --delay 2.5
```

## How resuming works

Each destination folder gets a hidden `.download_session.json` file that stores:

- `seen_ids` — pins already downloaded
- `seen_hashes` — content hashes already saved (cross-pin dedup)
- `bookmark` — the pagination token to continue from

Re-running with the **same query and folder** continues from the last bookmark
and skips anything already collected. Delete that file to start the query over
from scratch.

Files are named `pin_<number>_<pinId>.<ext>`, e.g. `pin_0042_1234567890.jpg`.

## Stopping

Press `Ctrl+C` once. In-flight downloads finish, state is saved, and a summary is
printed — so it's always safe to resume later.

## Troubleshooting

- **"No logged-in session found" / empty results** — you're not logged into
  Pinterest in the browser it read from. Log in when the browser opens (then
  press ENTER), or pass `--browser <name>` to point at the right browser.
- **"Empty page" / stops early** — Pinterest may be rate-limiting. Lower
  `--workers`, raise `--delay`, and try again in a few minutes.
- **Nothing downloads** — run with `-v` to see whether images are being skipped
  (e.g. by `--min-res`) or requests are failing.
- **`ModuleNotFoundError`** — activate the venv and `pip install -r requirements.txt`.

## License / disclaimer

Provided as-is for personal and educational use. You are responsible for
complying with Pinterest's Terms of Service and applicable copyright law when
downloading and using images.
