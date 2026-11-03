"""Run inside the image to exercise valid and unreadable gateway values."""
from plaso.parsers.winreg_plugins.networks import NetworksWindowsRegistryPlugin


class Value:
    def __init__(self, data):
        self.data = data

    def GetDataAsObject(self):
        return self.data


class Key:
    def __init__(self, subkeys=(), values=None):
        self.subkeys = subkeys
        self.values = values or {}

    def GetSubkeys(self):
        return self.subkeys

    def GetValueByName(self, name):
        return self.values.get(name)


class Mediator:
    def __init__(self):
        self.warnings = []

    def ProduceWarning(self, warning):
        self.warnings.append(warning)


valid = Key(values={"ProfileGuid": Value("valid"), "DefaultGatewayMac": Value(b"\x01\x02\x03\x04\x05\x06")})
missing = Key(values={"ProfileGuid": Value("missing"), "DefaultGatewayMac": Value(None)})
absent = Key(values={"ProfileGuid": Value("absent")})
mediator = Mediator()
result = NetworksWindowsRegistryPlugin()._GetNetworkInfo(Key([Key([valid, missing, absent])]), mediator)
assert result == {"valid": ("01:02:03:04:05:06", None), "missing": (None, None), "absent": (None, None)}
assert len(mediator.warnings) == 1 and "DefaultGatewayMac" in mediator.warnings[0]
print("NetworkList fix: valid, missing-data, and absent-value cases passed")
