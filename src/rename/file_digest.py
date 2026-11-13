"""Non-cryptographic content comparison; never a Plan/approval fingerprint."""
from pathlib import Path
import re

from xxhash import xxh3_128, xxh3_128_hexdigest


def is_xxh3_128(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def digest_bytes(data):
    return xxh3_128_hexdigest(data)


def digest_file(path):
    """Read once in bounded chunks. The caller owns path/stat safety checks."""
    result = xxh3_128()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()
