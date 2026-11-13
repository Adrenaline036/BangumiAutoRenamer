"""计算进入生产镜像的应用文件指纹，不写文件。"""
from pathlib import Path
import hashlib
import json


def source_fingerprint(root: Path) -> str:
    files = {}
    for path in sorted((root / "src").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("requirements.txt", "start.py", "LICENSE"):
        files[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    print(source_fingerprint(Path(__file__).resolve().parents[1]))
