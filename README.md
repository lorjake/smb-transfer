# smb-transfer

smb-transfer is an interactive, resilient SMB client built on top of Impacket. It provides a REPL for exploring SMB shares, performing recursive file discovery with powerful filters (by extension, path, and filename wildcards), and reliably transferring filtered file sets with multi-threaded, resumable downloads.

This repository is an improved version of `impacket/examples/smbclient.py` focused on large-scale, resumable transfers and advanced file discovery and filtering.

---

## What's new in this version

In addition to earlier features (recursive lsf, transfer), the repository now includes:

- Dry-run / preview mode for `transfer` (`transfer --dry-run`) to show which files would be downloaded without starting transfers.
- Export from `lsf` into CSV or JSON (`lsf --export=path/to/out.csv` or `lsf --export=out.json --json`).
- Additional filters in `lsf`:
  - size:min-max (supports k/m/g suffixes) e.g. `lsf size:1k-10m`.
  - date:YYYY-MM-DD..YYYY-MM-DD e.g. `lsf date:2023-01-01..2023-12-31`.
- Interactive numbered selection: after running `lsf`, choose files by index with `select 1-5,8` then `transfer` to download just those.
- Unit tests (pytest) for scanning and filtering logic using mocked SMB responses.

---

## Requirements

- Python 3.8+
- impacket
- pytest (for running tests)

Install dependencies in a virtualenv:

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install impacket pytest
```

---

## Quick start

Run the client:

```bash
python3 smb_sync.py 'username[:password]@target' [-threads N] [-target-ip IP] [-port 139|445] [-debug]
```

Interactive example:

```text
SMB> shares
SMB> use SharedDocs
SMB> lcd /tmp/smb-downloads
SMB> cd Projects\2025
SMB> lsf ext:pdf
SMB> select 1-3
SMB> transfer --dry-run
SMB> transfer --threads 8
```

---

## Running tests

From the repository root:

```bash
pytest -q
```

This runs unit tests placed in `tests/` which mock SMB server responses for the scanning and filtering logic.
