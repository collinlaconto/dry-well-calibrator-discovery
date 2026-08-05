"""Import the flat application modules through their runtime package name."""

from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]


def bootstrap_calsuite():
    """Expose the repository root as the ``calsuite`` package."""
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    package = sys.modules.get("calsuite")
    if package is None:
        package = types.ModuleType("calsuite")
        package.__path__ = [root]
        package.__version__ = "test"
        sys.modules["calsuite"] = package
    return package


bootstrap_calsuite()
