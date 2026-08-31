#!/usr/bin/env python3
import os
import sys
import cmd
import time
import logging
import argparse
import fnmatch
import shlex
import csv
import json
import re
from datetime import datetime
from queue import Queue
from threading import Thread
from impacket.examples import logger
from impacket.examples.utils import parse_target
from impacket.smbconnection import SMBConnection
from impacket.smb3structs import FILE_READ_DATA

CHUNK_SIZE = 64 * 1024


def parse_index_set(s: str):
    """Parse index expressions like "1,3-5,8" into a sorted list of unique 0-based indices."""
    if not s:
        return []
    parts = re.split(r'[ ,]+', s.strip())
    idxs = set()
    for p in parts:
        if not p:
            continue
        if '-' in p:
            a, b = p.split('-', 1)
            try:
                a_i = int(a)
                b_i = int(b)
            except ValueError:
                continue
            if a_i <= 0 or b_i <= 0:
                continue
            for i in range(a_i, b_i + 1):
                idxs.add(i - 1)
        else:
            try:
                v = int(p)
            except ValueError:
                continue
            if v <= 0:
                continue
            idxs.add(v - 1)
    return sorted(idxs)


class SMBManager:
    """Manages thread-safe SMB connections and auto-reconnection."""
    def __init__(self, address, target_ip, port, domain, username, password, lmhash='', nthash=''):
        self.address = address
        self.target_ip = target_ip
        self.port = int(port)
        self.domain = domain
        self.username = username
        self.password = password
        self.lmhash = lmhash
        self.nthash = nthash

    def create_connection(self, max_retries=10, retry_delay=5):
        attempt = 0
        while attempt < max_retries:
            try:
                smb = SMBConnection(self.address, self.target_ip, sess_port=self.port)
                smb.login(self.username, self.password, self.domain, self.lmhash, self.nthash)
                return smb
            except Exception as e:
                attempt += 1
                logging.warning(f"[Reconnect] Connection dropped ({e}). Retry ({attempt}/{max_retries}) in {retry_delay}s...")
                time.sleep(retry_delay)
        raise ConnectionError("Failed to re-establish SMB connection after max retries.")


class ResilientDownloader:
    """Multi-threaded downloader with directory scanning, resume capabilities and exporting."""
    def __init__(self, smb_mgr, share, num_threads=4):
        self.smb_mgr = smb_mgr
        self.share = share
        self.num_threads = num_threads
        self.task_queue = Queue()

    def _get_item_mtime(self, item):
        # Try common attributes used by impacket FileEntry objects; return datetime or None
        for attr in ('get_mtime', 'get_last_write_time', 'get_mtime_epoch'):
            fn = getattr(item, attr, None)
            if callable(fn):
                try:
                    val = fn()
                    # If it's an int/float epoch
                    if isinstance(val, (int, float)):
                        try:
                            return datetime.fromtimestamp(val)
                        except Exception:
                            return None
                    # If it's already a datetime
                    if isinstance(val, datetime):
                        return val
                except Exception:
                    continue
        # Some item objects expose 'get_time' or 'get_create_time' - attempt .get_time
        try:
            val = getattr(item, 'get_time', None)
            if callable(val):
                v = val()
                if isinstance(v, (int, float)):
                    return datetime.fromtimestamp(v)
        except Exception:
            pass
        return None

    def scan_files(self, smb, remote_dir):
        """Recursively scans a remote SMB directory and returns list of dicts with path/size/mtime.
        remote_dir: path relative to share (use backslashes). Empty string means root of share.
        Returns: [{'path': str, 'size': int, 'mtime': datetime|None}, ...]
        """
        results = []
        search_path = f"{remote_dir}\\*" if remote_dir else "*"
        try:
            items = smb.listPath(self.share, search_path)
        except Exception as e:
            logging.error(f"Failed to list directory '{remote_dir}': {e}")
            return results

        for item in items:
            name = item.get_longname()
            if name in ['.', '..']:
                continue

            r_subpath = f"{remote_dir}\\{name}" if remote_dir else name

            if item.is_directory():
                results.extend(self.scan_files(smb, r_subpath))
            else:
                try:
                    size = item.get_filesize()
                except Exception:
                    size = 0
                mtime = self._get_item_mtime(item)
                results.append({'path': r_subpath, 'size': size, 'mtime': mtime})
        return results

    def discover_directory(self, smb, remote_dir, local_dir):
        """Queue all files from a remote directory recursively (keeps compatibility with older code)."""
        os.makedirs(local_dir, exist_ok=True)
        files = self.scan_files(smb, remote_dir)
        for f in files:
            remote_path = f['path']
            size = f['size']
            local_path = os.path.join(local_dir, *remote_path.split('\\'))
            self.task_queue.put((remote_path, local_path, size))

    def discover_pattern(self, smb, current_remote_path, local_dir, pattern):
        """Discovers items matching a specific wildcard or file/folder name using SMB server globbing."""
        if current_remote_path:
            search_path = f"{current_remote_path}\\{pattern}"
        else:
            search_path = pattern

        try:
            items = smb.listPath(self.share, search_path)
        except Exception as e:
            logging.error(f"Failed to match pattern '{pattern}' in '{current_remote_path}': {e}")
            return

        for item in items:
            name = item.get_longname()
            if name in ['.', '..']:
                continue

            r_subpath = f"{current_remote_path}\\{name}" if current_remote_path else name
            l_subpath = os.path.join(local_dir, name)

            if item.is_directory():
                self.discover_directory(smb, r_subpath, l_subpath)
            else:
                try:
                    size = item.get_filesize()
                except Exception:
                    size = 0
                self.task_queue.put((r_subpath, l_subpath, size))

    def start_sync(self, current_remote_path, local_dir, pattern="*"):
        main_smb = self.smb_mgr.create_connection()

        if pattern in ['*', '']:
            logging.info(f"Indexing all contents in '\\{current_remote_path}'...")
            self.discover_directory(main_smb, current_remote_path, local_dir)
        else:
            logging.info(f"Indexing pattern '{pattern}' in '\\{current_remote_path}'...")
            self.discover_pattern(main_smb, current_remote_path, local_dir, pattern)

        main_smb.logoff()

        total = self.task_queue.qsize()
        if total == 0:
            logging.info("No files found to download.")
            return

        logging.info(f"Downloading {total} files using {self.num_threads} threads...")
        threads = []
        for _ in range(self.num_threads):
            t = Thread(target=self.download_worker)
            t.daemon = True
            t.start()
            threads.append(t)

        self.task_queue.join()

        for _ in range(self.num_threads):
            self.task_queue.put(None)
        for t in threads:
            t.join()

        logging.info("Transfer completed.")

    def start_sync_from_list(self, file_list, local_base_dir):
        """Start downloads from an explicit list of remote files: file_list is [{'path':..., 'size':..., 'mtime':...}, ...].
        local_base_dir is the local directory where files will be saved preserving remote path structure.
        """
        for f in file_list:
            remote_path = f['path']
            size = f.get('size', 0)
            local_path = os.path.join(local_base_dir, *remote_path.split('\\'))
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            self.task_queue.put((remote_path, local_path, size))

        total = self.task_queue.qsize()
        if total == 0:
            logging.info("No files queued for download.")
            return

        logging.info(f"Downloading {total} files using {self.num_threads} threads...")
        threads = []
        for _ in range(self.num_threads):
            t = Thread(target=self.download_worker)
            t.daemon = True
            t.start()
            threads.append(t)

        self.task_queue.join()

        for _ in range(self.num_threads):
            self.task_queue.put(None)
        for t in threads:
            t.join()

        logging.info("Transfer completed.")

    def download_worker(self):
        """Worker thread executing file downloads with byte-offset resuming."""
        smb = None
        while True:
            task = self.task_queue.get()
            if task is None:
                if smb:
                    try:
                        smb.logoff()
                    except:
                        pass
                self.task_queue.task_done()
                break

            remote_file, local_file, expected_size = task
            os.makedirs(os.path.dirname(local_file), exist_ok=True)

            start_offset = 0
            if os.path.exists(local_file):
                start_offset = os.path.getsize(local_file)
                if start_offset == expected_size:
                    logging.info(f"[SKIP] {remote_file} (Already complete)")
                    self.task_queue.task_done()
                    continue
                elif start_offset > expected_size:
                    start_offset = 0

            mode = 'ab' if start_offset > 0 else 'wb'
            if start_offset > 0:
                logging.info(f"[RESUME] {remote_file} from byte {start_offset}/{expected_size}")
            else:
                logging.info(f"[START] {remote_file} ({expected_size} bytes)")

            offset = start_offset
            while offset < expected_size:
                try:
                    if smb is None:
                        smb = self.smb_mgr.create_connection()

                    tid = smb.connectTree(self.share)
                    fid = smb.openFile(tid, remote_file, desiredAccess=FILE_READ_DATA)

                    with open(local_file, mode) as lf:
                        while offset < expected_size:
                            bytes_to_read = min(CHUNK_SIZE, expected_size - offset)
                            chunk = smb.readFile(tid, fid, offset, bytes_to_read)
                            if not chunk:
                                break
                            lf.write(chunk)
                            offset += len(chunk)

                    smb.closeFile(tid, fid)
                    smb.disconnectTree(tid)

                except Exception as e:
                    logging.warning(f"[INTERRUPTED] {remote_file} at {offset}/{expected_size}. Error: {e}")
                    smb = None
                    mode = 'ab'
                    time.sleep(3)

            if offset == expected_size:
                logging.info(f"[COMPLETE] {remote_file}")

            self.task_queue.task_done()


class InteractiveSMBShell(cmd.Cmd):
    """Interactive Shell supporting multi-threaded resilient downloads and advanced file filtering."""
    prompt = "SMB> "

    def __init__(self, smb_mgr, threads=4):
        super().__init__()
        self.smb_mgr = smb_mgr
        self.threads = threads
        self.smb = self.smb_mgr.create_connection()
        self.current_share = None
        self.current_path = ""
        self.local_dir = os.getcwd()
        self.last_scan = []    # full list of scanned dicts [{'path','size','mtime'}]
        self.last_filtered = []
        print("[+] Authenticated! Type 'help' or 'shares' to list available shares.\n")

    def update_prompt(self):
        if self.current_share:
            path_str = f"\\{self.current_path}" if self.current_path else ""
            self.prompt = f"SMB ({self.current_share}{path_str})> "
        else:
            self.prompt = "SMB> "

    def do_shares(self, line):
        """List available shares on target server."""
        try:
            resp = self.smb.listShares()
            print("\nAvailable Shares:")
            print("-" * 45)
            for share in resp:
                name = share['shi1_netname'][:-1]
                remark = share['shi1_remark'][:-1]
                print(f"  {name:<20} {remark}")
            print("-" * 45 + "\n")
        except Exception as e:
            print(f"[-] Error listing shares: {e}")

    def do_use(self, line):
        """Connect to an SMB share: use <SHARE_NAME>"""
        share_name = line.strip().strip('\\/')
        if not share_name:
            print("Usage: use <SHARE_NAME>")
            return
        try:
            tid = self.smb.connectTree(share_name)
            self.smb.disconnectTree(tid)
            self.current_share = share_name
            self.current_path = ""
            self.update_prompt()
            print(f"[+] Connected to share: {share_name}")
        except Exception as e:
            print(f"[-] Failed to connect to share '{share_name}': {e}")

    def do_ls(self, line):
        """List files and folders in current directory."""
        if not self.current_share:
            print("[-] Error: Select a share first using 'use <SHARE_NAME>'")
            return

        search_path = f"{self.current_path}\\*" if self.current_path else "*"
        try:
            files = self.smb.listPath(self.current_share, search_path)
            print(f"\nDirectory listing for \\{self.current_path}:")
            print("-" * 45)
            for f in files:
                fname = f.get_longname()
                ftype = "<DIR>" if f.is_directory() else f"{f.get_filesize():>10} bytes"
                print(f"  {ftype:<14} {fname}")
            print("-" * 45 + "\n")
        except Exception as e:
            print(f"[-] Error listing directory: {e}")

    def do_cd(self, line):
        """Change directory: cd <folder> or cd .."""
        if not self.current_share:
            print("[-] Error: Select a share first using 'use <SHARE_NAME>'")
            return

        target = line.strip().replace('/', '\\')
        if not target:
            self.current_path = ""
        elif target == "..":
            parts = [p for p in self.current_path.split('\\') if p]
            if parts:
                parts.pop()
            self.current_path = "\\".join(parts)
        else:
            if self.current_path:
                self.current_path = f"{self.current_path}\\{target}"
            else:
                self.current_path = target
        self.update_prompt()

    def do_lcd(self, line):
        """Change or view local directory: lcd <local_path>"""
        path = line.strip()
        if not path:
            print(f"Current local directory: {self.local_dir}")
            return
        os.makedirs(path, exist_ok=True)
        self.local_dir = os.path.abspath(path)
        print(f"[+] Local download path set to: {self.local_dir}")

    def _parse_lsf_args(self, argline):
        """Parse lsf argument line and return filters dict and options.
        Supports:
          ext:csv list, pattern:PAT, path:REMOTE_PATH
          size:min-max (bytes or k/m/g suffix), date:YYYY-MM-DD..YYYY-MM-DD
          --export filename.csv/json and --format json/csv
        Returns: (filters, options) where filters is dict and options holds export/format
        """
        filters = {}
        options = {'export': None, 'format': 'csv'}
        if not argline:
            return filters, options
        parts = shlex.split(argline)
        for p in parts:
            if p.startswith('--export='):
                options['export'] = p.split('=', 1)[1]
                continue
            if p == '--json' or p == '--format=json':
                options['format'] = 'json'
                continue
            if p.startswith('ext:'):
                exts = [e.strip().lstrip('.') .lower() for e in p[len('ext:'):].split(',') if e.strip()]
                filters['ext'] = exts
                continue
            if p.startswith('pattern:'):
                filters['pattern'] = p[len('pattern:'):]
                continue
            if p.startswith('path:'):
                filters['path'] = p[len('path:'):].strip().replace('/', '\\')
                continue
            if p.startswith('size:'):
                rng = p[len('size:'):]
                # format min-max where each can be empty
                lo, hi = None, None
                if '-' in rng:
                    a, b = rng.split('-', 1)
                    lo = a.strip() or None
                    hi = b.strip() or None
                else:
                    lo = rng
                def parse_size_token(t):
                    if t is None:
                        return None
                    t = t.strip().lower()
                    m = re.match(r'^(\d+)([kmg])?$', t)
                    if not m:
                        try:
                            return int(t)
                        except Exception:
                            return None
                    n = int(m.group(1))
                    suf = m.group(2)
                    if not suf:
                        return n
                    if suf == 'k':
                        return n * 1024
                    if suf == 'm':
                        return n * 1024 * 1024
                    if suf == 'g':
                        return n * 1024 * 1024 * 1024
                    return n
                filters['min_size'] = parse_size_token(lo)
                filters['max_size'] = parse_size_token(hi)
                continue
            if p.startswith('date:'):
                rng = p[len('date:'):]
                lo, hi = None, None
                if '..' in rng:
                    a, b = rng.split('..', 1)
                    lo = a.strip() or None
                    hi = b.strip() or None
                else:
                    lo = rng
                def parse_date_token(t):
                    if not t:
                        return None
                    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
                        try:
                            return datetime.strptime(t, fmt)
                        except Exception:
                            continue
                    return None
                filters['date_from'] = parse_date_token(lo)
                filters['date_to'] = parse_date_token(hi)
                continue
            # shorthand wildcard or extension
            if '*' in p or '?' in p:
                filters['pattern'] = p
                continue
            # extension shorthand like pdf or .pdf
            if p.startswith('.'):
                filters['ext'] = [p.lstrip('.').lower()]
                continue
            if re.match(r'^[a-zA-Z0-9]+$', p) and '.' not in p:
                # treat as extension shorthand
                filters['ext'] = [p.lower()]
                continue
            # unknown token - ignore or maybe treat as pattern
        return filters, options

    def _apply_filters(self, entries, filters):
        """Filter list of entries (dicts). Returns filtered list.
        Supported filters: ext, pattern, path, min_size, max_size, date_from, date_to
        """
        out = []
        for e in entries:
            path = e['path']
            size = e.get('size', 0) or 0
            mtime = e.get('mtime')
            basename = os.path.basename(path)
            # path filter
            if 'path' in filters:
                p = filters['path'].lstrip('\\')
                norm_remote = path.lstrip('\\')
                if not norm_remote.lower().startswith(p.lower()):
                    continue
                out.append(e)
                continue
            # extension filter
            if 'ext' in filters:
                exts = filters['ext']
                if any(basename.lower().endswith('.' + ex) for ex in exts):
                    out.append(e)
                continue
            # pattern filter
            if 'pattern' in filters:
                pat = filters['pattern']
                if fnmatch.fnmatch(basename, pat):
                    out.append(e)
                continue
            # size filters
            if filters.get('min_size') is not None and size < filters.get('min_size'):
                continue
            if filters.get('max_size') is not None and size > filters.get('max_size'):
                continue
            # date filters
            if filters.get('date_from') is not None:
                if mtime is None or mtime < filters.get('date_from'):
                    continue
            if filters.get('date_to') is not None:
                if mtime is None or mtime > filters.get('date_to'):
                    continue
            # if no special filters matched above, and none applied, accept
            if not any(k in filters for k in ('ext', 'pattern', 'path')):
                out.append(e)
        return out

    def do_mget(self, line):
        """Recursively download contents from current directory:
        mget *        (Downloads everything recursively in current directory)
        mget          (Same as mget *)
        mget *.pdf    (Downloads matching extension files/folders recursively)
        mget Folder   (Downloads specific subfolder recursively)
        """
        if not self.current_share:
            print("[-] Error: Select a share first using 'use <SHARE_NAME>'")
            return

        pattern = line.strip().replace('/', '\\')
        downloader = ResilientDownloader(self.smb_mgr, self.current_share, self.threads)
        downloader.start_sync(self.current_path, self.local_dir, pattern=pattern)

    def do_lsf(self, line):
        """List files from current share recursively and optionally filter them.

        Usage examples:
          lsf                 -> list all files recursively from current path
          lsf ext:pdf         -> show only .pdf files
          lsf pdf             -> same as ext:pdf
          lsf .mp3            -> show mp3 files
          lsf pattern:Xy*     -> wildcard pattern on filename
          lsf path:Folder\\Sub -> show files only under that remote path (recursive)
          lsf size:1k-10m     -> min and max size (supports k/m/g suffix)
          lsf date:2023-01-01..2023-12-31 -> date range (YYYY-MM-DD)
          lsf --export=out.csv    -> export results (CSV by default)
          lsf --json              -> export JSON
        """
        if not self.current_share:
            print("[-] Error: Select a share first using 'use <SHARE_NAME>'")
            return

        filters, options = self._parse_lsf_args(line)
        downloader = ResilientDownloader(self.smb_mgr, self.current_share, self.threads)

        print("[+] Scanning remote files (this may take a while)...")
        all_files = downloader.scan_files(self.smb, self.current_path)
        self.last_scan = all_files

        filtered = self._apply_filters(all_files, filters)
        self.last_filtered = filtered

        # display results as numbered tree-like with sizes and mtime
        print(f"\nFound {len(filtered)} files:")
        print("-" * 120)
        for idx, e in enumerate(filtered, start=1):
            mtime_str = e['mtime'].strftime('%Y-%m-%d %H:%M:%S') if e.get('mtime') else 'N/A'
            print(f"{idx:4d}. {e['path']}  -  {e['size']} bytes  -  {mtime_str}")
        print("-" * 120 + "\n")

        # export if requested
        if options.get('export'):
            outpath = options['export']
            fmt = options.get('format', 'csv')
            try:
                if fmt == 'json':
                    with open(outpath, 'w', encoding='utf-8') as jf:
                        json.dump(filtered, jf, default=str, indent=2)
                else:
                    with open(outpath, 'w', newline='', encoding='utf-8') as cf:
                        writer = csv.writer(cf)
                        writer.writerow(['path', 'size', 'mtime'])
                        for e in filtered:
                            writer.writerow([e['path'], e['size'], e['mtime'].isoformat() if e.get('mtime') else ''])
                print(f"[+] Exported {len(filtered)} results to {outpath}")
            except Exception as ex:
                print(f"[-] Failed to export results: {ex}")

    def do_select(self, line):
        """Select items from the last lsf output to be the current filtered set.
        Usage: select 1,3-5  (selects by indexes shown by lsf; 1-based)
        select clear        (clears selection)
        """
        if not self.last_filtered:
            print("[-] No lsf results to select from. Run 'lsf' first.")
            return
        arg = line.strip()
        if not arg:
            print("Usage: select <index-set>  (e.g. select 1-5,8)")
            return
        if arg.lower() == 'clear':
            self.last_filtered = []
            print("[+] Selection cleared.")
            return
        idxs = parse_index_set(arg)
        picked = []
        for i in idxs:
            if 0 <= i < len(self.last_filtered):
                picked.append(self.last_filtered[i])
        if not picked:
            print("[-] No matching indexes found in last lsf output.")
            return
        self.last_filtered = picked
        print(f"[+] Selected {len(picked)} items for transfer.")

    def do_transfer(self, line):
        """Transfer the currently filtered files into the local download directory.

        Usage:
          transfer                       -> downloads files from last 'lsf' filter into local dir
          transfer --dry-run             -> show what would be downloaded without starting
          transfer --threads N           -> set number of threads for this transfer
          transfer 1-5,8                 -> transfer only the indexed items from last lsf output
          transfer --dry-run 1-5         -> combine options
        """
        if not self.current_share:
            print("[-] Error: Select a share first using 'use <SHARE_NAME>'")
            return

        if not self.last_filtered:
            print("[-] No filtered files found. Run 'lsf' (with optional filter) first.")
            return

        parts = shlex.split(line)
        dry_run = False
        nthreads = self.threads
        index_expr = None
        i = 0
        while i < len(parts):
            p = parts[i]
            if p in ('--dry-run', '--dry'):
                dry_run = True
                i += 1
                continue
            if p.startswith('--threads='):
                try:
                    nthreads = int(p.split('=', 1)[1])
                except Exception:
                    pass
                i += 1
                continue
            if p == '--threads' and i + 1 < len(parts):
                try:
                    nthreads = int(parts[i + 1])
                except Exception:
                    pass
                i += 2
                continue
            # leftover token treat as index expression or thread count numeric
            if re.match(r'^\d+$', p) and len(parts) == 1:
                try:
                    nthreads = int(p)
                except Exception:
                    pass
                i += 1
                continue
            # otherwise treat as indices
            index_expr = p
            i += 1

        to_transfer = self.last_filtered
        if index_expr:
            idxs = parse_index_set(index_expr)
            selected = []
            for j in idxs:
                if 0 <= j < len(self.last_filtered):
                    selected.append(self.last_filtered[j])
            to_transfer = selected

        if not to_transfer:
            print("[-] No files selected for transfer.")
            return

        print(f"[+] Preparing to transfer {len(to_transfer)} files to {self.local_dir}")
        if dry_run:
            print("[DRY-RUN] The following files would be downloaded:")
            for e in to_transfer:
                print(f"  {e['path']}  -  {e['size']} bytes")
            return

        downloader = ResilientDownloader(self.smb_mgr, self.current_share, nthreads)
        downloader.start_sync_from_list(to_transfer, self.local_dir)

    def do_exit(self, line):
        """Exit the shell."""
        print("Goodbye!")
        return True

    def do_EOF(self, line):
        return True


# Minimal CLI entrypoint
def main():
    parser = argparse.ArgumentParser(description="Resilient Interactive SMB Client")
    parser.add_argument('target', help='[[domain/]username[:password]@]<targetName or address>')
    parser.add_argument('-threads', type=int, default=4, help='Number of concurrent download threads (default: 4)')
    parser.add_argument('-hashes', help='NTLM hashes, format LMHASH:NTHASH')
    parser.add_argument('-target-ip', help='IP Address of target machine')
    parser.add_argument('-port', choices=['139', '445'], default='445', help='Destination TCP port')
    parser.add_argument('-debug', action='store_true', help='Enable debug logging')

    options = parser.parse_args()
    logger.init(True, options.debug)

    domain, username, password, address = parse_target(options.target)
    target_ip = options.target_ip or address

    lmhash, nthash = '', ''
    if options.hashes:
        lmhash, nthash = options.hashes.split(':')

    if not password and username and not options.hashes:
        from getpass import getpass
        password = getpass("Password: ")

    try:
        smb_mgr = SMBManager(address, target_ip, options.port, domain, username, password, lmhash, nthash)
        shell = InteractiveSMBShell(smb_mgr, options.threads)
        shell.cmdloop()
    except Exception as e:
        logging.error(f"Connection error: {e}")


if __name__ == '__main__':
    main()
