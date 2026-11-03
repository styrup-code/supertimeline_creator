import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('vhdx_reader', Path(__file__).resolve().parents[1] / 'vhdx_reader.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def entry(name, kind, data=b'', children=()):
    metadata = SimpleNamespace(flags=1, type=kind, size=len(data),
                               mtime=1234567890, mtime_nano=123456700,
                               atime=1234567800, atime_nano=0)
    obj = SimpleNamespace(info=SimpleNamespace(name=SimpleNamespace(name=name), meta=metadata))
    obj.as_directory = lambda: children
    obj.read_random = lambda offset, size: data[offset:offset + size]
    return obj


class VhdxTests(unittest.TestCase):
    def fake_modules(self, children, parent=None):
        class Disk:
            parent_filename = parent
            media_size = 4096
            def open(self, name, mode):
                assert mode == 'r'
            def close(self):
                pass
            def read_buffer_at_offset(self, size, offset):
                data = bytearray(size)
                if offset == 0 and size >= 512:
                    data[3:11] = b'NTFS    '
                return bytes(data)
        class Image:
            def __init__(self, **kwargs):
                pass
        class FileSystem:
            def __init__(self, image, **kwargs):
                if image.read(510, 2) != b'\x55\xaa':
                    raise OSError('missing signature')
            def open_dir(self, path):
                return children
        tsk = SimpleNamespace(Img_Info=Image, TSK_IMG_TYPE_EXTERNAL=1,
                              TSK_VS_PART_FLAG_ALLOC=1, TSK_FS_TYPE_NTFS=1,
                              TSK_FS_META_FLAG_ALLOC=1, TSK_FS_META_TYPE_DIR=2,
                              TSK_FS_META_TYPE_REG=3, FS_Info=FileSystem)
        # Use the unpartitioned filesystem fallback for these synthetic fixtures.
        def no_partition(image):
            raise OSError('no partition table')
        tsk.Volume_Info = no_partition
        return {'pytsk3': tsk, 'pyvhdi': SimpleNamespace(file=Disk)}

    def test_extracts_drive_mft_with_read_only_signature_overlay(self):
        root = [entry(b'$MFT', 3, b'container-mft'),
                entry(b'C', 2, children=[entry(b'$MFT', 3, b'collected-mft'), entry(b'file', 3, b'data')])]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, self.fake_modules(root)):
            dest = Path(tmp)
            mfts, warnings = reader.extract_vhdx(Path('sample.vhdx'), dest, lambda x: None)
            self.assertEqual([p.relative_to(dest).as_posix() for p in mfts], ['C/$MFT'])
            self.assertEqual(mfts[0].read_bytes(), b'collected-mft')
            self.assertEqual(mfts[0].stat().st_mtime_ns, 1234567890123456700)
            self.assertTrue(any('in-memory signature' in message for message in warnings))
            self.assertFalse((dest / '$MFT').exists())

    def test_rejects_unsafe_name_and_differencing_image(self):
        root = [entry(b'C', 2, children=[entry(b'../unsafe', 3, b'data')])]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, self.fake_modules(root)):
            with self.assertRaisesRegex(ValueError, 'Unsafe VHDX filename'):
                reader.extract_vhdx(Path('sample.vhdx'), Path(tmp), lambda x: None)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, self.fake_modules([], 'parent.vhdx')):
            with self.assertRaisesRegex(ValueError, 'Differencing VHDX'):
                reader.extract_vhdx(Path('sample.vhdx'), Path(tmp), lambda x: None)
