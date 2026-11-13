"""Explicit lightweight content evidence for companion-file operations."""
from dataclasses import dataclass
from pathlib import Path
from .file_digest import digest_file, is_xxh3_128
from .media_file_identity import media_identity

SMALL_CONTENT = 'content-xxh3-128-v1'


@dataclass(frozen=True)
class SmallFileFingerprint:
    size: int
    xxh3_128: str
    device: int
    inode: int
    kind: str = SMALL_CONTENT

    def __post_init__(self):
        if (self.kind != SMALL_CONTENT or not is_xxh3_128(self.xxh3_128)
                or any(type(v) is not int or v < 0 for v in (self.size,self.device,self.inode))
                or self.inode == 0):
            raise ValueError('小文件内容证据无效')



def small_fingerprint(path):
    path=Path(path)
    before=media_identity(path); stamp=path.stat().st_mtime_ns
    value=digest_file(path)
    if before != media_identity(path) or stamp != path.stat().st_mtime_ns:
        raise ValueError('小文件在读取期间发生变化')
    return SmallFileFingerprint(before.size,value,before.device,before.inode)
