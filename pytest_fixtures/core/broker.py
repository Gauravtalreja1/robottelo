from contextlib import contextmanager

from broker import Broker
import pytest

from robottelo.config import settings
from robottelo.hosts import ContentHostError, Satellite, lru_sat_ready_rhel


@pytest.fixture(scope='session')
def _default_sat(align_to_satellite):
    """Returns a Satellite object for settings.server.hostname"""
    if settings.server.hostname:
        try:
            return Satellite.get_host_by_hostname(settings.server.hostname)
        except ContentHostError:
            return Satellite()


@contextmanager
def _target_sat_imp(request, _default_sat, satellite_factory):
    """This is the actual working part of the following target_sat fixtures"""
    if request.node.get_closest_marker(name='destructive'):
        new_sat = satellite_factory()
        yield new_sat
        new_sat.teardown()
        Broker(hosts=[new_sat]).checkin()
    elif 'sanity' in request.config.option.markexpr:
        installer_sat = lru_sat_ready_rhel(settings.server.version.rhel_version)
        settings.set('server.hostname', installer_sat.hostname)
        yield installer_sat
    else:
        yield _default_sat


@pytest.fixture
def target_sat(request, _default_sat, satellite_factory):
    with _target_sat_imp(request, _default_sat, satellite_factory) as sat:
        yield sat


@pytest.fixture(scope='module')
def module_target_sat(request, _default_sat, satellite_factory):
    with _target_sat_imp(request, _default_sat, satellite_factory) as sat:
        yield sat


@pytest.fixture(scope='session')
def session_target_sat(request, _default_sat, satellite_factory):
    with _target_sat_imp(request, _default_sat, satellite_factory) as sat:
        yield sat


@pytest.fixture(scope='class')
def class_target_sat(request, _default_sat, satellite_factory):
    with _target_sat_imp(request, _default_sat, satellite_factory) as sat:
        yield sat
