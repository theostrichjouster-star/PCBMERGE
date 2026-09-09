"""pcbmerge - combine several EAGLE designs into one schematic and board."""

from .eagle import EagleDoc, EagleError
from .merge import Design, MergeReport, build_resolver, load_designs, merge
from .nets import Action, Kind, NetResolver
from .plan import DesignSpec, MergePlan

__version__ = "0.1.0"

__all__ = [
    "Action", "Design", "DesignSpec", "EagleDoc", "EagleError", "Kind",
    "MergePlan", "MergeReport", "NetResolver", "build_resolver",
    "load_designs", "merge", "__version__",
]
