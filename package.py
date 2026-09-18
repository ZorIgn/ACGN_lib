"""Build the Windows portable directory and archive."""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist" / "ACGLib"


def main():
    """Bundle the runtime, application and matching source for Windows."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-only", action="store_true")
    parser.add_argument("--installer", action="store_true")
    parser.add_argument("--iscc", type=Path)
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
        "desktop/Launcher.cs",
        "desktop/installer.iss",
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
    compiler = (
        Path(os.environ.get("WINDIR", "C:/Windows"))
        / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    )
    subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/optimize+",
            "/platform:anycpu",
            f"/out:{DIST / 'ACGLib.exe'}",
            f"/win32icon:{ROOT / 'src/static/img/acglib.ico'}",
            "/reference:System.Windows.Forms.dll",
            "/reference:System.Drawing.dll",
            str(ROOT / "desktop/Launcher.cs"),
        ],
        check=True,
    )
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
            if (
                relative.name == ".env"
                or "db" in relative.parts
                or "staticfiles" in relative.parts
            ):
                continue
            package.write(file, Path("ACGLib") / relative)
    print(f"Package: {target} ({target.stat().st_size / 1024 / 1024:.1f} MiB)")
    artifacts = [target]
    if args.installer:
        compiler = (
            args.iscc or shutil.which("iscc") or BUILD / "tools/InnoSetup/ISCC.exe"
        )
        if not Path(compiler).exists():
            raise RuntimeError("Install Inno Setup and pass --iscc path/to/ISCC.exe")
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
        subprocess.run(
            [
                str(compiler),
                f"/DAppVersion={version}",
                f"/DPackageRoot={DIST}",
                str(ROOT / "desktop/installer.iss"),
            ],
            check=True,
        )
        artifacts.append(ROOT / "dist" / f"ACGLib-{version}-Windows-Setup.exe")
    (ROOT / "dist/SHA256SUMS.txt").write_text(
        "".join(
            f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}\n"
            for file in artifacts
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
