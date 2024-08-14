"""
Implement the ControllerService of the CSI spec
"""
import logging
import re

from math import ceil
from time import sleep
from typing import Callable

import pyone

from grpc_interceptor.exceptions import (
    NotFound,
    Internal,
    InvalidArgument,
    FailedPrecondition,
    ResourceExhausted,
    OutOfRange,
    AlreadyExists,
)

from pb import csi_pb2
from pb import csi_pb2_grpc

import constant

logger = logging.getLogger("ControllerService")


class ControllerServicer(csi_pb2_grpc.ControllerServicer):
    """
    Implement the ControllerService as a gRPC Servicer
    """

    def __init__(self, one_api_endpoint: str, one_api_auth: str, my_vm_id: int):
        logger.debug(
            "Connection to StorPool API at %s with token %s",
            one_api_endpoint,
            one_api_auth,
        )
        self._one_api = pyone.OneServer(one_api_endpoint, one_api_auth)
        self._my_vm_id = my_vm_id

    def ControllerGetCapabilities(self, request, context):
        response = csi_pb2.ControllerGetCapabilitiesResponse()

        create_delete_volume_cap = response.capabilities.add()
        create_delete_volume_cap.rpc.type = (
            create_delete_volume_cap.RPC.CREATE_DELETE_VOLUME
        )

        publish_unpublish_volume_cap = response.capabilities.add()
        publish_unpublish_volume_cap.rpc.type = (
            publish_unpublish_volume_cap.RPC.PUBLISH_UNPUBLISH_VOLUME
        )

        publish_readonly_cap = response.capabilities.add()
        publish_readonly_cap.rpc.type = (
            publish_readonly_cap.RPC.PUBLISH_READONLY
        )

        expand_volume_cap = response.capabilities.add()
        expand_volume_cap.rpc.type = (
            publish_readonly_cap.RPC.EXPAND_VOLUME
        )

        return response

    def CreateVolume(self, request, context):
        if not request.name:
            raise InvalidArgument("Missing volume name")

        if not request.volume_capabilities:
            raise InvalidArgument("Missing volume capabilities")

        datastore_id = 0

        if request.parameters["datastore_id"] is None:
            logger.warning("OpenNebula datastore ID was not explicitly specified, falling back to datastore ID 0")
        else:
            datastore_id = int(request.parameters["datastore_id"])

        volume_size = self._determine_volume_size(request.capacity_range)

        logger.info(f"Provisioning volume {request.name} (datastore id: {datastore_id}, size: {volume_size} MB)")

        for requested_capability in request.volume_capabilities:
            if requested_capability.WhichOneof("access_type") == "mount":
                if (requested_capability.access_mode.mode
                        != requested_capability.AccessMode.SINGLE_NODE_WRITER
                        and requested_capability.access_mode.mode
                        != requested_capability.AccessMode.SINGLE_NODE_READER_ONLY):
                    raise InvalidArgument(f"Requested unsupported access mode: {requested_capability.access_mode.mode}")
            else:
                raise InvalidArgument("Requested unsupported block access mode")

        try:
            for image in self._one_api.imagepool.info(-2, -1, -1).IMAGE:
                if image.get_NAME() == request.name:
                    if image.get_SIZE() == volume_size:
                        return self._build_create_volume_response(str(image.get_ID()), volume_size)
                    else:
                        raise AlreadyExists(f"PVC {request.name} already exists as image {image.get_ID()} but its size "
                                            f"({image.get_SIZE()} MB) differs from the requested ({volume_size} MB)")

            datablock_image_id = self._one_api.image.allocate(
                {"NAME": request.name,
                 "TYPE": "DATABLOCK",
                 "PERSISTENT": "YES",
                 "SIZE": volume_size},
                datastore_id)

            return self._build_create_volume_response(str(datablock_image_id), volume_size * (1024 ** 2))
        except pyone.OneActionException as error:
            if "Not enough space in datastore" in str(error):
                raise OutOfRange(str(error))
        except pyone.OneException as error:
            logger.error(f"OpenNebula API error {str(error)}")
            raise Internal(str(error))

    def DeleteVolume(self, request, context):
        if not request.volume_id:
            raise InvalidArgument("Missing volume id")

        logger.info(f"Deleting volume {request.volume_id}")

        try:
            self._one_api.image.delete(int(request.volume_id))
            logger.debug(f"Successfully deleted volume {request.volume_id}")
        except pyone.OneNoExistsException:
            logger.debug(f"Tried to delete an non-existing volume: {request.volume_id}")
        except pyone.OneActionException as error:
            if "VMs using it" in str(error):
                raise FailedPrecondition(str(error))
        except pyone.OneException as error:
            raise Internal(str(error))

        return csi_pb2.DeleteVolumeResponse()

    def ValidateVolumeCapabilities(self, request, context):
        response = csi_pb2.ValidateVolumeCapabilitiesResponse()

        if not request.volume_id:
            raise InvalidArgument("Missing volume Id")

        if not request.volume_capabilities:
            raise InvalidArgument("Missing volume capabilities")

        try:
            image = self._one_api.image.info(int(request.volume_id))
        except pyone.OneNoExistsException:
            logger.error(
                f"Cannot validate volume with image ID {request.volume_id} because it doesn't exist."
            )
            raise NotFound(
                f"OpenNebula image ID {request.volume_id} does not exist."
            )

        if image.get_TYPE() != 2:
            logger.info(f"Image ID {request.volume_id} is not of type DATABLOCK.")
            raise FailedPrecondition(f"OpenNebula image ID {request.volume_id} is not of type DATABLOCK")

        if image.get_PERSISTENT() != 1:
            logger.info(f"Image ID {request.volume_id} is not set as PERSISTENT.")
            raise FailedPrecondition(f"OpenNebula image ID {request.volume_id} is not set as persistent.")

        for requested_capability in request.volume_capabilities:
            confirmed_capability = csi_pb2.VolumeCapability()
            if requested_capability.WhichOneof("access_type") == "mount":
                logger.debug("Volume %s is of type mount.", request.volume_id)
                confirmed_capability.mount.SetInParent()
                if (
                        requested_capability.access_mode.mode
                        == confirmed_capability.AccessMode.SINGLE_NODE_WRITER
                        or requested_capability.access_mode.mode
                        == confirmed_capability.AccessMode.SINGLE_NODE_READER_ONLY
                ):
                    confirmed_capability.access_mode.mode = (
                        requested_capability.access_mode.mode
                    )
                    response.confirmed.volume_capabilities.append(
                        confirmed_capability
                    )

        return response

    def ControllerPublishVolume(self, request, context):
        if not request.volume_id:
            raise InvalidArgument("Missing volume Id")

        if not request.node_id:
            raise InvalidArgument("Missing node id")

        if not request.HasField("volume_capability"):
            raise InvalidArgument("Missing volume capabilities")

        logger.info(
            f"Attaching image ID {request.volume_id} to VM ID {request.node_id} as readonly: {request.readonly}"
        )

        if not re.match(constant.OPENNEBULA_INSTANCE_ID_REGEX, request.node_id):
            logger.error(f"Tried to attach the image to invalid node id: {request.node_id}")
            raise NotFound(f"Invalid OpenNebula VM ID {request.node_id}")

        self._attach_image(vm_id=int(request.node_id),
                           image_id=int(request.volume_id),
                           wait_settle_vm_action=True)
        attached_vm_template = self._one_api.vm.info(int(request.node_id)).get_TEMPLATE()

        for disk_attachment in attached_vm_template["DISK"]:
            if "IMAGE_ID" in disk_attachment and int(disk_attachment["IMAGE_ID"]) == int(request.volume_id):
                return csi_pb2.ControllerPublishVolumeResponse(
                    publish_context={"readonly": str(request.readonly),
                                     "node_target_path": f"/dev/{disk_attachment['TARGET']}"}
                )
        else:
            logger.error(f"OpenNebula successfully attached image ID {request.volume_id} to VM ID {request.node_id},"
                         f"but it does not exist in the VMs disk attachments")
            raise Internal(f"Cannot determine PVs target path")

    def ControllerUnpublishVolume(self, request, context):
        if not request.volume_id:
            raise InvalidArgument("Missing volume Id")

        logger.info(f"Unpublishing image ID {request.volume_id}")

        self._detach_image(vm_id=int(request.node_id),
                           image_id=int(request.volume_id),
                           wait_settle_vm_action=True)

        return csi_pb2.ControllerUnpublishVolumeResponse()

    def ControllerExpandVolume(self, request, context):
        """
        Handles requests to expand a volume
        :param request:
        :param context:
        :return:
        """

        if not request.volume_id:
            raise InvalidArgument("Missing volume ID")

        if not request.capacity_range:
            raise InvalidArgument("Missing new volume capacity range")

        new_image_size = self._determine_volume_size(request.capacity_range)

        expand_volume_response = csi_pb2.ControllerExpandVolumeResponse()
        expand_volume_response.capacity_bytes = new_image_size * (1024 ** 2)
        expand_volume_response.node_expansion_required = False

        try:
            image = self._one_api.image.info(int(request.volume_id))

            if image.get_SIZE() >= new_image_size:
                return expand_volume_response

            if len(image.get_VMS().get_ID()):
                expand_volume_response.node_expansion_required = True
                attached_vm_id = image.get_VMS().get_ID()[0]
                logger.debug(f"Image ID {request.volume_id} is currently attached to VM ID {attached_vm_id}, "
                             f"will notify the Kubelet to resize the file system.")
                logger.info(f"Expanding in an online manner image ID {request.volume_id} to {new_image_size} MB")
                self._resize_image(attached_vm_id=int(attached_vm_id),
                                   image_id=int(request.volume_id),
                                   new_size_in_mb=new_image_size,
                                   wait_settle_vm_action=True)
            else:
                logger.debug(f"Image ID {request.volume_id} is not currently attached, "
                             f"attaching to self (VM ID {self._my_vm_id})")
                self._attach_image(vm_id=self._my_vm_id,
                                   image_id=int(request.volume_id),
                                   wait_settle_vm_action=True)
                logger.info(f"Expanding in an offline manner image ID {request.volume_id} to {new_image_size} MB")
                self._resize_image(attached_vm_id=self._my_vm_id,
                                   image_id=int(request.volume_id),
                                   new_size_in_mb=new_image_size,
                                   wait_settle_vm_action=True)
                logger.debug(f"Detaching image ID {request.volume_id} from self (VM ID {self._my_vm_id})")
                self._detach_image(vm_id=self._my_vm_id,
                                   image_id=int(request.volume_id),
                                   wait_settle_vm_action=True)

        except pyone.OneNoExistsException as error:
            if "Error getting image" in str(error):
                error_message = f"Tried to resize image ID {request.volume_id} but it doesn't exist"
                logger.error(error_message)
                raise NotFound(error_message)

        return expand_volume_response

    def _attach_image(self,
                      vm_id: int,
                      image_id: int,
                      wait_settle_vm_action: bool = False):
        try:
            self._execute_vm_action(self._one_api.vm.attach,
                                    vm_id,
                                    f"DISK=[IMAGE_ID = \"{image_id}\"]",
                                    wait_settle_vm_state=wait_settle_vm_action)
        except pyone.OneNoExistsException as error:
            if "Error getting virtual machine" in str(error):
                error_message = f"Could not find VM ID {vm_id} in OpenNebula while attaching image {image_id}"
                logger.error(error_message)
                raise NotFound(error_message)
            elif "does not exist" in str(error):
                error_message = f"Image ID {image_id} does not exist in OpenNebula"
                logger.error(error_message)
                raise NotFound(error_message)
        except pyone.OneActionException as error:
            if "already in use" in str(error):
                image = self._one_api.image.info(int(image_id))
                attached_vm_id = image.get_VMS().get_ID()[0]
                if int(attached_vm_id) != vm_id:
                    error_message = f"Image ID {image_id} is already attached to VM ID {attached_vm_id}"
                    logger.error(error_message)
                    raise FailedPrecondition(error_message)
        except pyone.OneException as error:
            logger.error(f"OpenNebula API returned the following error: {str(error)}")
            raise Internal(str(error))

    def _detach_image(self,
                      vm_id: int,
                      image_id: int,
                      wait_settle_vm_action: bool = False) -> None:
        try:
            vm_template = self._one_api.vm.info(vm_id).get_TEMPLATE()

            if type(vm_template["DISK"]) != list:
                error_message = f"VM ID {vm_id} has no volumes to detach"
                logger.info(error_message)
                raise NotFound(error_message)
            else:
                for disk_attachment in vm_template["DISK"]:
                    if "IMAGE_ID" in disk_attachment and int(disk_attachment["IMAGE_ID"]) == int(image_id):
                        self._execute_vm_action(self._one_api.vm.detach,
                                                vm_id,
                                                int(disk_attachment["DISK_ID"]),
                                                wait_settle_vm_state=wait_settle_vm_action)
                        return

        except pyone.OneNoExistsException as error:
            if "Error getting virtual machine" in str(error):
                logger.error(f"Tried detaching image ID {image_id} from non-existing VM ID {vm_id}")
                raise NotFound(f"OpenNebula VM ID {vm_id} does not exist")

    def _resize_image(self,
                      attached_vm_id: int,
                      image_id: int,
                      new_size_in_mb: int,
                      wait_settle_vm_action: bool = False) -> None:
        attached_vm_template = self._one_api.vm.info(int(attached_vm_id)).get_TEMPLATE()

        for disk_attachment in attached_vm_template["DISK"]:
            if "IMAGE_ID" in disk_attachment and int(disk_attachment["IMAGE_ID"]) == image_id:
                self._execute_vm_action(self._one_api.vm.diskresize,
                                        attached_vm_id,
                                        int(disk_attachment["DISK_ID"]),
                                        str(new_size_in_mb),
                                        wait_settle_vm_state=wait_settle_vm_action)
                return

    @staticmethod
    def _execute_vm_action(api_action: Callable,
                           *api_args,
                           wait_settle_vm_state: bool = False) -> None:
        for i in range(1, 30 if wait_settle_vm_state else 1):
            try:
                logger.debug(f"Calling {api_action} with arguments {api_args}")
                api_action(*api_args)
                return
            except pyone.OneActionException as error:
                if "wrong state" in str(error):
                    sleep(i)
                else:
                    raise

    @staticmethod
    def _build_create_volume_response(volume_id: str, capacity_bytes: int):
        response = csi_pb2.CreateVolumeResponse()

        response.volume.volume_id = volume_id
        response.volume.capacity_bytes = capacity_bytes

        return response

    @staticmethod
    def _determine_volume_size(capacity_range):
        logger.debug(f"Required bytes: {capacity_range.required_bytes}, limit bytes: {capacity_range.limit_bytes}")

        if capacity_range.required_bytes > 0 or capacity_range.limit_bytes > 0:
            size_in_bytes = max(capacity_range.required_bytes, capacity_range.limit_bytes)
            if size_in_bytes >= constant.MIN_VOLUME_SIZE:
                return ceil(size_in_bytes / (1024 ** 2))
            else:
                return constant.MIN_VOLUME_SIZE
        else:
            return constant.DEFAULT_VOLUME_SIZE
