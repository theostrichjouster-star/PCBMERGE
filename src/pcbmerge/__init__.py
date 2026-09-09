"""pcbmerge - combine several EAGLE designs into one schematic and board."""

from .eagle import EagleDoc, EagleError
from .merge import Design, MergeReport, build_resolver, load_designs, merge
from .linking import Suggestion, suggest
from .nets import Action, Kind, NetResolver
from .plan import ConnectDecision, DesignSpec, InstanceSpec, MergePlan, expand
from .pruning import DropRule, PartGroup, PartRef, catalog

__version__ = "0.3.0"

__all__ = [
    "Action", "ConnectDecision", "Design", "DesignSpec", "DropRule", "EagleDoc",
    "EagleError", "InstanceSpec", "Kind", "MergePlan", "MergeReport",
    "NetResolver", "PartGroup", "PartRef", "Suggestion", "build_resolver",
    "catalog", "expand", "load_designs", "merge", "suggest", "__version__",
]
