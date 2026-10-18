#!/usr/bin/env python3
"""Apply this delivery patch without resetting or overwriting unrelated work.

Default is preflight only. Exact baseline files are replaced atomically;
diverged files are merged only when every old hunk has one exact match.
Any conflict stops the ENTIRE application before touching project files.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC,datetime
from pathlib import Path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def confined(root: Path,relative: str) -> Path:
    path = (root/relative).resolve()
    if not path.is_relative_to(root.resolve()) or Path(relative).is_absolute():
        raise ValueError(f"PATH_OUTSIDE_ROOT:{relative}")
    return path


def merge_exact_context(base: bytes,desired: bytes,current: bytes) -> bytes:
    old,new,live = (data.decode("utf-8").splitlines(keepends=True)
                    for data in (base,desired,current))
    diff = list(difflib.unified_diff(old,new,n=4))
    hunks = []
    for line in diff[2:]:
        if line.startswith("@@"):
            hunks.append([])
        elif hunks:
            hunks[-1].append(line)
    cursor = 0
    for hunk in hunks:
        before = [line[1:] for line in hunk if line[:1] in (" ","-")]
        after = [line[1:] for line in hunk if line[:1] in (" ","+")]
        if not before:
            raise ValueError("DIVERGED_INSERTION_WITHOUT_CONTEXT")
        matches = [i for i in range(cursor,len(live)-len(before)+1)
                   if live[i:i+len(before)] == before]
        if len(matches) != 1:
            raise ValueError(f"EXACT_CONTEXT_CONFLICT:matches={len(matches)}")
        index = matches[0]
        live[index:index+len(before)] = after
        cursor = index+len(after)
    return "".join(live).encode("utf-8")


def contains_delta(base: bytes,desired: bytes,current: bytes) -> bool:
    old,new,live = (data.decode("utf-8").splitlines(keepends=True)
                    for data in (base,desired,current))
    diff = list(difflib.unified_diff(old,new,n=4))
    hunks = []
    for line in diff[2:]:
        if line.startswith("@@"):
            hunks.append([])
        elif hunks and line[:1] in (" ","+"):
            hunks[-1].append(line[1:])
    cursor = 0
    for block in hunks:
        if not block:
            return False
        matches = [i for i in range(cursor,len(live)-len(block)+1)
                   if live[i:i+len(block)] == block]
        if len(matches) != 1:
            return False
        cursor = matches[0]+len(block)
    return bool(hunks)


def atomic_write(path: Path,data: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary = tempfile.mkstemp(dir=path.parent,prefix=".delivery-patch-")
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary,path.stat().st_mode & 0o777)
        os.replace(temporary,path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def apply_patch(package: Path,repo: Path,*,apply: bool=False) -> dict:
    manifest = json.loads((package/"patch_manifest.json").read_text())
    prepared,conflicts,report = [],[],[]
    for item in manifest["files"]:
        relative = item["path"]
        # The payload allowlist contains only source/tests/tools/harness.
        if not relative.startswith(("src/","tests/","tools/","harness/")):
            raise ValueError(f"PAYLOAD_PATH_FORBIDDEN:{relative}")
        path = confined(repo,relative)
        desired = confined(package/"payload",relative).read_bytes()
        if sha(desired) != item["new_sha256"]:
            raise ValueError(f"PAYLOAD_HASH_MISMATCH:{relative}")
        base_path = confined(package/"baseline",relative)
        base = base_path.read_bytes() if item.get("base_sha256") else None
        if base is not None and sha(base) != item["base_sha256"]:
            raise ValueError(f"BASELINE_HASH_MISMATCH:{relative}")
        current = path.read_bytes() if path.is_file() else None
        try:
            if current == desired:
                mode,result = "already_applied",desired
            elif base is None:
                if current is not None:
                    raise ValueError("NEW_FILE_ALREADY_EXISTS_WITH_DIFFERENT_CONTENT")
                mode,result = "create",desired
            elif current is None:
                raise ValueError("BASE_FILE_MISSING")
            elif current == base:
                mode,result = "replace_exact_baseline",desired
            elif base and current.startswith(base):
                mode,result = "merge_append_only_changes",desired+current[len(base):]
            elif desired and current.startswith(desired):
                mode,result = "already_applied_with_local_changes",current
            elif contains_delta(base,desired,current):
                mode,result = "already_applied_with_local_changes",current
            else:
                mode,result = "merge_preserving_local_changes",merge_exact_context(base,desired,current)
        except (ValueError,UnicodeError) as error:
            conflicts.append({"path":relative,"error":str(error)})
            continue
        report.append({"path":relative,"action":mode,"result_sha256":sha(result),
                       "current_sha256":sha(current) if current is not None else None})
        if not mode.startswith("already_applied"):
            prepared.append((path,current,result))
    result = {"status":"conflicts" if conflicts else "preflight_ok","applied":False,
              "repo":str(repo),"files":report,"conflicts":conflicts}
    if conflicts or not apply:
        return result
    # Check no file changed between preflight and the write transaction.
    for path,original,_ in prepared:
        current = path.read_bytes() if path.is_file() else None
        if current != original:
            raise ValueError(f"CONCURRENT_FILE_CHANGE:{path}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = repo/".delivery_patch_backups"/stamp
    for path,original,_ in prepared:
        if original is not None:
            atomic_write(backup/path.relative_to(repo),original)
    changed = []
    try:
        for path,original,new in prepared:
            atomic_write(path,new)
            changed.append((path,original))
        for path,_,new in prepared:
            if path.read_bytes() != new:
                raise ValueError(f"WRITE_VERIFICATION_FAILED:{path}")
    except BaseException:
        for path,original in reversed(changed):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path,original)
        raise
    result.update({"status":"applied","applied":True,"backup":str(backup),
                   "changed_files":len(changed)})
    atomic_write(backup/"installation_report.json",json.dumps(result,ensure_ascii=False,indent=2).encode())
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo",type=Path,required=True)
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--report",type=Path)
    args = parser.parse_args()
    repo = args.repo.expanduser().resolve()
    if not (repo/"src/slides_cli/gpn/restyle_runner.py").is_file():
        parser.error("--repo must point at the gpn-restyler code directory")
    try:
        with (repo/".delivery_patch.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result = apply_patch(Path(__file__).resolve().parent,repo,apply=args.apply)
        if args.report:
            atomic_write(args.report,json.dumps(result,ensure_ascii=False,indent=2).encode())
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result["conflicts"] else 0
    except (OSError,ValueError) as error:
        print(json.dumps({"status":"installer_error","applied":False,"error":str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
