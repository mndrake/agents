"""chmc - a pure-Python compiler for Microsoft Compiled HTML Help (.chm) files."""

__version__ = "1.0.0"

from .project import compile_project, load_folder, load_hhp, load_project  # noqa: E402

__all__ = ["compile_project", "load_folder", "load_hhp", "load_project", "__version__"]
