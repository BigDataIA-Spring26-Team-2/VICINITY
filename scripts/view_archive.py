"""Interactive S3 archive browser for Vicinity.

Navigate the bucket like a file manager. Pick a number, enter a
simple action, no flags to memorize.

Usage (from project root):

    python -m scripts.view_archive

Folder navigation:
    <number>  open folder / open file menu
    ..        go up one level
    r         refresh
    q         quit

File menu actions:
    1   Head 3 records
    2   Head N records (prompts for N)
    3   View full file
    4   Grep field=value (prompts)
    5   Save to ./s3_downloads/ (prompts for decompress)
    b   Back
    q   Quit
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app.config import get_settings


# ── Colors ──────────────────────────────────────────────────────────

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "cyan": "\033[36m", "gray": "\033[90m",
}


def c(color: str, text) -> str:
    return f"{C.get(color, '')}{text}{C['reset']}"


def _clear():
    print("\033[2J\033[H", end="")


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ── S3 helpers ──────────────────────────────────────────────────────

def _client():
    import boto3
    s = get_settings()
    if not s.s3_bucket:
        print(c("red", "S3_BUCKET not configured in .env"))
        sys.exit(1)
    return boto3.client("s3", region_name=s.aws_region), s.s3_bucket


def _list_folder(client, bucket: str, prefix: str) -> tuple[list, list]:
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"
    paginator = client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")

    folders, files = [], []
    for page in pages:
        for common in page.get("CommonPrefixes", []) or []:
            folders.append(common["Prefix"])
        for obj in page.get("Contents", []) or []:
            if obj["Key"] == prefix:
                continue
            files.append({
                "key":   obj["Key"],
                "size":  obj["Size"],
                "mtime": obj["LastModified"],
            })
    return sorted(folders), sorted(files, key=lambda f: f["key"])


def _read_bytes(client, bucket: str, key: str) -> bytes:
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


def _decompress(data: bytes, key: str) -> bytes:
    return gzip.decompress(data) if key.endswith(".gz") else data


# ── UI ──────────────────────────────────────────────────────────────

def _print_header(bucket: str, prefix: str):
    _clear()
    print(c("bold", "  Vicinity S3 Archive Browser"))
    print(c("dim",  f"  s3://{bucket}/{prefix or ''}"))
    print()


def _render_folder(folders: list, files: list) -> list:
    items = [("..", "up", None, None)]
    for f in folders:
        name = f.rstrip("/").split("/")[-1]
        items.append((name, "folder", f, None))
    for f in files:
        name = f["key"].split("/")[-1]
        items.append((name, "file", f["key"], f))

    name_width = min(max((len(n) for n, _, _, _ in items), default=20), 50)

    for i, (name, kind, _, meta) in enumerate(items):
        num = "[..]" if kind == "up" else f"[{i:>2}]"
        display = name if len(name) <= name_width else name[:name_width - 1] + "..."

        if kind == "up":
            print(f"  {c('dim', num)}  {c('dim', '(go up)')}")
        elif kind == "folder":
            print(f"  {c('cyan', num)}  {c('blue', display.ljust(name_width))}  {c('dim', 'folder')}")
        else:
            size = _human_size(meta["size"])
            ts = meta["mtime"].strftime("%Y-%m-%d %H:%M")
            print(f"  {c('cyan', num)}  {display.ljust(name_width)}  "
                  f"{c('yellow', size.rjust(8))}  {c('gray', ts)}")

    return items


def _print_file_menu(key: str, size: int):
    _clear()
    print(c("bold", "  Vicinity S3 Archive Browser"))
    print(c("dim",  f"  File: {key}"))
    print(c("dim",  f"  Size: {_human_size(size)}"))
    print()
    print("  What would you like to do?")
    print()
    print(f"    {c('cyan', '1')}  Head   - first 3 records")
    print(f"    {c('cyan', '2')}  Head N - first N records")
    print(f"    {c('cyan', '3')}  View   - full file")
    print(f"    {c('cyan', '4')}  Grep   - filter by field=value")
    print(f"    {c('cyan', '5')}  Save   - download locally")
    print(f"    {c('cyan', 'b')}  Back")
    print(f"    {c('cyan', 'q')}  Quit")
    print()


# ── File actions ────────────────────────────────────────────────────

def _show_records(raw: bytes, key: str, limit: Optional[int]):
    data = _decompress(raw, key)
    lines = data.decode("utf-8", errors="replace").splitlines()

    print()
    print(c("dim", f"  {_human_size(len(data))} decompressed, {len(lines)} lines"))
    print()

    shown = 0
    for i, line in enumerate(lines):
        if limit is not None and shown >= limit:
            break
        try:
            obj = json.loads(line)
            print(c("bold", f"-- record {i + 1} --"))
            print(json.dumps(obj, indent=2, default=str))
            print()
            shown += 1
        except json.JSONDecodeError:
            print(data.decode("utf-8", errors="replace")[:10000])
            return

    if limit is not None and len(lines) > shown:
        print(c("dim", f"  ...and {len(lines) - shown} more records"))


def _grep_records(raw: bytes, key: str, field: str, value: str):
    data = _decompress(raw, key)
    print()
    matches = 0
    total = 0
    v_lower = value.lower()
    for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines()):
        total += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_val = obj.get(field)
        if raw_val is None:
            continue
        if v_lower in str(raw_val).lower():
            print(c("bold", f"-- match {matches + 1} (line {i + 1}) --"))
            print(json.dumps(obj, indent=2, default=str))
            print()
            matches += 1
            if matches >= 20:
                print(c("dim", "  ...hit limit 20"))
                break
    print(c("bold", f"  {matches} matches across {total} records"))


def _save_file(raw: bytes, key: str, decompress: bool):
    out_root = Path("s3_downloads")
    out_key = key[:-3] if decompress and key.endswith(".gz") else key
    data = _decompress(raw, key) if decompress else raw

    out_path = out_root / out_key
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    print()
    print(c("green", f"  Saved: {out_path}"))
    print(c("dim",   f"  Size:  {_human_size(len(data))}"))


# ── Prompts ─────────────────────────────────────────────────────────

def _pause():
    print()
    try:
        input(c("dim", "  Press Enter to continue..."))
    except (EOFError, KeyboardInterrupt):
        sys.exit(0)


def _prompt(msg: str) -> str:
    try:
        return input(c("cyan", f"  {msg} ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


# ── File-level loop ─────────────────────────────────────────────────

def _handle_file(client, bucket: str, key: str, size: int):
    raw: bytes | None = None

    def _ensure_loaded():
        nonlocal raw
        if raw is None:
            print(c("dim", f"  Fetching {_human_size(size)}..."))
            raw = _read_bytes(client, bucket, key)

    while True:
        _print_file_menu(key, size)
        choice = _prompt("Choice:").lower()

        if choice in ("b", ""):
            return
        if choice == "q":
            sys.exit(0)

        try:
            _ensure_loaded()
        except Exception as e:
            print(c("red", f"  Failed to fetch: {e}"))
            _pause()
            return

        if choice == "1":
            _show_records(raw, key, limit=3)
            _pause()
        elif choice == "2":
            n_str = _prompt("How many records?")
            try:
                n = int(n_str)
            except ValueError:
                print(c("red", "  Invalid number"))
                _pause()
                continue
            _show_records(raw, key, limit=n)
            _pause()
        elif choice == "3":
            _show_records(raw, key, limit=None)
            _pause()
        elif choice == "4":
            filt = _prompt("Filter (field=value):")
            if "=" not in filt:
                print(c("red", "  Must be field=value"))
                _pause()
                continue
            field, value = filt.split("=", 1)
            _grep_records(raw, key, field.strip(), value.strip())
            _pause()
        elif choice == "5":
            if key.endswith(".gz"):
                dec = _prompt("Decompress on save? [y/N]").lower()
                decompress = dec in ("y", "yes")
            else:
                decompress = False
            _save_file(raw, key, decompress)
            _pause()
        else:
            print(c("red", "  Unknown choice"))
            _pause()


# ── Folder-level loop ───────────────────────────────────────────────

def main():
    client, bucket = _client()
    prefix = "pipelines/"

    while True:
        _print_header(bucket, prefix)

        try:
            folders, files = _list_folder(client, bucket, prefix)
        except Exception as e:
            print(c("red", f"  Failed to list: {e}"))
            sys.exit(1)

        if not folders and not files:
            print(c("dim", "  (empty folder)"))

        items = _render_folder(folders, files)

        print()
        print(c("dim", "  Enter number to open, .. for up, r refresh, q quit"))
        choice = _prompt("Choice:").lower()

        if choice in ("q", "quit", "exit"):
            return
        if choice in ("r", ""):
            continue
        if choice == "..":
            parts = prefix.rstrip("/").split("/")
            prefix = "/".join(parts[:-1]) + "/" if len(parts) > 1 else ""
            continue

        try:
            idx = int(choice)
        except ValueError:
            print(c("red", "  Invalid choice"))
            _pause()
            continue

        if idx < 0 or idx >= len(items):
            print(c("red", "  Out of range"))
            _pause()
            continue

        _, kind, target, meta = items[idx]
        if kind == "up":
            parts = prefix.rstrip("/").split("/")
            prefix = "/".join(parts[:-1]) + "/" if len(parts) > 1 else ""
        elif kind == "folder":
            prefix = target
        elif kind == "file":
            _handle_file(client, bucket, target, meta["size"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()