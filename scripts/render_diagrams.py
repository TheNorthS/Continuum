"""Render repository-native architecture diagrams with a local Graphviz installation."""

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("architecture", "qa-flow", "context-compaction", "memory-lifecycle")


def main():
    parser = argparse.ArgumentParser(
        description="Render README SVG/PNG diagrams with Graphviz"
    )
    parser.add_argument(
        "--font", default="Microsoft YaHei", help="Installed CJK font name"
    )
    parser.add_argument("--dpi", type=int, default=160, help="PNG resolution only")
    args = parser.parse_args()
    executable = shutil.which("dot")
    if not executable:
        parser.error("Graphviz dot was not found in PATH")
    if not 72 <= args.dpi <= 300:
        parser.error("dpi must be between 72 and 300")
    directory = ROOT / "docs/assets/diagrams"
    for name in NAMES:
        for extension in ("svg", "png"):
            # SVG uses Graphviz's 72-point coordinate system. Higher DPI can
            # scale its graph without expanding the viewBox, clipping content.
            dpi = 72 if extension == "svg" else args.dpi
            subprocess.run(
                [
                    executable,
                    f"-T{extension}",
                    f"-Gdpi={dpi}",
                    f"-Gfontname={args.font}",
                    f"-Nfontname={args.font}",
                    f"-Efontname={args.font}",
                    str(directory / f"{name}.dot"),
                    "-o",
                    str(directory / f"{name}.{extension}"),
                ],
                check=True,
            )
        print(name + ": SVG + PNG rendered")


if __name__ == "__main__":
    main()
