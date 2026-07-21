# Smart Pinterest Downloader

A small command-line tool that searches Pinterest for a query and downloads the
highest-resolution image for each result. It paginates through results,
remembers what it has already saved (per folder), and can be stopped and resumed
at any time.

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

- **"Empty page" / stops early** — Pinterest may be rate-limiting. Lower
  `--workers`, raise `--delay`, and try again in a few minutes.
- **Nothing downloads** — run with `-v` to see whether images are being skipped
  (e.g. by `--min-res`) or requests are failing.
- **`ModuleNotFoundError`** — activate the venv and `pip install -r requirements.txt`.

## License / disclaimer

Provided as-is for personal and educational use. You are responsible for
complying with Pinterest's Terms of Service and applicable copyright law when
downloading and using images.
