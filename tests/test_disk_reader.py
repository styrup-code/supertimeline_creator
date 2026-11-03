import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('disk_reader', Path(__file__).resolve().parents[1] / 'disk_reader.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class DiskReaderTests(unittest.TestCase):
    def test_largest_allocated_partition_and_mft(self):
        partitions = [SimpleNamespace(flags=2, start=0, len=4),
                      SimpleNamespace(flags=1, start=4, len=10),
                      SimpleNamespace(flags=2, start=14, len=500),
                      SimpleNamespace(flags=1, start=514, len=30)]
        class Volumes:
            info = SimpleNamespace(block_size=512)
            def __iter__(self):
                return iter(partitions)
        class File:
            info = SimpleNamespace(meta=SimpleNamespace(size=3))
            def read_random(self, offset, size):
                return b'mft'[offset:offset + size]
        class Filesystem:
            info = SimpleNamespace(ftype=1)
            def __init__(self, image, offset):
                self.offset = offset
            def open(self, path):
                self.asserted_path = path
                return File()
        tsk = SimpleNamespace(Img_Info=lambda path: object(), Volume_Info=lambda image: Volumes(),
                              FS_Info=Filesystem, TSK_VS_PART_FLAG_ALLOC=1, TSK_FS_TYPE_NTFS=1)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {'pytsk3': tsk}):
            mfts, report, partition, is_ntfs = reader.prepare_disk(Path('sample.dd'), Path(tmp), 'D', lambda s: None)
            self.assertEqual(partition, 2)
            self.assertTrue(is_ntfs)
            self.assertEqual(mfts[0].read_bytes(), b'mft')
            self.assertEqual(mfts[0].relative_to(tmp).as_posix(), 'D/$MFT')
            self.assertTrue(any('offset 263168' in item for item in report))

    def test_unpartitioned_non_ntfs_skips_mft(self):
        class Image:
            def get_size(self):
                return 1000
        def no_partitions(image):
            raise OSError('no partition table')
        tsk = SimpleNamespace(Img_Info=lambda path: Image(), Volume_Info=no_partitions,
                              FS_Info=lambda image, offset: SimpleNamespace(info=SimpleNamespace(ftype=2)),
                              TSK_FS_TYPE_NTFS=1)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {'pytsk3': tsk}):
            mfts, report, partition, is_ntfs = reader.prepare_disk(Path('sample.raw'), Path(tmp), log=lambda s: None)
            self.assertEqual((mfts, partition, is_ntfs), ([], None, False))
            self.assertTrue(any('not NTFS' in item for item in report))
