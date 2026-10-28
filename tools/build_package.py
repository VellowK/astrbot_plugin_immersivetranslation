"""Build a source-only AstrBot ZIP from an explicit allow-list, never local state.

Run: python tools/build_package.py
The resulting ZIP is not an offline engine distribution.
"""

import argparse
import re
import stat
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
NAME = "astrbot_plugin_immersivetranslation"
SOURCES = (
    "__init__.py",
    "main.py",
    "intent.py",
    "bridge.py",
    "download.py",
    "process.py",
    "runtime.py",
    "engine_runner.py",
    "errors.py",
    "_conf_schema.json",
    "metadata.yaml",
    "requirements.txt",
    "requirements-engine.txt",
    "requirements-engine-ocr.txt",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    ".gitignore",
    "tests/ci-workflow.yml",
    "tools/build_package.py",
)


def source_paths(root: Path) -> list[Path]:
    paths = [root / name for name in SOURCES]
    # Include developer tests for reproducibility, not samples, logs or __pycache__.
    paths.extend(sorted((root / "tests").glob("test_*.py")))
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or unsafe source file: {path.relative_to(root)}")
    return paths


def build(root: Path, destination: Path) -> int:
    paths = source_paths(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "x", compression=ZIP_DEFLATED) as archive:
        for path in paths:
            info = ZipInfo(NAME + "/" + path.relative_to(root).as_posix())
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return len(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    metadata = (ROOT / "metadata.yaml").read_text("utf-8")
    match = re.search(r"^version: (v[0-9]+\.[0-9]+\.[0-9]+)$", metadata, re.MULTILINE)
    if not match:
        parser.error("metadata.yaml must have a plain semantic version")
    destination = args.output or ROOT / "dist" / f"{NAME}-{match[1]}.zip"
    count = build(ROOT, destination)
    print(f"Built {destination.resolve()} ({count} source files; dependencies not bundled)")


if __name__ == "__main__":
    main()
