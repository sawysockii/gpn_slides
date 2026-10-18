"""Reproduce tests against the PROVIDED PARTIAL SOURCE SNAPSHOT.

The archive omits upstream slides_cli/api.py and other unchanged modules.
Namespace bootstrap avoids importing their unavailable __init__; it does NOT
replace any gpn implementation. Do not use this as the production CLI.
On the complete Mac checkout use normal uv/pytest commands in LAUNCH_PROMPT.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import platform
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


class Tee:
    def __init__(self,*streams):
        self.streams = streams
    def write(self,value):
        for stream in self.streams:
            stream.write(value)
    def flush(self):
        for stream in self.streams:
            stream.flush()
    def isatty(self):
        return False
    def __getattr__(self,name):
        return getattr(self.streams[0],name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout",type=Path,required=True)
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    evidence = Path(__file__).resolve().parent
    os.chdir(checkout)
    namespace = types.ModuleType("slides_cli")
    namespace.__path__ = [str(checkout/"src/slides_cli")]
    sys.modules["slides_cli"] = namespace
    modules = ["test_delivery_patch.py","test_delivery_dispatch.py","test_delivery_native.py",
        "test_runner_reissue_idempotence.py","test_stage7_runner.py",
        "test_stage7_acceptance.py","test_stage7_design.py","test_stage7_semantics.py"]
    command = ["-q","-ra","-c","/dev/null",*(str(checkout/"tests/gpn"/m) for m in modules),
        str(evidence/"test_installer.py"),f"--junitxml={evidence/'pytest.xml'}",
        f"--basetemp={checkout.parent/'verified_test_artifacts'}"]
    (evidence/"pytest.xml").unlink(missing_ok=True)
    with (evidence/"pytest.log").open("w") as stream:
        with contextlib.redirect_stdout(Tee(sys.stdout,stream)):
            code = pytest.main(command)
    tree = ET.parse(evidence/"pytest.xml")
    suites = tree.findall(".//testsuite")
    report = {"scope":"provided-source snapshot; not full Mac repo or corporate corpus",
        "checkout":str(checkout),"command":command,"exit_code":int(code),
        "tests":sum(int(s.get("tests",0)) for s in suites),
        "failures":sum(int(s.get("failures",0)) for s in suites),
        "errors":sum(int(s.get("errors",0)) for s in suites),
        "skipped":sum(int(s.get("skipped",0)) for s in suites),
        "python":platform.python_version(),"platform":platform.platform(),
        "dependencies":{name:importlib.metadata.version(name) for name in (
            "ortools","pytest","python-pptx","pydantic","lxml")},
        "corporate_three_case_conversion_executed_here":False,
        "real_chromium_measurement_executed_here":False,
        "powerpoint_ui_verified_here":False}
    report["passed"] = report["tests"]-report["failures"]-report["errors"]-report["skipped"]
    (evidence/"verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
