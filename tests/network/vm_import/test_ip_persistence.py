from typing import Final

import pytest

from libs.net.traffic_generator import is_tcp_connection
from libs.net.vmspec import IP_ADDRESS, lookup_iface_status, lookup_primary_network
from tests.network.localnet.liblocalnet import client_server_active_connection
from tests.network.vm_import.source_provider import extract_primary_vm_network_data
from utilities.constants import PUBLIC_DNS_SERVER_IP, QUARANTINED
from utilities.virt import migrate_vm_and_verify

SERVER_PORT: Final[int] = 1234
VM_CONSOLE_CMD_TIMEOUT: Final[int] = 20

pytestmark = pytest.mark.xfail(
    reason=f"{QUARANTINED}: Migration takes very long, tracked in MTV-3947",
    run=False,
)


@pytest.mark.polarion("CNV-12208")
def test_network_data_is_preserved_after_vm_import(source_vm_powered_on, imported_cudn_vm):
    source_vm_ip, source_vm_mac = extract_primary_vm_network_data(vm=source_vm_powered_on)
    target_vm_iface = lookup_iface_status(
        vm=imported_cudn_vm, iface_name=lookup_primary_network(vm=imported_cudn_vm).name
    )
    target_vm_ip, target_vm_mac = target_vm_iface.get(IP_ADDRESS, None), target_vm_iface.get("mac", None)

    assert source_vm_mac == target_vm_ip and source_vm_ip == target_vm_ip, (
        f"The network data was not preserved during VM import. Expected: IP={source_vm_ip}, MAC={source_vm_mac}. "
        f"Got: IP={target_vm_ip}, MAC={target_vm_mac}."
    )


@pytest.mark.polarion("")
def test_imported_vm_egress_connectivity(imported_cudn_vm):
    imported_cudn_vm.console(commands=[f"ping -c 3 {PUBLIC_DNS_SERVER_IP}"], timeout=VM_CONSOLE_CMD_TIMEOUT)


@pytest.mark.polarion("CNV-12212")
def test_connectivity_between_imported_and_local_vms(imported_cudn_vm, local_cudn_vm):
    with client_server_active_connection(
        client_vm=imported_cudn_vm,
        server_vm=local_cudn_vm,
        spec_logical_network=lookup_primary_network(vm=local_cudn_vm).name,
        port=SERVER_PORT,
    ) as (client, server):
        assert is_tcp_connection(server=server, client=client)


def test_connectivity_over_inner_migration_between_imported_and_local_vms(
    admin_client, imported_cudn_vm, local_cudn_vm
):
    with client_server_active_connection(
        client_vm=imported_cudn_vm,
        server_vm=local_cudn_vm,
        spec_logical_network=lookup_primary_network(vm=local_cudn_vm).name,
        port=SERVER_PORT,
    ) as (client, server):
        migrate_vm_and_verify(vm=imported_cudn_vm, client=admin_client)
        assert is_tcp_connection(server=server, client=client)
