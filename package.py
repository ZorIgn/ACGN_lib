"""Build the Windows portable directory and archive."""

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist" / "ACGLib"


def main():
    """Bundle the pinned runtime, application source and initial review list."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-only", action="store_true")
    args = parser.parse_args()
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    if not args.archive_only:
        archive = BUILD / "python-3.12.10-embed-amd64.zip"
        if not archive.exists():
            import requests

            response = requests.get(
                "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
                timeout=120,
            )
            response.raise_for_status()
            archive.write_bytes(response.content)
        if (
            hashlib.md5(archive.read_bytes()).hexdigest()
            != "fe8ef205f2e9c3ba44d0cf9954e1abd3"
        ):
            raise RuntimeError("Python runtime checksum mismatch")
        with zipfile.ZipFile(archive) as package:
            package.extractall(DIST / "runtime")
        requirements = BUILD / "requirements.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--no-hashes",
                "--output-file",
                str(requirements),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        target = DIST / "runtime" / "site-packages"
        if not (target / "django").exists():
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    "--target",
                    str(target),
                    "--link-mode=copy",
                    "-r",
                    str(requirements),
                ],
                check=True,
            )
    (DIST / "runtime" / "python312._pth").write_text(
        "python312.zip\n.\nsite-packages\nimport site\n", encoding="utf-8"
    )
    source_files = [
        "run.py",
        "package.py",
        "start.ps1",
        "stop.ps1",
        "启动.cmd",
        "停止.cmd",
        "允许手机访问.cmd",
        "allow-lan.ps1",
        "使用说明.md",
        "README.md",
        "docs/implementation.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        ".env.example",
    ]
    for name in source_files:
        (DIST / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, DIST / name)
    shutil.copytree(
        ROOT / "src",
        DIST / "src",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "db", "staticfiles", "__pycache__", "*.pyc", "tests"
        ),
    )
    (DIST / "src" / "db").mkdir(exist_ok=True)
    initial = ROOT / "src" / "db" / "initial-library.json"
    if initial.exists():
        shutil.copy2(initial, DIST / "src" / "db" / initial.name)
    with zipfile.ZipFile(DIST / "source.zip", "w", zipfile.ZIP_DEFLATED) as source:
        for name in source_files:
            source.write(ROOT / name, name)
        for file in (ROOT / "src").rglob("*"):
            if file.is_file() and not {"db", "staticfiles", "__pycache__"}.intersection(
                file.relative_to(ROOT / "src").parts
            ):
                source.write(file, file.relative_to(ROOT))
    target = ROOT / "dist" / "ACGLib-Windows.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for file in DIST.rglob("*"):
            if not file.is_file() or "__pycache__" in file.parts:
                continue
            relative = file.relative_to(DIST)
            if relative.name == ".env" or (
                "db" in relative.parts and relative.name != "initial-library.json"
            ):
                continue
            package.write(file, Path("ACGLib") / relative)
    print(f"Package: {target} ({target.stat().st_size / 1024 / 1024:.1f} MiB)")


if __name__ == "__main__":
    main()
