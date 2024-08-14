"""
This module contains various utility functions
"""


def get_mounted_devices() -> list[dict]:
    """
    Returns all mounts currently present on the node
    :return: A list containing dictionaries with information about a mount
    :rtype: list
    """
    result = []
    with open("/proc/mounts") as file:
        mounts = [mount.strip("\n") for mount in file.readlines()]
        for mount in mounts:
            attributes = mount.split(" ")
            result.append(
                {
                    "device": attributes[0],
                    "target": attributes[1],
                    "filesystem": attributes[2],
                    "options": attributes[3],
                }
            )
        return result
