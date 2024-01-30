import re

from box import Box
from broker import Broker
from fauxfactory import gen_string
import pytest

from robottelo.config import settings


@pytest.fixture(scope='module')
def module_discovery_hostgroup(module_org, module_location, module_target_sat):
    host = module_target_sat.api.Host(organization=module_org, location=module_location).create()
    return module_target_sat.api.HostGroup(
        organization=[module_org],
        location=[module_location],
        medium=host.medium,
        root_pass=gen_string('alpha'),
        operatingsystem=host.operatingsystem,
        ptable=host.ptable,
        domain=host.domain,
        architecture=host.architecture,
    ).create()


@pytest.fixture(scope='module')
def discovery_org(module_org, module_target_sat):
    discovery_org = module_target_sat.update_setting('discovery_organization', module_org.name)
    yield module_org
    module_target_sat.update_setting('discovery_organization', discovery_org)


@pytest.fixture(scope='module')
def discovery_location(module_location, module_target_sat):
    discovery_loc = module_target_sat.update_setting('discovery_location', module_location.name)
    yield module_location
    module_target_sat.update_setting('discovery_location', discovery_loc)


@pytest.fixture(scope='module')
def provisioning_env(module_target_sat, discovery_org, discovery_location):
    # Build PXE default template to get default PXE file
    module_target_sat.cli.ProvisioningTemplate().build_pxe_default()
    return module_target_sat.api_factory.configure_provisioning(
        org=discovery_org,
        loc=discovery_location,
        os=f'Redhat {module_target_sat.cli_factory.RHELRepository().repo_data["version"]}',
    )


@pytest.fixture(scope='session')
def session_discovery_sat(
    session_provisioning_sat,
    session_sca_manifest_org,
    session_location,
):
    """Creates a Satellite with discovery installed and configured"""
    sat = session_provisioning_sat.sat
    # Register to CDN and install discovery image
    sat.register_to_cdn()
    sat.execute('yum -y --disableplugin=foreman-protector install foreman-discovery-image')
    sat.unregister()
    # Symlink image so it can be uploaded for KEXEC
    disc_img_path = sat.execute(
        'find /usr/share/foreman-discovery-image -name "foreman-discovery-image-*.iso"'
    ).stdout[:-1]
    disc_img_name = disc_img_path.split("/")[-1]
    sat.execute(f'ln -s {disc_img_path} /var/www/html/pub/{disc_img_name}')
    # Change 'Default PXE global template entry'
    pxe_entry = sat.api.Setting().search(query={'search': 'Default PXE global template entry'})[0]
    if pxe_entry.value != 'discovery':
        pxe_entry.value = 'discovery'
        pxe_entry.update(['value'])
    # Build PXE default template to get default PXE file
    sat.api.ProvisioningTemplate().build_pxe_default()

    # Update discovery taxonomies settings
    discovery_loc = sat.api.Setting().search(query={'search': 'name=discovery_location'})[0]
    discovery_loc.value = session_location.name
    discovery_loc.update(['value'])
    discovery_org = sat.api.Setting().search(query={'search': 'name=discovery_organization'})[0]
    discovery_org.value = session_sca_manifest_org.name
    discovery_org.update(['value'])

    # Enable flag to auto provision discovered hosts via discovery rules
    discovery_auto = sat.api.Setting().search(query={'search': 'name=discovery_auto'})[0]
    discovery_auto.value = 'true'
    discovery_auto.update(['value'])

    return Box(sat=sat, iso=disc_img_name)


@pytest.fixture
def pxeless_discovery_host(provisioning_host, session_discovery_sat):
    """Fixture for returning a pxe-less discovery host for provisioning"""
    sat = session_discovery_sat.sat
    image_name = f'{gen_string("alpha")}-{session_discovery_sat.iso}'
    mac = provisioning_host._broker_args['provisioning_nic_mac_addr']
    # Remaster and upload discovery image to automatically input values
    result = sat.execute(
        'cd /var/www/html/pub && '
        f'discovery-remaster {session_discovery_sat.iso} '
        f'"proxy.type=foreman proxy.url=https://{sat.hostname}:443 fdi.pxmac={mac} fdi.pxauto=1"'
    )
    pattern = re.compile(r"foreman-discovery-image\S+")
    fdi = pattern.findall(result.stdout)[0]
    Broker(
        workflow='import-disk-image',
        import_disk_image_name=image_name,
        import_disk_image_url=(f'https://{sat.hostname}/pub/{fdi}'),
    ).execute()
    # Change host to boot from CD ISO
    Broker(
        job_template='configure-pxe-boot-rhv',
        target_host=provisioning_host.name,
        target_vlan_id=settings.provisioning.vlan_id,
        target_vm_firmware=provisioning_host._broker_args['target_vm_firmware'],
        target_vm_cd_iso=image_name,
        target_boot_scenario='pxeless_pre',
    ).execute()
    yield provisioning_host
    # Remove ISO from host and delete disk image
    Broker(
        job_template='configure-pxe-boot-rhv',
        target_host=provisioning_host.name,
        target_vlan_id=settings.provisioning.vlan_id,
        target_vm_firmware=provisioning_host._broker_args['target_vm_firmware'],
        target_boot_scenario='pxeless_pre',
    ).execute()
    Broker(workflow='remove-disk-image', remove_disk_image_name=image_name).execute()
