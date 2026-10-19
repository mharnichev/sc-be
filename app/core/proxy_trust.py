"""Validate the explicit proxy network boundary without contacting a proxy."""
import os
import sys
from ipaddress import ip_network


def validate_proxy_allowlist(value: str) -> None:
    entries=value.split(',')
    if not entries or any(not entry.strip() for entry in entries):
        raise ValueError('An explicit trusted proxy IP/network list is required')
    for entry in entries:
        network=ip_network(entry.strip(),strict=True)
        if network.prefixlen==0:
            raise ValueError('A catch-all trusted proxy network is forbidden')


def main() -> int:
    try:
        validate_proxy_allowlist(os.environ.get('TRUSTED_PROXY_IPS','127.0.0.1'))
    except ValueError:
        print('Invalid TRUSTED_PROXY_IPS: use explicit proxy IPs/networks; catch-all trust is forbidden',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
