"""按模块隔离运行全部测试。

为什么需要它：PySide6 在解释器退出时，若仍有顶层 QWidget 存活，析构顺序不当可能
段错误（所有断言都已跑完、结果已打印之后才发生）。单进程跑全量套件会让这个
退出期崩溃污染整体退出码。测试清理已调整 Qt 退出顺序；这里仍保留每个模块一个
子进程的隔离，并严格检查输出及退出码：

* 退出码为 0、输出含独立的 ``OK`` 行且不含 ``FAILED``/``ERROR:`` → 通过；
* 否则 → 失败，并打印该模块的输出尾部。

用法::

    python tests/run_all.py            # 全部模块
    python tests/run_all.py test_parser test_tree   # 指定模块
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def discover_modules() -> list:
    names = []
    for name in sorted(os.listdir(HERE)):
        if name.startswith("test_") and name.endswith(".py"):
            names.append(name[:-3])
    return names


def run_module(name: str) -> tuple:
    env = dict(os.environ)
    env.setdefault("JSONWB_HEADLESS", "1")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", f"tests.{name}", "-v"],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180)
    except subprocess.TimeoutExpired as exc:
        chunks = [chunk.decode(errors="replace") if isinstance(chunk, bytes) else chunk
                  for chunk in (exc.stdout or "", exc.stderr or "")]
        output = "".join(chunks)
        return False, "TIMEOUT", time.time() - started, -1, output
    elapsed = time.time() - started
    output = (proc.stdout or "") + (proc.stderr or "")
    passed = (proc.returncode == 0 and re.search(r"^OK(?: \(.*\))?$", output, re.MULTILINE)
              is not None and "FAILED" not in output and "ERROR:" not in output)
    counts = ""
    for line in output.splitlines():
        if line.startswith("Ran "):
            counts = line
            break
    return passed, counts, elapsed, proc.returncode, output


def main(argv: list) -> int:
    modules = argv[1:] or discover_modules()
    failures = []
    total_tests = 0
    for name in modules:
        passed, counts, elapsed, rc, output = run_module(name)
        try:
            total_tests += int(counts.split()[1])
        except (IndexError, ValueError):
            pass
        flag = "PASS" if passed else "FAIL"
        extra = "" if passed or rc == 0 else f" (exit={rc}, teardown crash?)"
        print(f"[{flag}] {name:24s} {counts:28s} {elapsed:5.1f}s{extra}", flush=True)
        if not passed:
            failures.append(name)
            tail = "\n".join(output.splitlines()[-40:])
            print(tail, flush=True)
    print("-" * 60)
    print(f"modules: {len(modules) - len(failures)}/{len(modules)} passed, "
          f"tests: {total_tests}")
    if failures:
        print("failed modules:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
