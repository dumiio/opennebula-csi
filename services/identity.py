"""
Implement the Identity service for the CSI plugin
"""
import logging

from pb import csi_pb2
from pb import csi_pb2_grpc

import constant

logger = logging.getLogger("IdentityService")


class IdentityServicer(csi_pb2_grpc.IdentityServicer):
    """
    Implements IndentityService from the CSI spec
    """

    def __int__(self, ready: bool = False):
        self._ready = ready

    def set_ready(self, value: bool) -> None:
        """
        Sets the ready state of the Node
        :param value: Whether Node is ready or not
        :type value: bool
        """
        self._ready = value

    def GetPluginInfo(self, request, context):
        return csi_pb2.GetPluginInfoResponse(
            name=constant.CSI_PLUGIN_NAME,
            vendor_version=constant.CSI_PLUGIN_VERSION,
        )

    def GetPluginCapabilities(self, request, context):
        response = csi_pb2.GetPluginCapabilitiesResponse()

        controller_capability = response.capabilities.add()
        controller_capability.service.type = (
            controller_capability.Service.CONTROLLER_SERVICE
        )

        return response

    def Probe(self, request, context):
        logger.debug("Probing")
        response = csi_pb2.ProbeResponse()
        response.ready.value = self._ready
        return response
