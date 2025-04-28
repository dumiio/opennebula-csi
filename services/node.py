"""
Bla-bla
"""
import distutils.util
import logging
import subprocess
import os

from pathlib import Path

from grpc_interceptor.exceptions import (
    NotFound,
    Internal,
    AlreadyExists,
    InvalidArgument,
)
from pb import csi_pb2
from pb import csi_pb2_grpc

import utils

RESIZE_TOOL_MAP = {
    "ext4": "/sbin/resize2fs"
}

logger = logging.getLogger("NodeService")


def image_is_attached(image_path: str) -> bool:
    """
    Checks whether a StorPool volume is attached to the current node
    """
    return Path(image_path).is_block_device()


def image_is_formatted(image_path: str) -> bool:
    """
    Checks whether a StorPool volume is formatted
    """
    return (
        subprocess.run(
            ["blkid", image_path], check=False
        ).returncode
        == 0
    )


def image_get_fs(image_path: str) -> str:
    """
    Returns the filesystem of a volume
    """
    return subprocess.run(
        ["blkid", "-o", "value", "-s", "TYPE", image_path],
        check=False,
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()


def image_is_mounted(image_path: str) -> bool:
    """
    Checks if a volume is mounted
    """
    system_mounts = utils.get_mounted_devices()
    return (
        len(
            [
                mount
                for mount in system_mounts
                if mount["device"] == image_path
            ]
        )
        > 0
    )


def image_get_mount_info(image_path: str) -> dict:
    """
    Retrieves information about a mount
    """
    system_mounts = utils.get_mounted_devices()
    return [
        mount
        for mount in system_mounts
        if mount["device"] == image_path
    ][0]


def generate_mount_options(readonly: bool, mount_flags) -> str:
    """
    Generates mount options taking into account if the volume is read-only
    """
    mount_options = ["discard"]

    if readonly:
        mount_options.append("ro")
    else:
        mount_options.append("rw")

    mount_options.extend(mount_flags)

    if len(mount_options) == 1:
        return mount_options[0]

    return ",".join(mount_options)


class NodeServicer(csi_pb2_grpc.NodeServicer):
    """
    Provides NodeService implementation
    """

    def __init__(self, my_vm_id: int):
        self._node_id = my_vm_id

    def NodeGetInfo(self, request, context):
        return csi_pb2.NodeGetInfoResponse(
            node_id=str(self._node_id),
            max_volumes_per_node=20,
        )

    def NodeGetCapabilities(self, request, context):
        response = csi_pb2.NodeGetCapabilitiesResponse()

        stage_unstage_cap = response.capabilities.add()
        stage_unstage_cap.rpc.type = stage_unstage_cap.RPC.STAGE_UNSTAGE_VOLUME

        volume_expand_cap = response.capabilities.add()
        volume_expand_cap.rpc.type = volume_expand_cap.RPC.EXPAND_VOLUME

        volume_stats_cap = response.capabilities.add()
        volume_stats_cap.rpc.type = volume_stats_cap.RPC.GET_VOLUME_STATS

        return response

    def NodeStageVolume(self, request, context):
        if not request.volume_id:
            raise InvalidArgument("Missing volume id.")

        if not request.HasField("volume_capability"):
            raise InvalidArgument("Missing volume capabilities.")

        if not request.staging_target_path:
            raise InvalidArgument("Missing staging path.")

        if "node_target_path" not in request.publish_context:
            raise Internal(f"Node target path not specified")

        image_device_path = request.publish_context["node_target_path"]

        if not image_is_attached(image_device_path):
            logger.error(f"Image target path {image_device_path} does not exist at VM ID {self._node_id}")
            raise NotFound(f"Could not locate image ID {request.volume_id} path at VM ID {self._node_id}")

        if request.volume_capability.WhichOneof("access_type") == "mount":
            logger.info(
                "Staging mount volume: %s to path: %s",
                request.volume_id,
                request.staging_target_path,
            )

            image_requested_fs = "ext4"

            if request.volume_capability.mount.fs_type:
                image_requested_fs = request.volume_capability.mount.fs_type
                logger.debug(f"CO specified file system: {image_requested_fs}")

            logger.debug(f"CO specified readonly: {request.publish_context['readonly']}")

            if request.volume_capability.mount.mount_flags:
                logger.debug(f"CO specified the following mount options: {request.volume_capability.mount.mount_flags}")

            mount_options = generate_mount_options(
                bool(
                    distutils.util.strtobool(
                        request.publish_context["readonly"]
                    )
                ),
                request.volume_capability.mount.mount_flags,
            )

            if not image_is_mounted(image_device_path):
                if not image_is_formatted(image_device_path):
                    logger.debug(
                        """Volume %s is not formatted, formatting with %s""",
                        request.volume_id,
                        image_requested_fs,
                    )
                    format_command = subprocess.run(
                        [
                            "mkfs." + image_requested_fs,
                            image_device_path,
                        ],
                        stdout=subprocess.DEVNULL,
                        encoding="utf-8",
                        capture_output=False,
                        check=False,
                    )
                    if format_command.returncode != 0:
                        logger.error(
                            """Failed to format volume %s with the following error: %s""",
                            request.volume_id,
                            format_command.stderr,
                        )
                        raise Internal(
                            f"""StorPool volume {request.volume_id} format
                             failed with error: {format_command.stderr}"""
                        )
                else:
                    image_current_fs = image_get_fs(image_device_path)
                    if image_requested_fs != image_current_fs:
                        logger.error(
                            """Volume %s is already formatted with %s""",
                            request.volume_id,
                            image_current_fs,
                        )
                        raise AlreadyExists(
                            f"""StorPool volume {request.volume_id} is already formatted
                             with {image_current_fs} but CO tried to
                             stage it with {image_requested_fs}"""
                        )
                    else:
                        fsck_command = subprocess.run(
                            [
                                "fsck",
                                "-T",
                                "-fp",
                                image_device_path,
                            ],
                            encoding="utf-8",
                            capture_output=True,
                            check=False,
                        )

                        if fsck_command.returncode != 0:
                            error_message = f"Running fsck on {image_device_path} failed with code: {fsck_command.returncode}"
                            logger.error(error_message)
                            logger.error(f"Output: {fsck_command.stdout}")
                            logger.error(f"Error: {fsck_command.stderr}")
                            raise Internal(error_message)

                        self._extend_image(image_device_path, image_current_fs)

                logger.debug(
                    f"Volume {request.volume_id} is not mounted, mounting at {request.staging_target_path}"
                )

                mount_command = subprocess.run(
                    [
                        "mount",
                        "-o",
                        mount_options,
                        image_device_path,
                        request.staging_target_path,
                    ],
                    encoding="utf-8",
                    capture_output=True,
                    check=False,
                )

                if mount_command.returncode != 0:
                    logger.error(
                        """Failed to mount volume %s with the following error: %s""",
                        request.volume_id,
                        mount_command.stderr,
                    )
                    raise Internal(
                        f"""The following error occurred while
                        mounting StorPool volume {request.volume_id}: {mount_command.stderr}"""
                    )
            else:
                image_mount_info = image_get_mount_info(request.volume_id)

                if image_mount_info["target"] != request.staging_target_path:
                    logger.error(
                        """Volume %s is already mounted at %s""",
                        request.volume_id,
                        request.staging_target_path,
                    )
                    raise AlreadyExists(
                        f"""StorPool volume {request.volume_id} is
                         already mounted at {image_mount_info['target']}"""
                    )

                if (
                    request.volume_capability.mount.mount_flags
                    and image_mount_info["options"] != mount_options
                ):
                    logger.error(
                        """Volume %s is already mounted with %s""",
                        request.volume_id,
                        image_mount_info["options"],
                    )
                    raise AlreadyExists(
                        f"""StorPool volume {request.volume_id} is
                         already mounted with {image_mount_info['options']}"""
                    )

        return csi_pb2.NodeStageVolumeResponse()

    def NodeUnstageVolume(self, request, context):
        if not request.volume_id:
            raise InvalidArgument("Missing volume id")

        if not request.staging_target_path:
            raise InvalidArgument("Missing stating target path")

        for mount in utils.get_mounted_devices():
            if mount["target"] == request.staging_target_path:
                logger.debug(f"Image ID {request.volume_id} is mounted, unmounting")
                unmount_command = subprocess.run(
                    ["umount", request.staging_target_path],
                    encoding="utf-8",
                    capture_output=True,
                    check=False,
                )
                if unmount_command.returncode != 0:
                    logger.error(
                        """Failed to unmount volume %s with the following error: %s""",
                        request.volume_id,
                        unmount_command.stderr,
                    )
                    raise Internal(
                        f"The following error occurred while unmounting "
                        f"StorPool volume {request.volume_id}: {unmount_command.stderr}"
                    )

        return csi_pb2.NodeUnstageVolumeRequest()

    def NodePublishVolume(self, request, context):
        if not request.volume_id:
            raise InvalidArgument("Missing volume id")

        if not request.target_path:
            raise InvalidArgument("Missing target path")

        if not request.HasField("volume_capability"):
            raise InvalidArgument("Missing volume capabilities")

        logger.info(
            "Publishing volume %s at %s",
            request.volume_id,
            request.target_path,
        )

        target_path = Path(request.target_path)

        if not target_path.exists():
            logger.debug(
                "Target path %s doesn't exist, creating it.",
                request.target_path,
            )
            target_path.mkdir(mode=755, parents=True, exist_ok=True)

        if not target_path.is_mount():
            logger.debug(
                "Volume %s is not mounted, mounting it.", request.volume_id
            )
            mount_options = ["bind"]

            if request.readonly:
                mount_options.append("ro")
            else:
                mount_options.append("rw")

            mount_options.extend(request.volume_capability.mount.mount_flags)

            mount_command = subprocess.run(
                [
                    "mount",
                    "-o",
                    ",".join(mount_options),
                    request.staging_target_path,
                    request.target_path,
                ],
                encoding="utf-8",
                capture_output=False,
                check=False,
                stdout=subprocess.DEVNULL,
            )

            if mount_command.returncode != 0:
                logger.error(
                    "Binding volume %s failed with: %s",
                    request.volume_id,
                    mount_command.stderr,
                )
                raise Internal(
                    f"""The following error occurred
                     while binding StorPool volume {request.volume_id}: {mount_command.stderr}"""
                )

        return csi_pb2.NodePublishVolumeResponse()

    def NodeUnpublishVolume(self, request, context):
        if not request.volume_id:
            raise InvalidArgument("Missing volume id")

        if not request.target_path:
            raise InvalidArgument("Missing target path")

        logger.info(
            """Unpublishing volume %s from %s""",
            request.volume_id,
            request.target_path,
        )

        target_path = Path(request.target_path)

        if target_path.is_mount():
            logger.debug(
                "Volume %s is mounted, unmounting it", request.volume_id
            )
            unmount_command = subprocess.run(
                ["umount", request.target_path],
                encoding="utf-8",
                capture_output=False,
                check=False,
                stdout=subprocess.DEVNULL,
            )

            if unmount_command.returncode != 0:
                logger.error(
                    "Unbinding volume %s failed with: %s",
                    request.volume_id,
                    unmount_command.stderr,
                )
                raise Internal(
                    f"""The following error occurred while unbinding
                     StorPool volume {request.volume_id}: {unmount_command.stderr}"""
                )

        if target_path.is_dir():
            logger.debug(
                "Volume target path %s exists, removing it",
                request.target_path,
            )
            remove_target_path_command = subprocess.run(
                ["rmdir", request.target_path],
                encoding="utf-8",
                capture_output=False,
                check=False,
                stdout=subprocess.DEVNULL,
            )

            if remove_target_path_command.returncode != 0:
                logger.error(
                    """Failed to remove target path %s, error: %s""",
                    request.volume_id,
                    remove_target_path_command.stderr,
                )
                raise Internal(
                    f"The following error occurred while removing the target path {request.volume_id}: "
                    f"{remove_target_path_command.stderr}"
                )

        return csi_pb2.NodeUnpublishVolumeResponse()

    def NodeGetVolumeStats(self, request, context):
        if not request.volume_id:
            raise InvalidArgument("Missing volume id")

        if not request.volume_path:
            raise InvalidArgument("Missing volume path")

        logger.info(
            "Getting volume stats for %s at path %s",
            request.volume_id,
            request.volume_path,
        )

        volume_path = Path(request.volume_path)

        if not volume_path.exists():
            logger.error(
                "Volume path %s does not exist", request.volume_path
            )
            raise NotFound(f"Volume path {request.volume_path} does not exist")
        
        if not volume_path.is_mount():
            logger.error(
                f"Volume {request.volume_id} is not attached to node {self._node_id}"
            )
            raise NotFound(f"Volume {request.volume_id} is not attached to node {self._node_id}")

        response = csi_pb2.NodeGetVolumeStatsResponse()

        try:
            stat = os.statvfs(request.volume_path)
            
            bytes_usage = response.usage.add()
            bytes_usage.unit = csi_pb2.VolumeUsage.Unit.BYTES
            
            bytes_usage.total = stat.f_blocks * stat.f_frsize
            bytes_usage.available = stat.f_bavail * stat.f_frsize
            bytes_usage.used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
            
            inodes_usage = response.usage.add()
            inodes_usage.unit = csi_pb2.VolumeUsage.Unit.INODES
            
            inodes_usage.total = stat.f_files
            inodes_usage.available = stat.f_favail
            inodes_usage.used = stat.f_files - stat.f_ffree
            
            logger.debug(
                "Volume %s stats: bytes total=%d, available=%d, used=%d; inodes total=%d, available=%d, used=%d",
                request.volume_id,
                bytes_usage.total,
                bytes_usage.available,
                bytes_usage.used,
                inodes_usage.total,
                inodes_usage.available,
                inodes_usage.used,
            )

        except Exception as e:
            logger.error(
                "Failed to get volume stats for %s: %s",
                request.volume_id,
                str(e),
            )
            raise Internal(f"Failed to get volume stats: {str(e)}")

        return response

    def NodeExpandVolume(self, request, context):
        """
        Handles FS resize accordingly
        :param request:
        :param context:
        :return:
        """

        if not request.volume_id:
            raise InvalidArgument("Missing volume id.")

        logger.info(f"Extending image {request.volume_id} file system")

        for mount in utils.get_mounted_devices():
            if mount["target"] == request.staging_target_path:
                logger.debug(f"Detected device {mount['device']} file system: {mount['filesystem']}")

                self._extend_image(mount["device"], mount["filesystem"])

                expand_volume_response = csi_pb2.NodeExpandVolumeResponse()
                return expand_volume_response

    @staticmethod
    def _extend_image(image_device_path: str, image_fs: str):
        try:
            extend_fs_tool = RESIZE_TOOL_MAP[image_fs]
        except KeyError:
            logger.error(f"CO requested to extend an unsupported file system: {image_fs}")
            raise Internal(f"Unsupported file system: {image_fs}")

        logger.debug(f"Using {extend_fs_tool} to extend the file system")

        extend_command = subprocess.run([
            extend_fs_tool,
            image_device_path
        ],
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        if extend_command.returncode != 0:
            error_message = (f"Extending the file system for volume {image_device_path} failed with: "
                             f"{extend_command.stderr}")
            logger.error(error_message)
            raise Internal(error_message)

        logger.debug(f"Resize tool rc: {extend_command.returncode}, output: {extend_command.stdout}")

