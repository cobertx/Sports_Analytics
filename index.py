"""Launch the dashboard with: python index.py"""

from pathlib import Path
import subprocess
import sys


# IDE Run buttons may use system Python even when .venv is installed.
# Switch before importing the app or any third-party dependencies.
if __name__ == "__main__":
    project_dir = Path(__file__).resolve().parent
    environment_dir = project_dir / ".venv"
    project_python = environment_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if project_python.is_file() and Path(sys.prefix).resolve() != environment_dir.resolve():
        try:
            raise SystemExit(subprocess.call([str(project_python), str(Path(__file__).resolve()), *sys.argv[1:]]))
        except KeyboardInterrupt:
            raise SystemExit(130) from None

from app import app, main, server


if __name__ == "__main__":
    main()
