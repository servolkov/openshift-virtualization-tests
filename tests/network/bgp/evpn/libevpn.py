import contextlib
import logging
import shlex
from collections.abc import Generator
from dataclasses import dataclass

from ocp_resources.pod import Pod

from libs.net.cluster import ipv4_supported_cluster, ipv6_supported_cluster
from libs.net.ip import filter_link_local_addresses, random_ipv4_address, random_ipv6_address
from libs.net.traffic_generator import IPERF_SERVER_PORT, PodTcpClient, TcpServer
from libs.net.vmspec import lookup_iface_status, lookup_primary_network
from libs.vm.vm import BaseVirtualMachine
from tests.network.libs.bgp import NET_TOOLS_CONTAINER_NAME

LOGGER = logging.getLogger(__name__)

CUDN_EVPN_SUBNET_IPV4: str = f"{random_ipv4_address(net_seed=5, host_address=0)}/24"
CUDN_EVPN_SUBNET_IPV6: str = f"{random_ipv6_address(net_seed=5, host_address=0)}/64"

_BRIDGE_NAME: str = "br0"
_VXLAN_DEST_PORT: int = 4789

_L2_ENDPOINT_NETNS: str = "l2-ep"
_L2_VXLAN_NAME: str = "vxlan-l2"
_L2_VETH_POD_SIDE: str = "veth-l2-frr"
_L2_VETH_EP_SIDE: str = "veth-l2-ep"


@dataclass
class EvpnEndpoint:
    """External EVPN endpoint in a network namespace inside the FRR pod."""

    pod: Pod
    ip_addresses: list[str]
    netns_name: str


class EndpointTcpClient(PodTcpClient):
    """PodTcpClient that runs iperf3 inside a network namespace.

    'ip netns exec' replaces itself with iperf3 via execvp,
    so pgrep/pkill match by the bare iperf3 cmdline.

    Args:
        netns: Network namespace to run iperf3 in.
    """

    def __init__(
        self,
        pod: Pod,
        server_ip: str,
        server_port: int,
        netns: str,
        container: str | None = None,
    ) -> None:
        super().__init__(pod=pod, server_ip=server_ip, server_port=server_port, container=container)
        self._netns = netns

    def __enter__(self) -> "EndpointTcpClient":
        run_cmd = f"ip netns exec {self._netns} {self._cmd}"
        self._pod.execute(
            command=["sh", "-c", f"nohup {run_cmd} >/tmp/iperf3.log 2>&1 &"],
            container=self._container,
        )
        self._ensure_is_running()
        return self


def cudn_evpn_subnets() -> list[str]:
    """Returns CUDN EVPN subnets based on cluster IP family support.

    Returns:
        List of subnet CIDRs (IPv4 and/or IPv6) supported by the cluster.
    """
    subnets = []
    if ipv4_supported_cluster():
        subnets.append(CUDN_EVPN_SUBNET_IPV4)
    if ipv6_supported_cluster():
        subnets.append(CUDN_EVPN_SUBNET_IPV6)
    return subnets


def deploy_evpn_bridge(pod: Pod) -> None:
    """Creates the shared Linux bridge inside the FRR pod for EVPN endpoints."""
    commands = [
        f"ip link add {_BRIDGE_NAME} type bridge",
        f"ip link set {_BRIDGE_NAME} up",
    ]
    for command in commands:
        pod.execute(command=shlex.split(command), container=NET_TOOLS_CONTAINER_NAME)

    LOGGER.info(f"EVPN bridge deployed: {_BRIDGE_NAME}")


def teardown_evpn_bridge(pod: Pod) -> None:
    """Removes the EVPN bridge from the FRR pod."""
    pod.execute(command=shlex.split(f"ip link delete {_BRIDGE_NAME}"), container=NET_TOOLS_CONTAINER_NAME)
    LOGGER.info(f"EVPN bridge removed: {_BRIDGE_NAME}")


def deploy_evpn_l2_endpoint(
    pod: Pod,
    vni: int,
    local_vtep_ip: str,
    remote_vtep_ips: list[str],
    endpoint_ips: list[str],
) -> EvpnEndpoint:
    """Creates a stretched L2 endpoint: VXLAN device + bridge + veth + netns.

    Data path: VM -> OVN VXLAN (VNI) -> vxlan-l2 -> br0 -> veth -> l2-ep namespace.

    Args:
        pod: The FRR pod hosting the endpoint.
        vni: MAC-VRF VNI (must match CUDN's macVRF VNI).
        local_vtep_ip: FRR pod's IP used as local VTEP.
        remote_vtep_ips: Cluster node IPs for BUM traffic forwarding.
        endpoint_ips: IPs with prefix length (e.g. ["10.0.5.250/24", "fd00::fa/64"]).

    Returns:
        EvpnEndpoint.
    """
    commands = _build_l2_endpoint_commands(
        vni=vni,
        local_vtep_ip=local_vtep_ip,
        remote_vtep_ips=remote_vtep_ips,
        endpoint_ips=endpoint_ips,
    )
    for command in commands:
        pod.execute(command=shlex.split(command), container=NET_TOOLS_CONTAINER_NAME)

    bare_ips = [ip.split("/")[0] for ip in endpoint_ips]
    LOGGER.info(f"EVPN L2 endpoint deployed: {bare_ips} in namespace {_L2_ENDPOINT_NETNS}")

    return EvpnEndpoint(pod=pod, ip_addresses=bare_ips, netns_name=_L2_ENDPOINT_NETNS)


def teardown_evpn_l2_endpoint(pod: Pod) -> None:
    """Removes the EVPN L2 endpoint (netns, VXLAN, veth) from the FRR pod."""
    for cmd in [
        f"ip netns delete {_L2_ENDPOINT_NETNS}",
        f"ip link delete {_L2_VXLAN_NAME}",
        f"ip link delete {_L2_VETH_POD_SIDE}",
    ]:
        pod.execute(command=shlex.split(cmd), container=NET_TOOLS_CONTAINER_NAME, ignore_rc=True)

    LOGGER.info(f"EVPN L2 endpoint removed: namespace={_L2_ENDPOINT_NETNS}")


def _build_l2_endpoint_commands(
    vni: int,
    local_vtep_ip: str,
    remote_vtep_ips: list[str],
    endpoint_ips: list[str],
) -> list[str]:
    return [
        f"ip link add {_L2_VXLAN_NAME} type vxlan id {vni} local {local_vtep_ip} dstport {_VXLAN_DEST_PORT} nolearning",
        f"ip link set {_L2_VXLAN_NAME} master {_BRIDGE_NAME}",
        *(f"bridge fdb append 00:00:00:00:00:00 dev {_L2_VXLAN_NAME} dst {ip}" for ip in remote_vtep_ips),
        f"ip link add {_L2_VETH_POD_SIDE} type veth peer name {_L2_VETH_EP_SIDE}",
        f"ip link set {_L2_VETH_POD_SIDE} master {_BRIDGE_NAME}",
        f"ip netns add {_L2_ENDPOINT_NETNS}",
        f"ip link set {_L2_VETH_EP_SIDE} netns {_L2_ENDPOINT_NETNS}",
        *(f"ip netns exec {_L2_ENDPOINT_NETNS} ip addr add {ip} dev {_L2_VETH_EP_SIDE}" for ip in endpoint_ips),
        f"ip netns exec {_L2_ENDPOINT_NETNS} ip link set {_L2_VETH_EP_SIDE} up",
        f"ip netns exec {_L2_ENDPOINT_NETNS} ip link set lo up",
        f"ip link set {_L2_VXLAN_NAME} up",
        f"ip link set {_L2_VETH_POD_SIDE} up",
    ]


@contextlib.contextmanager
def evpn_workloads_active_connections(
    endpoint: EvpnEndpoint,
    vm: BaseVirtualMachine,
) -> Generator[list[tuple[EndpointTcpClient, TcpServer]]]:
    """Opens TCP connections for all IP families between an EVPN endpoint and a VM.

    Args:
        endpoint: EVPN endpoint (L2 or L3) running the TCP client (sends traffic).
        vm: VM running the TCP server (receives traffic).

    Yields:
        List of (EndpointTcpClient, TcpServer) tuples, one per IP family.
    """
    iface_name = lookup_primary_network(vm=vm).name
    iface = lookup_iface_status(vm=vm, iface_name=iface_name)
    server_ips = list(filter_link_local_addresses(ip_addresses=iface.ipAddresses))

    with contextlib.ExitStack() as stack:
        active_conns = []
        for server_ip in server_ips:
            active_conns.append(
                stack.enter_context(
                    cm=_evpn_workloads_connection(
                        endpoint=endpoint,
                        vm=vm,
                        server_ip=str(server_ip),
                    ),
                )
            )
        yield active_conns


@contextlib.contextmanager
def _evpn_workloads_connection(
    endpoint: EvpnEndpoint,
    vm: BaseVirtualMachine,
    server_ip: str,
) -> Generator[tuple[EndpointTcpClient, TcpServer]]:
    with TcpServer(vm=vm, port=IPERF_SERVER_PORT, bind_ip=server_ip) as tcp_server:
        with EndpointTcpClient(
            pod=endpoint.pod,
            server_ip=server_ip,
            server_port=IPERF_SERVER_PORT,
            netns=endpoint.netns_name,
            container=NET_TOOLS_CONTAINER_NAME,
        ) as tcp_client:
            yield tcp_client, tcp_server
