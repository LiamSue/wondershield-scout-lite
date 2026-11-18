"""Experimental synthetic-only Scout Lite Developer Preview. Not production authorization."""
__version__ = "0.1.0.dev2+internalxlsx1"
from .scout_lite import Engine
from .sources import Fault
__all__ = ["Engine", "Fault", "__version__"]

from .task_contract import evaluate_task
