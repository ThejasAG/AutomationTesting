"""Compatibility package for commands launched from the automation/ folder.

The real ``automation`` package is the parent directory. When a command is run
from inside that directory, Python looks for ``automation`` one level too deep.
Extending ``__path__`` keeps imports like ``automation.api.main`` working.
"""

from pathlib import Path

_REAL_PACKAGE = Path(__file__).resolve().parent.parent
__path__.append(str(_REAL_PACKAGE))
