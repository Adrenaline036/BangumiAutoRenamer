"""Shared content CAS used under the owning Catalog's existing archive lock."""
import hashlib
import re


def require_archive_fingerprint(path, expected_fingerprint):
    if expected_fingerprint is None:
        return  # Explicit legacy maintenance caller, not a frozen detach intent.
    if (not isinstance(expected_fingerprint, str)
            or re.fullmatch('[0-9a-f]{64}', expected_fingerprint) is None
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected_fingerprint):
        raise ValueError('作品登记在确认后变化，不能归档，请刷新核查')
