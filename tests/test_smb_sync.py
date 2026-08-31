import pytest
from smb_sync import ResilientDownloader, InteractiveSMBShell, parse_index_set
from types import SimpleNamespace
from datetime import datetime


class FakeItem:
    def __init__(self, name, is_dir=False, size=0, mtime=None):
        self._name = name
        self._is_dir = is_dir
        self._size = size
        self._mtime = mtime

    def get_longname(self):
        return self._name

    def is_directory(self):
        return self._is_dir

    def get_filesize(self):
        return self._size

    def get_mtime(self):
        # return epoch
        if self._mtime is None:
            return None
        return int(self._mtime.timestamp())


class FakeSMB:
    def __init__(self, listing_map):
        # listing_map: path -> list of FakeItem
        self.map = listing_map

    def listPath(self, share, path):
        # path is something like "" or "Folder\\*" or "Folder\\Name"
        # normalize
        p = path
        # if endswith \* then list folder
        if p.endswith('\\*'):
            key = p[:-3]
            return self.map.get(key, [])
        return self.map.get(p, [])


@pytest.fixture
def sample_tree():
    # build a fake tree:
    # root: file1.txt, dirA
    # dirA: file2.pdf, subB
    # dirA\subB: file3.mp3
    now = datetime(2023, 6, 1, 12, 0, 0)
    listing = {
        "": [FakeItem('file1.txt', is_dir=False, size=100), FakeItem('dirA', is_dir=True)],
        "dirA\\*": [FakeItem('file2.pdf', is_dir=False, size=2048, mtime=now), FakeItem('subB', is_dir=True)],
        "dirA\\subB\\*": [FakeItem('file3.mp3', is_dir=False, size=4096)],
    }
    return FakeSMB(listing)


def test_scan_and_ext_filter(sample_tree):
    mgr = SimpleNamespace()
    downloader = ResilientDownloader(mgr, 'SHARE', num_threads=1)
    results = downloader.scan_files(sample_tree, '')
    assert any(r['path'] == 'file1.txt' for r in results)
    assert any(r['path'] == 'dirA\\file2.pdf' for r in results)
    assert any(r['path'] == 'dirA\\subB\\file3.mp3' for r in results)

    # apply extension filter via InteractiveSMBShell method
    shell = InteractiveSMBShell(mgr, threads=1)
    shell.last_scan = results
    filtered = shell._apply_filters(results, {'ext': ['pdf']})
    assert len(filtered) == 1
    assert filtered[0]['path'].endswith('.pdf')


def test_pattern_filter(sample_tree):
    mgr = SimpleNamespace()
    downloader = ResilientDownloader(mgr, 'SHARE')
    results = downloader.scan_files(sample_tree, '')
    shell = InteractiveSMBShell(mgr)
    filtered = shell._apply_filters(results, {'pattern': 'file3.*'})
    assert len(filtered) == 1
    assert filtered[0]['path'].endswith('file3.mp3')


def test_path_filter(sample_tree):
    mgr = SimpleNamespace()
    downloader = ResilientDownloader(mgr, 'SHARE')
    results = downloader.scan_files(sample_tree, '')
    shell = InteractiveSMBShell(mgr)
    filtered = shell._apply_filters(results, {'path': 'dirA\\subB'})
    assert len(filtered) == 1
    assert filtered[0]['path'].startswith('dirA\\subB')


def test_size_and_date_filters(sample_tree):
    mgr = SimpleNamespace()
    downloader = ResilientDownloader(mgr, 'SHARE')
    results = downloader.scan_files(sample_tree, '')
    shell = InteractiveSMBShell(mgr)
    # size: files >=2000
    filtered = shell._apply_filters(results, {'min_size': 2000})
    assert any(r['size'] >= 2000 for r in filtered)
    # date filter: file2.pdf has mtime 2023-06-01
    df = datetime(2023, 5, 1)
    dt = datetime(2023, 7, 1)
    filtered_date = shell._apply_filters(results, {'date_from': df, 'date_to': dt})
    assert any(r['path'].endswith('.pdf') for r in filtered_date)


def test_parse_index_set():
    s = '1-3,5,7'
    idx = parse_index_set(s)
    assert idx == [0, 1, 2, 4, 6]
