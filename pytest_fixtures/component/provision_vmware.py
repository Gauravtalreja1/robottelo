from fauxfactory import gen_string
import pytest

from robottelo.config import settings


@pytest.fixture(scope='session')
def session_vmware_cr(session_provisioning_sat, session_sca_manifest_org, session_location):
    vmware_cr = session_provisioning_sat.sat.api.VMWareComputeResource(
        name=gen_string('alpha'),
        provider='Vmware',
        url=settings.vmware.vcenter,
        user=settings.vmware.username,
        password=settings.vmware.password,
        datacenter=settings.vmware.datacenter,
        organization=[session_sca_manifest_org],
        location=[session_location],
    ).create()
    return vmware_cr


@pytest.fixture
def vmware_hostgroup(
    session_vmware_cr,
    session_provisioning_sat,
    session_sca_manifest_org,
    session_location,
    default_architecture,
    session_provisioning_rhel_content,
    session_lce_library,
    default_partitiontable,
    session_provisioning_capsule,
    pxe_loader,
):
    return session_provisioning_sat.sat.api.HostGroup(
        name=gen_string('alpha'),
        organization=[session_sca_manifest_org],
        location=[session_location],
        architecture=default_architecture,
        domain=session_provisioning_sat.domain,
        content_source=session_provisioning_capsule.id,
        content_view=session_provisioning_rhel_content.cv,
        compute_resource=session_vmware_cr,
        kickstart_repository=session_provisioning_rhel_content.ksrepo,
        lifecycle_environment=session_lce_library,
        root_pass=settings.provisioning.host_root_password,
        operatingsystem=session_provisioning_rhel_content.os,
        ptable=default_partitiontable,
        subnet=session_provisioning_sat.subnet,
        pxe_loader=pxe_loader.pxe_loader,
        group_parameters_attributes=[
            # assign AK in order the hosts to be subscribed
            {
                'name': 'kt_activation_keys',
                'parameter_type': 'string',
                'value': session_provisioning_rhel_content.ak.name,
            },
        ],
    ).create()
