#!/usr/bin/env python3
"""Build script for AnkiGit addon.

Usage:
  python build.py all     # clean → build → package
  python build.py clean   # remove build artifacts
  python build.py build   # copy addon files to build/
  python build.py package # create .ankiaddon zip
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Pure-Python runtime dependencies bundled into the add-on (Anki add-ons cannot
# pip install). Version specifiers are taken from pyproject.toml.
VENDORED_PACKAGES = ["gitpython"]


def get_vendor_requirements():
    import re
    import tomllib

    with open("pyproject.toml", "rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    reqs = [
        d for d in deps
        if re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0].lower() in VENDORED_PACKAGES
    ]
    if len(reqs) != len(VENDORED_PACKAGES):
        print(f"ERROR: {VENDORED_PACKAGES} not all found in pyproject.toml dependencies")
        sys.exit(1)
    return reqs


def vendor_dependencies(build_dir):
    vendor_dir = build_dir / "vendor"
    reqs = get_vendor_requirements()
    print(f"  Vendoring {', '.join(reqs)} into {vendor_dir} ...")
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--target", str(vendor_dir),
            "--only-binary", ":all:",
            "--no-compile", "--no-user", "--disable-pip-version-check",
            *reqs,
        ],
        check=True,
    )
    for pycache in vendor_dir.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)


def get_version():
    try:
        import tomllib
        with open("pyproject.toml", "rb") as f:
            pyproject_version = tomllib.load(f)["project"]["version"]
    except Exception:
        pyproject_version = "0.1.1"

    init_path = Path("anki_git") / "__init__.py"
    init_version = None
    for line in init_path.read_text().splitlines():
        if line.startswith("version"):
            init_version = line.split("=")[1].strip().strip('"').strip("'")
            break

    if init_version and init_version != pyproject_version:
        print(
            f"ERROR: Version mismatch! "
            f"pyproject.toml: {pyproject_version}, "
            f"anki_git/__init__.py: {init_version}"
        )
        sys.exit(1)

    return pyproject_version


def clean():
    print("Cleaning build artifacts...")
    for p in ["build", "*.egg-info", "__pycache__"]:
        for found in Path(".").rglob(p):
            if found.is_dir():
                shutil.rmtree(found, ignore_errors=True)
    print("  Done")


def build_addon():
    version = get_version()
    print(f"Building AnkiGit v{version}...")

    build_dir = Path("build") / "anki_git_addon"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    # Copy addon source
    src = Path("anki_git")
    if not src.exists():
        print("ERROR: anki_git/ not found")
        sys.exit(1)

    for item in src.iterdir():
        if item.name in ("__pycache__", "meta.json", "vendor"):
            continue
        dst = build_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(item, dst)

    vendor_dependencies(build_dir)

    # Generate manifest.json
    manifest = {
        "package": "AnkiGit",
        "name": "AnkiGit",
        "version": version,
    }
    manifest_path = build_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"  manifest.json written")

    print(f"  Built at {build_dir}")


def create_package():
    version = get_version()
    print("Creating .ankiaddon package...")

    addon_dir = Path("build") / "anki_git_addon"
    if not addon_dir.exists():
        print("ERROR: build/anki_git_addon not found. Run 'build' first.")
        sys.exit(1)

    build_dir = Path("build")
    package_name = f"anki_git_v{version}.ankiaddon"
    package_path = build_dir / package_name

    with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(addon_dir):
            for d in dirs:
                dir_path = Path(root) / d
                arc_path = dir_path.relative_to(addon_dir)
                zf.write(dir_path, str(arc_path) + "/")
            for file in files:
                file_path = Path(root) / file
                arc_path = file_path.relative_to(addon_dir)
                zf.write(file_path, arc_path)

    print(f"  Package: {package_path}")
    print(f"  Size: {package_path.stat().st_size / 1024:.1f} KB")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    if cmd == "clean":
        clean()
    elif cmd == "build":
        build_addon()
    elif cmd == "package":
        build_addon()
        create_package()
    elif cmd == "all":
        clean()
        build_addon()
        create_package()
        print("\nBuild complete!")
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
