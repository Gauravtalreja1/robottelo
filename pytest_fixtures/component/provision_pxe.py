import ipaddress
import os
from tempfile import mkstemp

from box import Box
from broker import Broker
from fauxfactory import gen_string
from packaging.version import Version
import pytest

from robottelo import constants
from robottelo.config import settings
from robottelo.hosts import ContentHost


@pytest.fixture(scope='session')
def session_provisioning_capsule(session_target_sat, session_location):
    """Assigns the `session_location` to Satellite's internal capsule and returns it"""
    capsule = session_target_sat.nailgun_smart_proxy
    capsule.location = [session_location]
    return capsule.update(['location'])


@pytest.fixture(scope='session')
def session_provisioning_rhel_content(
    request,
    session_provisioning_sat,
    session_sca_manifest_org,
    session_lce_library,
):
    """
    This fixture sets up kickstart repositories for a specific RHEL version
    that is specified in `request.param`.
    """
    sat = session_provisioning_sat.sat
    rhel_ver = request.param['rhel_version']
    repo_names = []
    if int(rhel_ver) <= 7:
        repo_names.append(f'rhel{rhel_ver}')
    else:
        repo_names.append(f'rhel{rhel_ver}_bos')
        repo_names.append(f'rhel{rhel_ver}_aps')
    rh_repos = []
    tasks = []
    rh_repo_id = ""
    content_view = sat.api.ContentView(organization=session_sca_manifest_org).create()

    # Custom Content for Client repo
    custom_product = sat.api.Product(
        organization=session_sca_manifest_org, name=f'rhel{rhel_ver}_{gen_string("alpha")}'
    ).create()
    client_repo = sat.api.Repository(
        organization=session_sca_manifest_org,
        product=custom_product,
        content_type='yum',
        url=settings.repos.SATCLIENT_REPO[f'rhel{rhel_ver}'],
    ).create()
    task = client_repo.sync(synchronous=False)
    tasks.append(task)
    content_view.repository = [client_repo]

    for name in repo_names:
        rh_kickstart_repo_id = sat.api_factory.enable_rhrepo_and_fetchid(
            basearch=constants.DEFAULT_ARCHITECTURE,
            org_id=session_sca_manifest_org.id,
            product=constants.REPOS['kickstart'][name]['product'],
            repo=constants.REPOS['kickstart'][name]['name'],
            reposet=constants.REPOS['kickstart'][name]['reposet'],
            releasever=constants.REPOS['kickstart'][name]['version'],
        )
        # do not sync content repos for discovery based provisioning.
        if not session_provisioning_sat.provisioning_type == 'discovery':
            rh_repo_id = sat.api_factory.enable_rhrepo_and_fetchid(
                basearch=constants.DEFAULT_ARCHITECTURE,
                org_id=session_sca_manifest_org.id,
                product=constants.REPOS[name]['product'],
                repo=constants.REPOS[name]['name'],
                reposet=constants.REPOS[name]['reposet'],
                releasever=constants.REPOS[name]['releasever'],
            )

        # Sync step because repo is not synced by default
        for repo_id in [rh_kickstart_repo_id, rh_repo_id]:
            if repo_id:
                rh_repo = sat.api.Repository(id=repo_id).read()
                task = rh_repo.sync(synchronous=False)
                tasks.append(task)
                rh_repos.append(rh_repo)
                content_view.repository.append(rh_repo)
                content_view.update(['repository'])
    for task in tasks:
        sat.wait_for_tasks(
            search_query=(f'id = {task["id"]}'),
            poll_timeout=2500,
        )
        task_status = sat.api.ForemanTask(id=task['id']).poll()
        assert task_status['result'] == 'success'
    rhel_xy = Version(
        constants.REPOS['kickstart'][f'rhel{rhel_ver}']['version']
        if rhel_ver == 7
        else constants.REPOS['kickstart'][f'rhel{rhel_ver}_bos']['version']
    )
    o_systems = sat.api.OperatingSystem().search(
        query={'search': f'family=Redhat and major={rhel_xy.major} and minor={rhel_xy.minor}'}
    )
    assert o_systems, f'Operating system RHEL {rhel_xy} was not found'
    os = o_systems[0].read()
    # return only the first kickstart repo - RHEL X KS or RHEL X BaseOS KS
    ksrepo = rh_repos[0]
    publish = content_view.publish()
    task_status = sat.wait_for_tasks(
        search_query=(f'Actions::Katello::ContentView::Publish and id = {publish["id"]}'),
        search_rate=15,
        max_tries=10,
    )
    assert task_status[0].result == 'success'
    content_view = sat.api.ContentView(
        organization=session_sca_manifest_org, name=content_view.name
    ).search()[0]
    ak = sat.api.ActivationKey(
        organization=session_sca_manifest_org,
        content_view=content_view,
        environment=session_lce_library,
    ).create()

    # Ensure client repo is enabled in the activation key
    content = ak.product_content(data={'content_access_mode_all': '1'})['results']
    client_repo_label = [repo['label'] for repo in content if repo['name'] == client_repo.name][0]
    ak.content_override(
        data={'content_overrides': [{'content_label': client_repo_label, 'value': '1'}]}
    )
    return Box(os=os, ak=ak, ksrepo=ksrepo, cv=content_view)


@pytest.fixture(scope='session')
def session_provisioning_sat(
    request,
    session_target_sat,
    session_sca_manifest_org,
    session_location,
    session_provisioning_capsule,
):
    """
    This fixture sets up the Satellite for PXE provisioning.
    It calls a workflow using broker to set up the network and to run satellite-installer.
    It uses the artifacts from the workflow to create all the necessary Satellite entities
    that are later used by the tests.
    """
    provisioning_type = getattr(request, 'param', '')
    sat = session_target_sat
    provisioning_domain_name = f'{gen_string("alpha").lower()}.foo'

    broker_data_out = Broker().execute(
        workflow='configure-install-sat-provisioning-rhv',
        artifacts='last',
        target_vlan_id=settings.provisioning.vlan_id,
        target_host=sat.name,
        provisioning_dns_zone=provisioning_domain_name,
        sat_version='stream' if sat.is_stream else sat.version,
    )

    broker_data_out = Box(**broker_data_out['data_out'])
    provisioning_interface = ipaddress.ip_interface(broker_data_out.provisioning_addr_ipv4)
    provisioning_network = provisioning_interface.network
    # TODO: investigate DNS setup issue on Satellite,
    # we might need to set up Sat's DNS server as the primary one on the Sat host
    provisioning_upstream_dns_primary = (
        broker_data_out.provisioning_upstream_dns.pop()
    )  # There should always be at least one upstream DNS
    provisioning_upstream_dns_secondary = (
        broker_data_out.provisioning_upstream_dns.pop()
        if len(broker_data_out.provisioning_upstream_dns)
        else None
    )

    domain = sat.api.Domain(
        location=[session_location],
        organization=[session_sca_manifest_org],
        dns=session_provisioning_capsule.id,
        name=provisioning_domain_name,
    ).create()

    subnet = sat.api.Subnet(
        location=[session_location],
        organization=[session_sca_manifest_org],
        network=str(provisioning_network.network_address),
        mask=str(provisioning_network.netmask),
        gateway=broker_data_out.provisioning_gw_ipv4,
        from_=broker_data_out.provisioning_host_range_start,
        to=broker_data_out.provisioning_host_range_end,
        dns_primary=provisioning_upstream_dns_primary,
        dns_secondary=provisioning_upstream_dns_secondary,
        boot_mode='DHCP',
        ipam='DHCP',
        dhcp=session_provisioning_capsule.id,
        tftp=session_provisioning_capsule.id,
        template=session_provisioning_capsule.id,
        dns=session_provisioning_capsule.id,
        httpboot=session_provisioning_capsule.id,
        discovery=session_provisioning_capsule.id,
        remote_execution_proxy=[session_provisioning_capsule.id],
        domain=[domain.id],
    ).create()

    # Workaround BZ: 2207698
    sat.execute(
        'echo ":blacklist_duration_minutes: 2" >> /etc/foreman-proxy/settings.d/dhcp_isc.yml'
    )
    assert sat.cli.Service.restart().status == 0

    return Box(sat=sat, domain=domain, subnet=subnet, provisioning_type=provisioning_type)


@pytest.fixture(scope='session')
def session_ssh_key_file():
    _, layout = mkstemp(text=True)
    os.chmod(layout, 0o600)
    with open(layout, 'w') as ssh_key:
        ssh_key.write(settings.provisioning.host_ssh_key_priv)
    return layout


@pytest.fixture
def provisioning_host(session_ssh_key_file, pxe_loader):
    """Fixture to check out blank VM"""
    # TODO: Make this cd_iso optional fixture parameter (update vm_firmware when adding this)
    cd_iso = ''
    with Broker(
        workflow='deploy-configure-pxe-provisioning-host-rhv',
        host_class=ContentHost,
        target_vlan_id=settings.provisioning.vlan_id,
        target_vm_firmware=pxe_loader.vm_firmware,
        target_vm_cd_iso=cd_iso,
        blank=True,
        target_memory='6GiB',
        auth=session_ssh_key_file,
    ) as prov_host:
        yield prov_host
        # Set host as non-blank to run teardown of the host
        prov_host.blank = getattr(prov_host, 'blank', False)


@pytest.fixture
def provision_multiple_hosts(session_ssh_key_file, pxe_loader, request):
    """Fixture to check out two blank VMs"""
    # TODO: Make this cd_iso optional fixture parameter (update vm_firmware when adding this)
    cd_iso = ''
    with Broker(
        workflow='deploy-configure-pxe-provisioning-host-rhv',
        host_class=ContentHost,
        _count=getattr(request, 'param', 2),
        target_vlan_id=settings.provisioning.vlan_id,
        target_vm_firmware=pxe_loader.vm_firmware,
        target_vm_cd_iso=cd_iso,
        blank=True,
        target_memory='6GiB',
        auth=session_ssh_key_file,
    ) as hosts:
        yield hosts
        # Set host as non-blank to run teardown of the host
        for prov_host in hosts:
            prov_host.blank = getattr(prov_host, 'blank', False)


@pytest.fixture
def provisioning_hostgroup(
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
        organization=[session_sca_manifest_org],
        location=[session_location],
        architecture=default_architecture,
        domain=session_provisioning_sat.domain,
        content_source=session_provisioning_capsule.id,
        content_view=session_provisioning_rhel_content.cv,
        kickstart_repository=session_provisioning_rhel_content.ksrepo,
        lifecycle_environment=session_lce_library,
        root_pass=settings.provisioning.host_root_password,
        operatingsystem=session_provisioning_rhel_content.os,
        ptable=default_partitiontable,
        subnet=session_provisioning_sat.subnet,
        pxe_loader=pxe_loader.pxe_loader,
        group_parameters_attributes=[
            {
                'name': 'remote_execution_ssh_keys',
                'parameter_type': 'string',
                'value': settings.provisioning.host_ssh_key_pub,
            },
            # assign AK in order the hosts to be subscribed
            {
                'name': 'kt_activation_keys',
                'parameter_type': 'string',
                'value': session_provisioning_rhel_content.ak.name,
            },
        ],
    ).create()


@pytest.fixture
def pxe_loader(request):
    """Map the appropriate PXE loader to VM bootloader"""
    PXE_LOADER_MAP = {
        'bios': {'vm_firmware': 'bios', 'pxe_loader': 'PXELinux BIOS'},
        'uefi': {'vm_firmware': 'uefi', 'pxe_loader': 'Grub2 UEFI'},
        'ipxe': {'vm_firmware': 'bios', 'pxe_loader': 'iPXE Embedded'},
        'http_uefi': {'vm_firmware': 'uefi', 'pxe_loader': 'Grub2 UEFI HTTP'},
    }
    return Box(PXE_LOADER_MAP[getattr(request, 'param', 'bios')])
