"""本地发布到 PyPI，完全不依赖 GitHub Actions。

用法（在项目根目录执行）：
    python scripts/release_pypi.py --bump patch          # 升版本、构建、上传、打本地 tag
    python scripts/release_pypi.py --dry-run             # 只升版本并构建校验，不上传
    python scripts/release_pypi.py --push                # 上传成功后同时推送 main 与 tag

凭据从项目根目录的 .pypirc 读取（已在 .gitignore 中），上传用户名固定为 __token__。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "tg_signer" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
PYPIRC = ROOT / ".pypirc"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def run(cmd: list[str], **kwargs) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True, **kwargs)


def project_name() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^name = "([^"]+)"', text, re.MULTILINE)
    if not match:
        sys.exit("pyproject.toml 中找不到 name")
    return match.group(1)


def read_version() -> str:
    match = re.search(r'__version__ = "([^"]+)"', INIT.read_text(encoding="utf-8"))
    if not match:
        sys.exit("tg_signer/__init__.py 中找不到 __version__")
    return match.group(1)


def bump_version(part: str) -> str:
    old = read_version()
    major, minor, patch = (int(x) for x in old.split("."))
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    new = f"{major}.{minor}.{patch}"
    INIT.write_text(
        re.sub(
            r'__version__ = "[^"]+"',
            f'__version__ = "{new}"',
            INIT.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )
    print(f"版本 {old} -> {new}")
    return new


def pypi_has(name: str, version: str) -> bool:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        return exc.code != 404
    except urllib.error.URLError as exc:
        sys.exit(f"无法访问 PyPI：{exc.reason}")


def clean() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    DIST.mkdir(exist_ok=True)


def artifacts(version: str) -> list[str]:
    files = sorted(DIST.glob(f"*{version}*"))
    if not files:
        sys.exit("构建后没有找到产物")
    return [str(p) for p in files]


def git(*args: str) -> None:
    run(["git", *args])


def main() -> None:
    parser = argparse.ArgumentParser(description="本地发布 tg-signer-plus 到 PyPI")
    parser.add_argument(
        "--bump", choices=["patch", "minor", "major"], help="发布前升级版本号"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只构建和校验，不上传、不打 tag"
    )
    parser.add_argument(
        "--push", action="store_true", help="上传后推送 main 与 tag 到远端"
    )
    parser.add_argument("--skip-tag", action="store_true", help="不创建 git tag")
    args = parser.parse_args()

    name = project_name()
    if args.bump:
        version = bump_version(args.bump)
    else:
        version = read_version()
    print(f"包名 {name}，版本 {version}")

    if not args.dry_run and pypi_has(name, version):
        sys.exit(f"PyPI 上已存在 {name} {version}，请先 --bump 再发布")
    if not args.dry_run and not PYPIRC.exists():
        sys.exit(f"缺少凭据文件 {PYPIRC}，格式见 .pypirc 模板（username = __token__）")

    clean()
    run([sys.executable, "-m", "build", "--wheel", "--sdist"])
    run([sys.executable, "-m", "twine", "check", *artifacts(version)])

    if args.dry_run:
        print(f"dry-run 完成，产物在 {DIST}")
        return

    run(
        [
            sys.executable,
            "-m",
            "twine",
            "upload",
            "--config-file",
            str(PYPIRC),
            *artifacts(version),
        ]
    )
    print(f"已上传 https://pypi.org/project/{name}/{version}/")

    tag = f"v{version}"
    if not args.skip_tag:
        git("add", str(INIT))
        if (
            subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode
            != 0
        ):
            git("commit", "-m", f"release: {version}")
        git("tag", tag)

    if args.push:
        git("push", "origin", "HEAD")
        if not args.skip_tag:
            git("push", "origin", tag)
        print("已推送 main 与 tag")

    payload = json.dumps(
        {"version": version, "files": artifacts(version)}, ensure_ascii=False
    )
    print(payload)


if __name__ == "__main__":
    main()
