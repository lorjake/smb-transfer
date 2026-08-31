#!/usr/bin/env python3
import os
import sys
import cmd
import time
import logging
import argparse
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

    def discover_directory(self, smb, remote_dir, local_dir):
        """Recursively scans a remote SMB directory and queues all files."""
        os.makedirs(local_dir, exist_ok=True)
        search_path = f"{remote_dir}\\*" if remote_dir else "*"

        try:
            items = smb.listPath(self.share, search_path)
        except Exception as e:
            logging.error(f"Failed to list directory '{remote_dir}': {e}")
            return

        for item in items:
            name = item.get_longname()
            if name in ['.', '..']:
                continue

            r_subpath = f"{remote_dir}\\{name}" if remote_dir else name
            l_subpath = os.path.join(local_dir, name)

            if item.is_directory():
                self.discover_directory(smb, r_subpath, l_subpath)
            else:
                self.task_queue.put((r_subpath, l_subpath, item.get_filesize()))

    def discover_pattern(self, smb, current_remote_path, local_dir, pattern):
        """Discovers items matching a specific wildcard or file/folder name."""
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

    def download_worker(self):
        """Worker thread executing file downloads with byte-offset resuming."""
        smb = None
        while True:
            task = self.task_queue.get()
            if task is None:
                if smb:
                    try: smb.logoff()
                    except: pass
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


class InteractiveSMBShell(cmd.Cmd):
    """Interactive Shell supporting multi-threaded resilient downloads."""
    prompt = "SMB> "

    def __init__(self, smb_mgr, threads=4):
        super().__init__()
        self.smb_mgr = smb_mgr
        self.threads = threads
        self.smb = self.smb_mgr.create_connection()
        self.current_share = None
        self.current_path = ""
        self.local_dir = os.getcwd()
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
