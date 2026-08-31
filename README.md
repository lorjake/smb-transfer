```markdown name=README.md
# smb-transfer

smb-transfer is an interactive, resilient SMB client built on top of Impacket. It provides a REPL for exploring SMB shares, performing recursive file discovery with powerful filters (by extension, path, and filename wildcards), and reliably transferring filtered file sets with multi-threaded, resumable downloads.

This repository is an improved, focused implementation inspired by impacket's example smbclient, optimized for large-scale/resumable transfers and advanced file discovery.


## Features

- Interactive shell (REPL) to list shares, change directories and inspect remote files.
- Recursive file scanning with the `lsf` command and flexible filtering:
  - Filter by extension (e.g., `lsf ext:pdf` or `lsf pdf`)
  - Filter by wildcard/pattern (e.g., `lsf pattern:Xy*` or `lsf Xy*`)
  - Filter by remote path (e.g., `lsf path:Folder\SubFolder`)
- Download filtered files in bulk using `transfer` with multi-threaded, resumable downloads.
- Preserves remote path structure under the local download directory.
- Resilient connection management with automatic reconnection and chunked/resumable transfers.

---


## Installation

No advanced install is required — the project is a single Python script.

```bash

python3 smb_sync.py '[DOMAIN/]username[:password]@<target>' [-threads N] [-target-ip IP] [-port 139|445] [-debug]
python3 smb_sync.py  admin:password@192.168.0.1 -thread 8

```


## Quick start

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

## Behavior, limits and tips

- Memory / performance: `lsf` builds an in-memory list of discovered files. On huge shares this can be slow and memory intensive. Use `lsf path:...` to limit scope.
- Server-side globbing vs recursion:
  - Where possible, server-side globbing is used (listPath with a pattern). The client falls back to explicit recursion to ensure correct results on servers with limited pattern support.
- Path separators:
  - Remote paths use backslashes. The shell accepts forward slashes and normalizes them automatically.
- Case sensitivity:
  - Extension filters are case-insensitive.
  - Filename pattern matching uses `fnmatch` behavior; awareness of case sensitivity depends on platform and pattern.
- Security:
  - Avoid passing plaintext passwords on the command line. Use password prompt or hashed credentials if required.
- Resumable downloads:
  - If a local file exists and is smaller than the remote file, the downloader attempts to resume from the local file size. If the local file is larger, it restarts the transfer.

---

