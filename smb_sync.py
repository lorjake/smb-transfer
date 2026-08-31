#!/usr/bin/env python3
import os
import sys
import cmd
import time
import logging
import argparse
import fnmatch
from queue import Queue
from threading import Thread
from impacket.examples import logger
from impacket.examples.utils import parse_target
from impacket.smbconnection import SMBConnection
from impacket.smb3structs import FILE_READ_DATA

CHUNK_SIZE = 64 * 1024


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
    """Multi-threaded downloader with directory scanning and resume capabilities."""
    def __init__(self, smb_mgr, share, num_threads=4):
        self.smb_mgr = smb_mgr
        self.share = share
        self.num_threads = num_threads
        self.task_queue = Queue()

    def scan_files(self, smb, remote_dir):
        """Recursively scans a remote SMB directory and returns list of (remote_path, filesize).
        remote_dir: path relative to share (use backslashes). Empty string means root of share.
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
                results.append((r_subpath, item.get_filesize()))
        return results

    def discover_directory(self, smb, remote_dir, local_dir):
        """Queue all files from a remote directory recursively (keeps compatibility with older code)."""
        os.makedirs(local_dir, exist_ok=True)
        files = self.scan_files(smb, remote_dir)
        for remote_path, size in files:
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
                self.task_queue.put((r_subpath, l_subpath, item.get_filesize()))

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
        """Start downloads from an explicit list of remote files: file_list is [(remote_path, size), ...].
        local_base_dir is the local directory where files will be saved preserving remote path structure.
        """
        for remote_path, size in file_list:
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
        self.last_scan = []    # full list of scanned files [(remote_path, size), ...]
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

    def _parse_lsf_args(self, argline):
        """Parse lsf argument line and return filters dict:
        Supported filters:
          - ext:<ext1,ext2> or .pdf or pdf  -> extension(s)
          - path:<remote_path>             -> remote path to restrict to (relative to share)
          - pattern:<wildcard> or wildcard with * or ? -> fnmatch pattern on filename
        If no filter specified, returns empty dict meaning 'all files'.
        """
        args = argline.strip()
        filters = {}
        if not args:
            return filters

        # path filter
        if args.startswith('path:'):
            filters['path'] = args[len('path:'):].strip().replace('/', '\\')
            return filters

        # pattern filter
        if args.startswith('pattern:'):
            filters['pattern'] = args[len('pattern:'):].strip()
            return filters

        # ext filter like ext:pdf or .pdf or pdf
        if args.startswith('ext:'):
            exts = [e.strip().lstrip('.') .lower() for e in args[len('ext:'):].split(',') if e.strip()]
            filters['ext'] = exts
            return filters

        # wildcard direct
        if '*' in args or '?' in args:
            filters['pattern'] = args
            return filters

        # single extension or filename
        if args.startswith('.') or '.' in args and '/' not in args and '\\' not in args:
            # treat as extension or filename
            if args.startswith('.'):
                filters['ext'] = [args.lstrip('.').lower()]
            elif args.count('.') == 1 and '*' not in args:
                # treat like filename or extension
                if args.startswith('*.'):
                    filters['pattern'] = args
                else:
                    # if begins with * treat pattern else if just 'pdf' assume extension
                    if args.lower().isdigit():
                        filters['pattern'] = args
                    else:
                        if args.lower().startswith('*.'):
                            filters['pattern'] = args
                        else:
                            filters['ext'] = [args.lower()]
            else:
                filters['pattern'] = args
            return filters

        # fallback: treat as extension name without dot
        if args:
            filters['ext'] = [args.lower()]
        return filters

    def do_lsf(self, line):
        """List files from current share recursively and optionally filter them.

        Usage examples:
          lsf                 -> list all files recursively from current path
          lsf ext:pdf         -> show only .pdf files
          lsf pdf             -> same as ext:pdf
          lsf .mp3            -> show mp3 files
          lsf pattern:Xy*     -> wildcard pattern on filename
          lsf path:Folder\\Sub -> show files only under that remote path (recursive)
        The results are cached in memory and you can use 'transfer' to download the filtered set.
        """
        if not self.current_share:
            print("[-] Error: Select a share first using 'use <SHARE_NAME>'")
            return

        filters = self._parse_lsf_args(line)
        downloader = ResilientDownloader(self.smb_mgr, self.current_share, self.threads)

        print("[+] Scanning remote files (this may take a while)...")
        all_files = downloader.scan_files(self.smb, self.current_path)
        self.last_scan = all_files

        filtered = []
        for remote_path, size in all_files:
            basename = os.path.basename(remote_path)
            # path filter
            if 'path' in filters:
                p = filters['path'].lstrip('\\')
                # normalize for comparison
                norm_remote = remote_path.lstrip('\\')
                if not norm_remote.lower().startswith(p.lower()):
                    continue
                filtered.append((remote_path, size))
                continue

            # extension filter
            if 'ext' in filters:
                exts = filters['ext']
                if any(basename.lower().endswith('.' + e) for e in exts):
                    filtered.append((remote_path, size))
                continue

            # pattern filter
            if 'pattern' in filters:
                pat = filters['pattern']
                if fnmatch.fnmatch(basename, pat):
                    filtered.append((remote_path, size))
                continue

            # no filter -> accept all
            filtered.append((remote_path, size))

        self.last_filtered = filtered

        # display results as tree-like with sizes
        print(f"\nFound {len(filtered)} files:")
        print("-" * 80)
        for remote_path, size in filtered:
            print(f"{remote_path}  -  {size} bytes")
        print("-" * 80 + "\n")

    def do_transfer(self, line):
        """Transfer the currently filtered files into the local download directory.

        Usage:
          transfer           -> downloads files from last 'lsf' filter into local dir
          transfer <nthreads> -> override number of threads for this transfer
        """
        if not self.current_share:
            print("[-] Error: Select a share first using 'use <SHARE_NAME>'")
            return

        if not self.last_filtered:
            print("[-] No filtered files found. Run 'lsf' (with optional filter) first.")
            return

        try:
            nthreads = int(line.strip()) if line.strip() else self.threads
        except ValueError:
            print("[-] Invalid thread count. Using default.")
            nthreads = self.threads

        downloader = ResilientDownloader(self.smb_mgr, self.current_share, nthreads)
        downloader.start_sync_from_list(self.last_filtered, self.local_dir)

    def do_exit(self, line):
        """Exit the shell."""
        print("Goodbye!")
        return True

    def do_EOF(self, line):
        return True


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
