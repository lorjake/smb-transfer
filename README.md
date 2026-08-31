# smb-transfer

smb-transfer is an interactive, resilient SMB client built on top of Impacket. It provides a small REPL for exploring SMB shares, performing recursive file listings with powerful filters (by extension, path, and filename wildcards), and reliably transferring filtered file sets with multi-threaded, resumable downloads.

This project is an improved version of `impacket/examples/smbclient.py` focused on large-scale, resumable transfers and advanced file discovery and filtering.

---

## Features

- Interactive shell (REPL) to list shares, change directories and inspect remote files.
- Recursive file scanning with the `lsf` command and flexible filtering:
  - Filter by extension (e.g., `lsf ext:pdf` or `lsf pdf`)
  - Filter by wildcard/pattern (e.g., `lsf pattern:Xy*` or `lsf Xy*`)
  - Filter by remote path (e.g., `lsf path:Folder\\SubFolder`)
- Download filtered files in bulk using `transfer` with multi-threaded, resumable downloads.
- Resilient connection management with automatic reconnection on transient SMB errors.
- Preserves remote path structure under a local download directory.

---

## Requirements

- Python 3.8+
- impacket

Install dependencies with pip (preferably in a virtualenv):

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install impacket
```

Note: Depending on your environment you may need additional system libraries for Impacket or to run with appropriate privileges.

---

## Quick start — run the client

Usage (CLI):

```bash
python3 smb_sync.py '[DOMAIN/]username[:password]@<target>' [-threads N] [-target-ip IP] [-port 139|445] [-debug]
```

Examples:

- Connect with username and prompt for password:
  ```bash
  python3 smb_sync.py 'DOMAIN/jdoe@fileserver'
  # Password prompt will appear
  ```

- Connect with inline password (be careful — this leaves credentials in your shell history):
  ```bash
  python3 smb_sync.py 'jdoe:SuperSecret@192.168.1.10' -threads 8
  ```

- Use NTLM hashes instead of a password: `-hashes LMHASH:NTHASH`.

After starting, you'll land in an interactive SMB> prompt.

---

## Interactive commands (complete reference)

All commands are entered at the `SMB>` prompt after connecting.

- shares
  - Description: List available shares on the target server.
  - Example:
    ```text
    SMB> shares
    Available Shares:
      C$                   Default share
      SharedDocs           Documents
    ```

- use <SHARE_NAME>
  - Description: Select a particular share to operate on (sets the current share and path to root).
  - Example:
    ```text
    SMB> use SharedDocs
    [+] Connected to share: SharedDocs
    SMB (SharedDocs)> 
    ```

- ls
  - Description: List files/folders under the current remote path (non-recursive).
  - Example:
    ```text
    SMB (SharedDocs)> ls
    Directory listing for \:
      <DIR>       Projects
      1024 bytes  README.pdf
    ```

- cd <folder>
  - Description: Change the current remote path. Use `cd ..` to go up and `cd` with no args to go to share root.
  - Example:
    ```text
    SMB> cd Projects
    SMB (SharedDocs\Projects)> ls
    ```

- lcd [<local_path>]
  - Description: Change the local download directory (where files are saved). Without args shows the current download path.
  - Example:
    ```text
    SMB> lcd /tmp/smb-downloads
    [+] Local download path set to: /tmp/smb-downloads
    ```

- lsf [FILTER]
  - Description: Recursively scan for files starting from the current remote path, apply optional filters, and print matching files with their remote path and size. Results are cached for later transfer with `transfer`.
  - Supported filter forms (examples):
    - No args: list all files recursively under current path
      - `lsf`
    - Extension filter (single or comma-separated):
      - `lsf ext:pdf`
      - `lsf pdf` (shorthand)
      - `lsf ext:pdf,docx` (multiple extensions)
      - `lsf .mp3`
    - Wildcard/pattern (fnmatch style):
      - `lsf pattern:Xy*`
      - `lsf Xy*` (shorthand)
      - `lsf *.zip`
    - Path filter (restrict to a remote subpath, recursive):
      - `lsf path:Folder\\SubFolder`
      - `lsf path:Projects\\2025`
  - Behavior notes:
    - The scan is recursive and returns a line per matched file: remote path and size in bytes.
    - Filters are case-insensitive when comparing extensions and path prefixes; filename pattern matching uses fnmatch semantics (case-sensitive depending on platform).
  - Example session:
    ```text
    SMB (SharedDocs\Projects)> lsf
    [+] Scanning remote files (this may take a while)...

    Found 42 files:
    Projects\\2025\\report_final.pdf  -  512000 bytes
    Projects\\2025\\slides.pptx        -  2048000 bytes
    ...
    ```

    Filter by extension:
    ```text
    SMB> lsf ext:pdf
    Found 8 files:
    Projects\\2025\\report_final.pdf  -  512000 bytes
    Projects\\2024\\notes.pdf         -  102400 bytes
    ```

    Filter by pattern:
    ```text
    SMB> lsf Xy*
    Found 3 files:
    Shared\\XyProject\\Xy_readme.txt  -  2048 bytes
    ```

    Filter by path:
    ```text
    SMB> lsf path:Projects\\2025
    Found 5 files:
    Projects\\2025\\report_final.pdf  -  512000 bytes
    ```
    ```

- transfer [<nthreads>]
  - Description: Download the files from the last `lsf` filtered result into the local directory set by `lcd`. If no `lsf` has been run, this command will prompt that there are no files.
  - Optional numeric argument overrides the number of download threads for this run.
  - Behavior:
    - Downloads preserve the remote path structure under your local directory (e.g., a file remote `Projects\\2025\\a.pdf` will be saved to `<local_dir>/Projects/2025/a.pdf`).
    - Downloads are chunked and resumable. If a partial file already exists locally the downloader will attempt to resume from the existing size.
  - Examples:
    ```text
    SMB> transfer
    [+] Downloading 8 files using 4 threads...
    [START] Projects\\2025\\report_final.pdf (512000 bytes)
    [COMPLETE] Projects\\2025\\report_final.pdf
    ```

    With custom thread count:
    ```text
    SMB> transfer 8
    [+] Downloading 8 files using 8 threads...
    ```

- mget <pattern>
  - Description: Backwards-compatible convenience command that performs a recursive `mget` of the given pattern from the current remote path. Examples accepted are `mget *`, `mget *.pdf`, `mget Folder`.
  - Example:
    ```text
    SMB> mget *.zip
    # Starts a recursive download of matching zip files
    ```

- exit / EOF
  - Description: Exit the interactive shell.

---

## Example step-by-step workflow

1) Start the client and authenticate:

```bash
python3 smb_sync.py 'mydomain/jdoe@fileserver' -threads 4
# Password prompt -> provide password
```

2) List shares and select a share:

```text
SMB> shares
SMB> use SharedDocs
```

3) Optionally change to a remote folder and set local directory:

```text
SMB> cd Projects
SMB> lcd /tmp/smb-downloads
```

4) Find all PDF files recursively under the current path:

```text
SMB> lsf ext:pdf
Found 12 files:
Projects\\2025\\report_final.pdf  -  512000 bytes
Projects\\Archives\\old.docx      -  20480 bytes  # (not displayed — example only pdfs)
```

5) Transfer the filtered PDF files to your local machine (use 8 threads):

```text
SMB> transfer 8
```

6) Confirm files are under `/tmp/smb-downloads/Projects/...`.

---

## Notes, tips and limitations

- Large shares: `lsf` performs a full recursive enumeration. On huge shares this may be slow and memory intensive (the program stores the scan in memory). If you plan large enumerations, ensure adequate memory or run targeted `lsf path:...` queries.
- Wildcard vs server-side matching: The client tries to use SMB server globbing where possible, but will recurse when needed to find matches. Some SMB servers may limit or paginate directory listings — this client expects the `listPath` calls to return full directory contents.
- Case sensitivity: Extension filters are treated case-insensitively. fnmatch pattern matching behaves according to Python's fnmatch semantics (which may be case-sensitive depending on the platform and pattern).
- Security: Be careful with inline passwords on the command line. Consider using NTLM hash option or a secure credential retrieval mechanism.

---

## Contributing and extending

- Add new filters: size (min/max), modified date ranges, include/exclude lists.
- Add CSV/JSON export of `lsf` results for automation (`lsf --export results.csv`).
- Add interactive numbered selection to pick individual files to transfer from `lsf` results.
- Add unit tests for the filtering/scanning logic (mock `listPath` responses).

If you'd like, I can implement: a dry-run mode, CSV export, additional filters (size/date), or tests. Tell me which and I'll add them.
