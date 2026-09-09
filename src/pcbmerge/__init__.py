"""pcbmerge - combine several PCB designs into one schematic and board.

Reads EAGLE and KiCad; writes EAGLE.
"""

from .eagle import EagleDoc, EagleError
from .merge import Design, MergeReport, build_resolver, load_designs, merge
from .kicad import KiCadError, convert as convert_kicad
from .linking import Suggestion, suggest
from .nets import Action, Kind, NetResolver
from .plan import ConnectDecision, DesignSpec, InstanceSpec, MergePlan, expand
from .pruning import DropRule, PartGroup, PartRef, catalog

__version__ = "0.6.0"

__all__ = [
    "Action", "ConnectDecision", "Design", "DesignSpec", "DropRule", "EagleDoc",
    "EagleError", "InstanceSpec", "Kind", "KiCadError", "MergePlan", "MergeReport",
    "NetResolver", "PartGroup", "PartRef", "Suggestion", "build_resolver",
    "catalog", "convert_kicad", "expand", "load_designs", "merge", "suggest",
    "__version__",
]
