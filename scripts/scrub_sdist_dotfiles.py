from __future__ import annotations

import argparse
import os
import tarfile
import tempfile
from pathlib import Path


def has_dot_component(name: str) -> bool:
    return any(part.startswith(".") for part in Path(name).parts)


def scrub_archive(path: Path) -> int:
    removed = 0
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with tarfile.open(path, "r:gz") as source, tarfile.open(temp_path, "w:gz") as target:
            for member in source.getmembers():
                if has_dot_component(member.name):
                    removed += 1
                    continue
                fileobj = source.extractfile(member) if member.isfile() else None
                target.addfile(member, fileobj)
                if fileobj is not None:
                    fileobj.close()
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove dotfile entries from generated source distributions."
    )
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args()

    total_removed = 0
    for archive in args.archives:
        total_removed += scrub_archive(archive)
    entry_word = "entry" if total_removed == 1 else "entries"
    print(f"Removed {total_removed} dotfile {entry_word} from sdist archives.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
