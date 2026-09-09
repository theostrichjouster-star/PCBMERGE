"""pcbmerge - combine several EAGLE designs into one schematic and board."""

from .eagle import EagleDoc, EagleError
from .merge import Design, MergeReport, build_resolver, load_designs, merge
from .linking import Suggestion, suggest
from .nets import Action, Kind, NetResolver
from .plan import DesignSpec, InstanceSpec, MergePlan, expand

__version__ = "0.2.0"

__all__ = [
    "Action", "Design", "DesignSpec", "EagleDoc", "EagleError", "InstanceSpec",
    "Kind", "MergePlan", "MergeReport", "NetResolver", "Suggestion",
    "build_resolver", "expand", "load_designs", "merge", "suggest",
    "__version__",
]
