#!/usr/bin/python3

"""
Main entrypoint of the driver, starts the gRPC server
"""

import argparse
import logging
import os
from concurrent import futures
from pathlib import Path

import grpc
from grpc_interceptor import ExceptionToStatusInterceptor
from pb import csi_pb2_grpc

import services

logger = logging.getLogger("Main")


def getargs() -> argparse.Namespace:
    """Return ArgumentParser instance object"""
    parser = argparse.ArgumentParser(
        description="""StorPool CSI driver""",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "--csi-endpoint", type=str, default="unix:///run/csi/sock"
    )

    parser.add_argument(
        "--one-api-endpoint",
        type=str,
        default=None,
        help="OpenNebula RPC API endpoint",
    )

    parser.add_argument(
        "--one-api-username",
        type=str,
        default=None,
        help="OpenNebula RPC API authentication user",
    )

    parser.add_argument(
        "--one-api-password",
        type=str,
        default=None,
        help="OpenNebula RPC API authentication password",
    )

    parser.add_argument(
        "--one-vm-id",
        type=int,
        default=0,
        help="OpenNebula VM ID of this Kubernetes node"
    )

    parser.add_argument(
        "--one-vm-id-path",
        type=str,
        default="/var/lib/cloud/vm-id",
        help="Path to a file containing the OpenNebula VM ID of this Kubernetes node"
    )

    parser.add_argument("--log", type=str, default="WARNING", help="Log level")

    parser.add_argument(
        "--worker-threads",
        type=int,
        default=10,
        help="Worker thread count for the gRPC server",
    )

    return parser.parse_args()


def main() -> None:
    """
    Main function running the gRPC server
    :return: None
    """
    args = getargs()

    log_level = getattr(logging, args.log.upper(), None)

    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(funcName)s %(levelname)s: %(message)s",
        level=log_level,
    )

    interceptors = [
        ExceptionToStatusInterceptor(
            status_on_unknown_exception=grpc.StatusCode.INTERNAL
        )
    ]

    my_vm_id = args.one_vm_id

    if my_vm_id == 0:
        logger.debug(f"OpenNebula VM ID was not explicitly specified as a command-line argument, "
                     f"falling back to retrieving the ID from a file")

        vm_id_file_path = Path(args.one_vm_id_path)
        if not vm_id_file_path.exists():
            logger.critical(f"OpenNebula VM ID file does not exist at {args.one_vm_id_path}")
            exit(1)
        else:
            with open(vm_id_file_path) as vm_id_file:
                vm_id = int(vm_id_file.readline())

                if vm_id == 0:
                    logger.critical(f"Invalid OpenNebula VM ID {vm_id} found, exiting")
                    exit(1)
                else:
                    my_vm_id = vm_id

    grpc_server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=args.worker_threads),
        interceptors=interceptors,
    )

    identity_servicer = services.IdentityServicer()
    identity_servicer.set_ready(True)

    csi_pb2_grpc.add_IdentityServicer_to_server(identity_servicer, grpc_server)
    csi_pb2_grpc.add_ControllerServicer_to_server(
        services.ControllerServicer(
            one_api_endpoint=os.environ.get(
                "ONE_API_ENDPOINT", args.one_api_endpoint
            ),
            one_api_auth=f"{os.environ.get('ONE_API_USERNAME', args.one_api_username)}:"
                         f"{os.environ.get('ONE_API_PASSWORD', args.one_api_password)}",
            my_vm_id=my_vm_id
        ),
        grpc_server,
    )
    csi_pb2_grpc.add_NodeServicer_to_server(
        services.NodeServicer(my_vm_id=my_vm_id), grpc_server
    )

    grpc_server.add_insecure_port(
        os.environ.get("CSI_ENDPOINT", args.csi_endpoint)
    )
    grpc_server.start()
    grpc_server.wait_for_termination()


if __name__ == "__main__":
    main()
