"""Command line entry point: python -m codev_mcp --backend simulated."""

from __future__ import annotations

import argparse
import os

from . import __version__
from .backend import BACKEND_NAMES
from .client import DEFAULT_TIMEOUT_SECONDS
from .server import build_server, default_working_directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codev-mcp",
        description="通过 COM 接口把本机 CODE V 10.2 接入 MCP 客户端。",
    )
    parser.add_argument(
        "--backend",
        choices=BACKEND_NAMES,
        default=os.environ.get("CODEV_MCP_BACKEND", "simulated"),
        help="simulated 用于自动化测试，com 驱动真实 CODE V 会话。",
    )
    parser.add_argument(
        "--working-directory",
        default=os.environ.get("CODEV_MCP_WORKDIR") or default_working_directory(),
        help="服务自有工作目录，CODE V 会话在其中运行。",
    )
    parser.add_argument(
        "--python",
        dest="python_executable",
        default=os.environ.get("CODEV_MCP_PYTHON") or None,
        help="运行工作进程的解释器，默认与当前进程相同。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("CODEV_MCP_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
        help="单次工具调用的超时秒数。",
    )
    parser.add_argument("--version", action="version", version=f"codev-mcp {__version__}")
    args = parser.parse_args(argv)

    server = build_server(
        args.backend,
        working_directory=args.working_directory,
        python_executable=args.python_executable,
        timeout=args.timeout,
    )
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

