# smb-transfer

smb-transfer is an interactive, resilient SMB client built on top of Impacket. It provides a command-line REPL for exploring SMB shares, performing large-scale recursive file discovery with powerful filters, exporting results, and reliably transferring filtered file sets with multi-threaded, resumable and multipart downloads.

This README documents every feature, option, and example step-by-step — including low-memory streaming mode, progress/ETA reporting, multipart tuning for large files, and improved timestamp detection.

---
Note: Examples below use the username `admin`, password `password`, and SMB host `192.168.1.1`.

---

Table of contents
- Features
- Requirements
- Installation
- Simple scenario (without streaming)
- Quick start examples (step-by-step) (with streaming)
- Full command reference (interactive)
  - Navigation commands
  - lsf (listing & filters) — all options explained
  - select (choose items by index)
  - transfer (download) — all options explained
  - mget (compatibility)
- Advanced: multipart & performance tuning
- Streaming / low-memory mode
- Exporting: CSV / JSON / columns / templates
- mtime detection details
- Testing
- Troubleshooting & tips

---

Features
- Interactive REPL for SMB shares (list shares, change directories, list files)
- Recursive file discovery with filters:
  - extension filters (e.g., pdf, mp3)
  - wildcard/pattern filters (fnmatch style)
  - path restrictions (scan a subtree only)
  - size ranges (supports k/m/g suffixes)
  - date ranges (YYYY-MM-DD)
- Low-memory streaming mode for very large shares (lsf --stream)
- Export lsf results to CSV or JSON, choose columns, or use a custom template
- Resumable multi-threaded downloads with automatic reconnect
- Multipart segmented downloads for large files (parallel ranges) with fallback
- Per-file progress bars and ETA (uses tqdm if installed)
- Dry-run preview mode for transfers
- Unit tests for scanning and filtering logic (pytest)

---

Requirements
- Python 3.8+
- pip
- Impacket (install via pip)
- Optional but recommended: tqdm (progress bars)

Install dependencies (recommended inside a virtualenv):

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install impacket tqdm
# For running tests:
pip install pytest
```
---
## Simple scenario (without streaming)

1. Start the client:

```bash
python3 smb_sync.py 'admin@192.168.1.10' -threads 4
# or
python3 smb_sync.py 'jdoe@192.168.1.10' -debug
```

2. At the `SMB>` prompt:
- List shares:
  ```
  SMB> shares
  ```
- Use a share:
  ```
  SMB> use SharedDocs
  ```

3. Set local download directory (optional):
  ```
  SMB> lcd /tmp/smb-downloads
  ```

4. Discover files:
  ```
  SMB> lsf ext:pdf
  ```

5. Transfer results:
  ```
  SMB> transfer 8
  ```

---

## Full command reference (interactive)

All commands are used at the interactive `SMB>` prompt after authentication.

- shares
  - Description: List available shares on the target.
  - Example:
    ```
    SMB> shares
    ```

- use <SHARE_NAME>
  - Description: Select the specified share (connects and sets current path to root).
  - Example:
    ```
    SMB> use SharedDocs
    ```

- ls
  - Description: List files and directories at the current remote path (non-recursive).
  - Example:
    ```
    SMB (SharedDocs\)> ls
    Directory listing for \:
      <DIR>       Projects
      1024 bytes  README.pdf
    ```

- cd <folder>
  - Description: Change current remote path. Use `cd ..` to go up; `cd` alone to go to share root.
  - Example:
    ```
    SMB> cd Projects
    SMB (SharedDocs\Projects)> 
    ```

- lcd [<local_path>]
  - Description: Show or change the local download directory. If omitted, shows current local path.
  - Examples:
    ```
    SMB> lcd
    Current local directory: /home/user
    SMB> lcd /tmp/smb-downloads
    [+] Local download path set to: /tmp/smb-downloads
    ```

- lsf [FILTER]
  - Description: Recursively scan for files starting at the current remote path, apply optional filters, and print matching files with remote path and size. The last scan/filter result is cached for use with `transfer`.
  - Behavior:
    - If called without arguments, `lsf` enumerates all files recursively from the current path.
    - Filters narrow the results and support extension lists, wildcard patterns, and path restrictions.
  - Filter syntax / examples:
    - No args: all files
      ```
      SMB> lsf
      ```
    - Extension filter (single or comma-separated):
      ```
      SMB> lsf ext:pdf
      SMB> lsf pdf
      SMB> lsf ext:pdf,docx
      SMB> lsf .mp3
      ```
    - Wildcard/pattern (fnmatch style):
      ```
      SMB> lsf pattern:Xy*
      SMB> lsf Xy*
      SMB> lsf *.zip
      ```
    - Path filter (restrict to a remote subpath, recursive):
      ```
      SMB> lsf path:Folder\SubFolder
      SMB> lsf path:Projects\2025
      ```
  - Output: `lsf` prints the number of matches and lists them as:
    ```
    Remote\Path\File.ext  -  <size> bytes
    ```
  - Notes:
    - Filters for extensions are case-insensitive.
    - Patterns use Python's `fnmatch` semantics.

- transfer [<nthreads>]
  - Description: Download files from the last `lsf` filtered result into the local directory (`lcd`). Optionally specify number of threads for the transfer.
  - Examples:
    ```
    SMB> transfer
    SMB> transfer 8
    ```
  - Behavior:
    - Preserves remote path under local directory. Example: `Projects\2025\a.pdf` -> `<local_dir>/Projects/2025/a.pdf`.
    - Resumable: if a partial file exists, the download resumes from the file size on disk.
    - Chunked reads with automatic reconnect on transient failures.

- mget <pattern>
  - Description: Compatibility convenience: recursively download matching items from the current path. Equivalent to calling the downloader with a `pattern`.
  - Examples:
    ```
    SMB> mget *
    SMB> mget *.pdf
    SMB> mget FolderName
    ```

- exit / EOF
  - Description: Exit the interactive shell.

---

## Examples — step-by-step workflows

1) Download all PDFs below a specific remote path

- Start client and authenticate:
  ```bash
  python3 smb_sync.py 'domain/jdoe@fileserver'
  # enter password when prompted
  ```

- Select share and path:
  ```
  SMB> use SharedDocs
  SMB> cd Projects\2025
  SMB> lcd /tmp/smb-downloads
  ```

- Scan for PDFs:
  ```
  SMB> lsf ext:pdf
  [+] Scanning remote files (this may take a while)...

  Found 7 files:
  Projects\2025\report_final.pdf  -  512000 bytes
  Projects\2025\data\summary.pdf   -  128000 bytes
  ...
  ```

- Transfer the filtered results:
  ```
  SMB> transfer 8
  [+] Downloading 7 files using 8 threads...
  [START] Projects\2025\report_final.pdf (512000 bytes)
  [COMPLETE] Projects\2025\report_final.pdf
  ...
  ```

2) Transfer files matching a wildcard across the share

- At prompt:
  ```
  SMB> use SharedDocs
  SMB> lsf Xy*
  SMB> transfer
  ```

This will find files whose basename matches `Xy*` anywhere under the current path and download them.

3) Targeted path-only scan and download

- At prompt:
  ```
  SMB> use SharedDocs
  SMB> lsf path:Archives\2020
  SMB> transfer
  ```

This downloads everything under `\\SharedDocs\Archives\2020\` recursively.


---

Quick start examples (step-by-step) (with streaming)

These examples assume the target SMB server is at 192.168.1.1 and you can authenticate with the account admin / password.

1) Start the client and authenticate (you can omit `:password` to be prompted):

```bash
python3 smb_sync.py 'admin:password@192.168.1.1'
# or prompting for password:
python3 smb_sync.py 'admin@192.168.1.1'
# you will be prompted for the password interactively
```

You will see a prompt:

```
[+] Authenticated! Type 'help' or 'shares' to list available shares.
SMB>
```

2) List shares and connect to a share:

```
SMB> shares
SMB> use SharedDocs
SMB (SharedDocs)>
```

3) Change local download directory (where files will be stored):

```
SMB (SharedDocs)> lcd /tmp/smb-downloads
[+] Local download path set to: /tmp/smb-downloads
```

4) Discover files recursively and filter by PDF extension:

```
SMB (SharedDocs)> lsf ext:pdf
[+] Scanning remote files (this may take a while)...

Found 8 files:
   1. Projects\2025\report_final.pdf  -  512000 bytes  -  2025-04-01 12:00:00
   2. Projects\2024\summary.pdf       -  128000 bytes  -  2024-11-12 09:32:10
   ...
```

5) Preview the transfer (dry-run) of all filtered results:

```
SMB (SharedDocs)> transfer --dry-run
[DRY-RUN] The following files would be downloaded:
  Projects\2025\report_final.pdf  - 512000 bytes
  Projects\2024\summary.pdf       - 128000 bytes
```

6) Download the filtered files using 8 threads (multipart will be used automatically for large files):

```
SMB (SharedDocs)> transfer --threads=8
# Per-file progress bars with ETA will appear (requires tqdm). Large files may be split into parts and downloaded in parallel.
```

7) Stream a huge share and export results to CSV without using lots of memory:

```
SMB (SharedDocs)> lsf --stream --export=results.csv --export-columns=path,size,mtime
# This writes results to results.csv as entries are discovered, using very little memory.
```

8) Use a template to print custom output per file while streaming:

```
SMB> lsf --stream --template="{path} | {size} bytes"
```

---

Full command reference (interactive)

Navigation and basic commands
- shares
  - List available SMB shares on the target.
  - Example: `shares`

- use <SHARE_NAME>
  - Select a share to operate on (sets current path to root).
  - Example: `use SharedDocs`

- ls
  - List files and directories at the current remote path (non-recursive).
  - Example: `ls`

- cd <folder>
  - Change the current remote path (use `cd ..` to go up, `cd` alone to go to root).
  - Example: `cd Projects\2025`

- lcd [<local_path>]
  - Show or change the local download directory. Example: `lcd /tmp/smb-downloads`

lsf — recursive listing with filters (core discovery command)

Syntax: lsf [FILTERS and OPTIONS]

Supported filters and options (can be combined):
- ext:<ext1,ext2> or shorthand `pdf` or `.pdf`
  - Filter by extension(s), case-insensitive. Example: `lsf ext:pdf,docx` or `lsf pdf`

- pattern:<wildcard> or shorthand with `*`/`?`
  - fnmatch-style wildcard applied to the filename (basename). Example: `lsf Xy*` or `lsf pattern:report_??.pdf`

- path:<remote_path>
  - Restrict scanning to a remote subpath (relative to the current share). Example: `lsf path:Projects\2025`

- size:min-max
  - Filter by size range. Numbers accept k/m/g suffixes. Examples:
    - `lsf size:1k-10m` (between 1 KiB and 10 MiB)
    - `lsf size:10m-` (10 MiB and larger)

- date:YYYY-MM-DD..YYYY-MM-DD
  - Filter by modification date range (inclusive). Example: `lsf date:2023-01-01..2023-12-31`
  - Note: files without a detectable mtime will be excluded by date filters.

Options:
- --stream
  - Low-memory streaming mode: results are printed and optionally exported as they are discovered (no full in-memory list).
  - Example: `lsf --stream --export=out.csv`

- --export=FILE
  - Write results to FILE. Default output format is CSV; use --json to write JSON.
  - Example: `lsf ext:pdf --export=pdfs.csv`

- --json or --format=json
  - Write JSON output instead of CSV.
  - Example: `lsf --export=out.json --json`

- --export-columns=col1,col2,...
  - Choose which columns to include in CSV/JSON. Supported columns: `path`, `size`, `mtime` (ISO). Default: `path,size,mtime`.
  - Example: `lsf --export=out.csv --export-columns=path,size`

- --template="{...}"
  - Print custom formatted lines for each match using Python str.format with fields `{path}`, `{size}`, `{mtime}` (mtime will be ISO string). Example: `lsf --stream --template="{path} | {size} bytes"`

- --no-progress
  - Disable progress bars and tqdm usage for downstream commands (useful in non-interactive environments).

- --chunk-size=N
  - Override read chunk size (bytes) used during downloads. Default is 1 MiB.

- --multipart-threshold=N
  - Override size threshold (bytes) above which multipart downloads are attempted. Default 32 MiB.

- --max-parts=N
  - Override maximum parts used when performing multipart downloads. Default 8.

Examples (lsf)
- List all files recursively under current path:
  - `lsf`

- List all .pdf files under current path and export to CSV:
  - `lsf ext:pdf --export=pdfs.csv`

- Stream a huge tree and export selected columns to a CSV file as entries are discovered:
  - `lsf --stream --export=streamed.csv --export-columns=path,size`

- Use a pattern and template (print custom rows):
  - `lsf Xy* --template="{path}|{size}|{mtime}"`

select — pick items from last lsf result

- `select 1-5,8` — sets the current filtered set to the items with the specified indexes (1-based) from the last non-streaming `lsf` output.
- `select clear` — clear selection.

transfer — download the currently filtered set

Usage: transfer [OPTIONS] [INDEX_EXPR]

Options:
- --dry-run or --dry
  - Print the list of files that would be downloaded and exit (no network activity).

- --threads=N or an integer argument
  - Set the number of worker threads used for concurrent downloads.
  - Example: `transfer --threads=8` or `transfer 8`.

- INDEX_EXPR (e.g. `1-5,8`)
  - Download only the indexed items from the last `lsf` output.

Behavior and notes:
- Downloads preserve the remote path under the local directory (set by `lcd`). E.g., `Projects\2025\a.pdf` -> `<local_dir>/Projects/2025/a.pdf`.
- Resumable: if a local partial file exists, the downloader will resume from the current size.
- Multipart segmented downloads: files larger than `--multipart-threshold` (default 32 MiB) may be split into up to `--max-parts` parts and downloaded in parallel. This speeds up transfers over high-latency links.
- If multipart fails for a file, the downloader falls back to single-stream resumable download.

Example transfers
- Preview (dry-run) everything discovered by the last lsf:
  - `transfer --dry-run`

- Download all currently filtered items with 8 threads:
  - `transfer --threads=8`

- Download just items 2–4 from the last lsf:
  - `transfer 2-4`

mget — legacy convenience

- `mget <pattern>` performs a recursive download from the current path using server globbing where available (equivalent to start_sync with a pattern). Example: `mget *.zip`.

Advanced: multipart & performance tuning

Multipart basics
- Multipart splits a large file into byte ranges (parts) and downloads parts concurrently using separate SMB connections. The parts are combined after successful download.

Tuning knobs
- --chunk-size: read block size used while streaming segments. Larger sizes reduce syscall overhead but increase memory per-read.
- --multipart-threshold: file size above which multipart is attempted.
- --max-parts: maximum number of concurrent parts per file.

Recommendations
- LAN (low-latency): multipart may not help much; prefer fewer parts and larger chunk size.
- WAN (high-latency): multipart can greatly increase throughput by parallelizing round-trip-limited reads.
- If the server enforces connection limits or throttling, reduce `--max-parts` and/or `--threads`.

Automatic fallbacks
- If multipart fails for a file, the downloader will log the failure and fall back to the single-stream resumable download.

Streaming / low-memory mode
- Use `lsf --stream` to print and export matching files as they are discovered. This avoids loading a complete file list into memory and is intended for very large repositories.
- Streaming can be combined with `--export` or `--template` to save or format results as they are discovered.

Exporting: CSV / JSON / columns / templates
- CSV default columns: `path,size,mtime` (mtime in ISO format if available).
- Use `--export-columns=col1,col2` to select which columns to include in CSV export.
- Use `--template` to format lines per result; fields available: `{path}`, `{size}`, `{mtime}` (ISO string or empty).
- JSON export stores a list of objects: `[{"path":..., "size":..., "mtime":...}, ...]`.

mtime detection details
- The scanner attempts many attribute accessors and common formats to detect file modification times. It recognizes:
  - Impacket-provided getters (various names)
  - Unix epoch seconds
  - Unix epoch milliseconds
  - Windows FILETIME (100-ns ticks since 1601) and converts to UTC
- When a server does not expose a mtime, the scanner returns `None` for that file and date-based filters will omit that entry.

Testing
- Unit tests for scanning and filtering can be run with pytest:

```bash
pytest -q
```

The tests use mocked SMB responses to validate the filtering and index parsing logic.

Troubleshooting & tips
- If progress bars interfere with non-interactive logging or CI, install without `tqdm` or use `--no-progress`.
- If you see repeated disconnects during multipart downloads, reduce `--max-parts` and `--threads`.
- For huge shares use `lsf --stream --export=out.csv` to avoid memory exhaustion.
- Avoid passing plaintext passwords on the command line in production. Use password prompt, secure secrets manager, or NTLM hashes where appropriate.

