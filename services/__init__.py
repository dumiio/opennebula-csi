"""
Contains all the services the driver must implement via gRPC
"""

from . import identity
from . import controller
from . import node

IdentityServicer = identity.IdentityServicer
ControllerServicer = controller.ControllerServicer
NodeServicer = node.NodeServicer
