import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('patch_plaso', Path(__file__).resolve().parents[1] / 'patch_plaso.py')
patch_plaso = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch_plaso)


class PatchTests(unittest.TestCase):
    def test_expected_source_is_patched_and_compiles(self):
        source = '''class Plugin:
    def _GetNetworkInfo(self, signatures_key):
        for category in signatures_key:
            for signature in category:
                registry_value = signature
                if registry_value:
                    default_gateway_mac_address = ":".join(
                        [f"{octet:02x}" for octet in bytearray(registry_value.data)]
                    )
                else:
                    default_gateway_mac_address = None
    def ExtractEvents(self, signatures, parser_mediator):
        network_info = self._GetNetworkInfo(signatures)
'''
        result = patch_plaso.patch_source(source)
        compile(result, '<patched plugin>', 'exec')
        self.assertIn('self._GetNetworkInfo(signatures, parser_mediator)', result)
        self.assertIn('registry_value.data is not None', result)
        self.assertIn('parser_mediator.ProduceWarning(', result)
        with self.assertRaisesRegex(ValueError, 'review patch'):
            patch_plaso.patch_source(result)
        with self.assertRaisesRegex(ValueError, 'review patch'):
            patch_plaso.patch_source(source.replace('self._GetNetworkInfo(signatures)', 'other()'))
