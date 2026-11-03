"""Apply the build-time fix for unreadable NetworkList gateway values."""
from pathlib import Path

CHANGES = (
    ("def _GetNetworkInfo(self, signatures_key):", "def _GetNetworkInfo(self, signatures_key, parser_mediator):"),
    ("self._GetNetworkInfo(signatures)", "self._GetNetworkInfo(signatures, parser_mediator)"),
    ('''                if registry_value:
                    default_gateway_mac_address = ":".join(
                        [f"{octet:02x}" for octet in bytearray(registry_value.data)]
                    )
                else:
                    default_gateway_mac_address = None
''', '''                if registry_value and registry_value.data is not None:
                    default_gateway_mac_address = ":".join(
                        [f"{octet:02x}" for octet in bytearray(registry_value.data)]
                    )
                else:
                    default_gateway_mac_address = None
                    if registry_value:
                        parser_mediator.ProduceWarning(
                            "DefaultGatewayMac has no readable data; gateway address omitted"
                        )
'''),
)


def patch_source(source):
    """Apply only to the expected upstream source; never partially patch it."""
    for old, new in CHANGES:
        if source.count(old) != 1:
            raise ValueError("Plaso NetworkList plugin changed; review patch before building")
        source = source.replace(old, new, 1)
    return source


def main():
    # Plaso exists in the image, not necessarily on the host running the launcher.
    from plaso.parsers.winreg_plugins import networks

    path = Path(networks.__file__)
    try:
        source = patch_source(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")


if __name__ == '__main__':
    main()
