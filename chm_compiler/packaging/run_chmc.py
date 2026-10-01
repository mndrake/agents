"""Entry script for the standalone executables (see packaging/build.sh).

PyInstaller needs a top-level script; chmc/__main__.py uses relative
imports and can't serve as one.
"""

import sys

from chmc.cli import main

sys.exit(main())
