"""在本地复现 GitHub Actions CI（.github/workflows/ci.yml）的全部检查。

依次执行与 CI 相同的命令，任一步失败即停止并返回非 0 退出码：

1. ``uv sync --dev``                —— 三个 job 都先做的依赖安装
2. ``uv run ruff check .``          —— Lint & Format
3. ``uv run ruff format --check .`` —— Lint & Format
4. ``uv run pyright``               —— Type check
5. ``uv run pytest``                —— Tests（按 pyproject.toml 的 addopts 输出覆盖率）

用法::

    uv run python scripts/ci_check.py
    uv run python scripts/ci_check.py --skip-sync --skip-tests
"""

import argparse
import io
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Step:
    """一个待执行的检查步骤，command 即 CI 中对应的命令"""

    name: str
    command: list[str]


def _build_steps(*, skip_sync: bool, skip_tests: bool) -> list[Step]:
    steps: list[Step] = []
    if not skip_sync:
        steps.append(Step("uv sync --dev", ["uv", "sync", "--dev"]))
    steps.append(Step("uv run ruff check .", ["uv", "run", "ruff", "check", "."]))
    steps.append(Step("uv run ruff format --check .", ["uv", "run", "ruff", "format", "--check", "."]))
    steps.append(Step("uv run pyright", ["uv", "run", "pyright"]))
    if not skip_tests:
        steps.append(Step("uv run pytest", ["uv", "run", "pytest"]))
    return steps


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在本地复现 CI 检查（lint / 格式 / 类型检查 / 测试）")
    parser.add_argument("--skip-sync", action="store_true", help="跳过 uv sync --dev（依赖已安装或离线时）")
    parser.add_argument("--skip-tests", action="store_true", help="跳过 pytest，只跑 lint 与类型检查")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # 固定 UTF-8 输出，避免 Windows 上管道/重定向时中文按本地代码页编码
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    args = _parse_args(argv)
    skip_sync = bool(args.skip_sync)
    skip_tests = bool(args.skip_tests)

    if shutil.which("uv") is None:
        print("未找到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/", file=sys.stderr)
        return 1

    steps = _build_steps(skip_sync=skip_sync, skip_tests=skip_tests)
    results: list[tuple[Step, int, float]] = []

    for step in steps:
        print(f"\n==> {step.name}", flush=True)
        started = time.monotonic()
        exit_code = subprocess.run(step.command, cwd=REPO_ROOT, check=False).returncode
        elapsed_seconds = time.monotonic() - started
        results.append((step, exit_code, elapsed_seconds))

        if exit_code != 0:
            print(f"步骤失败：{step.name}（exit {exit_code}），已停止后续检查", file=sys.stderr)
            break

    print("\n===== 本地 CI 检查结果 =====")
    for step, exit_code, elapsed_seconds in results:
        status = "PASS" if exit_code == 0 else "FAIL"
        print(f"[{status}] {step.name} ({elapsed_seconds:.0f}s)")

    if len(results) != len(steps) or any(exit_code != 0 for _, exit_code, _ in results):
        print("CI 检查未通过，请按上方输出修复后重试", file=sys.stderr)
        return 1

    print("全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
