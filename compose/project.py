from __future__ import absolute_import
from __future__ import unicode_literals

import logging
import os
from functools import reduce

from docker.errors import APIError

from .config import ConfigurationError
from .config import get_service_name_from_net
from .const import DEFAULT_TIMEOUT
from .const import LABEL_ONE_OFF
from .const import LABEL_PROJECT
from .const import LABEL_SERVICE
from .container import Container
from .legacy import check_for_legacy_containers
from .network import Network
from .service import ContainerNet
from .service import ConvergenceStrategy
from .service import Net
from .service import parse_volume_from_spec
from .service import Service
from .service import ServiceNet
from .service import VolumeFromSpec
from .utils import parallel_execute


log = logging.getLogger(__name__)


def sort_service_dicts(services):
    # Topological sort (Cormen/Tarjan algorithm).
    unmarked = services[:]
    temporary_marked = set()
    sorted_services = []

    def get_service_names(links):
        return [link.split(':')[0] for link in links]

    def get_service_names_from_volumes_from(volumes_from):
        return [
            parse_volume_from_spec(volume_from).source
            for volume_from in volumes_from
        ]

    def get_service_dependents(service_dict, services):
        name = service_dict['name']
        return [
            service for service in services
            if (name in get_service_names(service.get('links', [])) or
                name in get_service_names_from_volumes_from(service.get('volumes_from', [])) or
                name == get_service_name_from_net(service.get('net')))
        ]

    def visit(n):
        if n['name'] in temporary_marked:
            if n['name'] in get_service_names(n.get('links', [])):
                raise DependencyError('A service can not link to itself: %s' % n['name'])
            if n['name'] in n.get('volumes_from', []):
                raise DependencyError('A service can not mount itself as volume: %s' % n['name'])
            else:
                raise DependencyError('Circular import between %s' % ' and '.join(temporary_marked))
        if n in unmarked:
            temporary_marked.add(n['name'])
            for m in get_service_dependents(n, services):
                visit(m)
            temporary_marked.remove(n['name'])
            unmarked.remove(n)
            sorted_services.insert(0, n)

    while unmarked:
        visit(unmarked[-1])

    return sorted_services


class Project(object):
    """
    A collection of services.
    """
    def __init__(self, name, services, client, use_networking=False, network_driver=None):
        self.name = name
        self.services = services
        self.client = client
        self.use_networking = use_networking
        self.network_driver = network_driver or 'bridge'

    def labels(self, one_off=False):
        return [
            '{0}={1}'.format(LABEL_PROJECT, self.name),
            '{0}={1}'.format(LABEL_ONE_OFF, "True" if one_off else "False"),
        ]

    @classmethod
    def from_dicts(cls, name, service_dicts, client, use_networking=False, network_driver=None):
        """
        Construct a ServiceCollection from a list of dicts representing services.
        """
        project = cls(name, [], client, use_networking=use_networking, network_driver=network_driver)

        if use_networking:
            remove_links(service_dicts)

        for service_dict in sort_service_dicts(service_dicts):
            links = project.get_links(service_dict)
            volumes_from = project.get_volumes_from(service_dict)
            net = project.get_net(service_dict)

            project.services.append(
                Service(
                    client=client,
                    project=name,
                    use_networking=use_networking,
                    links=links,
                    net=net,
                    volumes_from=volumes_from,
                    **service_dict))
        return project

    @property
    def service_names(self):
        return [service.name for service in self.services]

    def get_service(self, name):
        """
        Retrieve a service by name. Raises NoSuchService
        if the named service does not exist.
        """
        for service in self.services:
            if service.name == name:
                return service

        raise NoSuchService(name)

    def validate_service_names(self, service_names):
        """
        Validate that the given list of service names only contains valid
        services. Raises NoSuchService if one of the names is invalid.
        """
        valid_names = self.service_names
        for name in service_names:
            if name not in valid_names:
                raise NoSuchService(name)

    def get_services(self, service_names=None, include_deps=False):
        """
        Returns a list of this project's services filtered
        by the provided list of names, or all services if service_names is None
        or [].

        If include_deps is specified, returns a list including the dependencies for
        service_names, in order of dependency.

        Preserves the original order of self.services where possible,
        reordering as needed to resolve dependencies.

        Raises NoSuchService if any of the named services do not exist.
        """
        if service_names is None or len(service_names) == 0:
            return self.get_services(
                service_names=self.service_names,
                include_deps=include_deps
            )
        else:
            unsorted = [self.get_service(name) for name in service_names]
            services = [s for s in self.services if s in unsorted]

            if include_deps:
                services = reduce(self._inject_deps, services, [])

            uniques = []
            [uniques.append(s) for s in services if s not in uniques]
            return uniques

    def get_links(self, service_dict):
        links = []
        if 'links' in service_dict:
            for link in service_dict.get('links', []):
                if ':' in link:
                    service_name, link_name = link.split(':', 1)
                else:
                    service_name, link_name = link, None
                try:
                    links.append((self.get_service(service_name), link_name))
                except NoSuchService:
                    raise ConfigurationError(
                        'Service "%s" has a link to service "%s" which does not '
                        'exist.' % (service_dict['name'], service_name))
            del service_dict['links']
        return links

    def get_volumes_from(self, service_dict):
        volumes_from = []
        if 'volumes_from' in service_dict:
            for volume_from_config in service_dict.get('volumes_from', []):
                volume_from_spec = parse_volume_from_spec(volume_from_config)
                # Get service
                try:
                    service_name = self.get_service(volume_from_spec.source)
                    volume_from_spec = VolumeFromSpec(service_name, volume_from_spec.mode)
                except NoSuchService:
                    try:
                        container_name = Container.from_id(self.client, volume_from_spec.source)
                        volume_from_spec = VolumeFromSpec(container_name, volume_from_spec.mode)
                    except APIError:
                        raise ConfigurationError(
                            'Service "%s" mounts volumes from "%s", which is '
                            'not the name of a service or container.' % (
                                service_dict['name'],
                                volume_from_spec.source))
                volumes_from.append(volume_from_spec)
            del service_dict['volumes_from']
        return volumes_from

    def get_net(self, service_dict):
        net = service_dict.pop('net', None)
        if not net:
            if self.use_networking:
                return Net(self.name)
            return Net(None)

        net_name = get_service_name_from_net(net)
        if not net_name:
            return Net(net)

        try:
            return ServiceNet(self.get_service(net_name))
        except NoSuchService:
            pass
        try:
            return ContainerNet(Container.from_id(self.client, net_name))
        except APIError:
            raise ConfigurationError(
                'Service "%s" is trying to use the network of "%s", '
                'which is not the name of a service or container.' % (
                    service_dict['name'],
                    net_name))

    def start(self, service_names=None, **options):
        for service in self.get_services(service_names):
            service.start(**options)

    def stop(self, service_names=None, **options):
        parallel_execute(
            objects=self.containers(service_names),
            obj_callable=lambda c: c.stop(**options),
            msg_index=lambda c: c.name,
            msg="Stopping"
        )

    def pause(self, service_names=None, **options):
        for service in reversed(self.get_services(service_names)):
            service.pause(**options)

    def unpause(self, service_names=None, **options):
        for service in self.get_services(service_names):
            service.unpause(**options)

    def kill(self, service_names=None, **options):
        parallel_execute(
            objects=self.containers(service_names),
            obj_callable=lambda c: c.kill(**options),
            msg_index=lambda c: c.name,
            msg="Killing"
        )

    def remove_stopped(self, service_names=None, **options):
        all_containers = self.containers(service_names, stopped=True)

        networking = {}
        services = self.get_services(service_names)

        manual_net = False

        for service in services:
            manual_net = service.net.mode == 'manual'
            service.remove_duplicate_containers()

            if manual_net:
                service.net = Net(None)
                if os.geteuid == 0:
                    networking.update(Network.parse_options(service.name,
                                                            service.options))

                else:
                    log.error('Not running as root, not setting up extra networking')
                    manual_net = False

                # Pop keywords so that we pass docker validation moving forward
                for keyw in Network.keywords():
                    try:
                        service.options.pop(keyw)
                    except KeyError:
                        pass

        if manual_net:
            # Carve out the networking data
            for container in all_containers:
                if networking.get(container.service):
                    networking[container.service]['short_id'] = container.short_id
                    networking[container.service]['client'] = container.client

            # execute_net_cleanup undoes the network provisioning in up
            net = Network(networking)
            net.execute_net_cleanup()

        stopped_containers = [c for c in all_containers if not c.is_running]
        parallel_execute(
            objects=stopped_containers,
            obj_callable=lambda c: c.remove(**options),
            msg_index=lambda c: c.name,
            msg="Removing"
        )

    def restart(self, service_names=None, **options):
        for service in self.get_services(service_names):
            service.restart(**options)

    def build(self, service_names=None, no_cache=False, pull=False):
        for service in self.get_services(service_names):
            if service.can_be_built():
                service.build(no_cache, pull)
            else:
                log.info('%s uses an image, skipping' % service.name)

    def up(self,
           service_names=None,
           start_deps=True,
           strategy=ConvergenceStrategy.changed,
           do_build=True,
           timeout=DEFAULT_TIMEOUT):

        networking = {}
        services = self.get_services(service_names, include_deps=start_deps)

        # Sanitize net=manual option by renaming it to none:
        manual_net = False

        for service in services:
            service.remove_duplicate_containers()

            manual_net = service.net.mode == 'manual'

            if manual_net:
                # Assign None to service.options['net'] to pass docker
                # validation
                service.net = Net(None)
                if os.geteuid() == 0:
                    # Get all the relevant networking keywords from service
                    networking.update(Network.parse_options(service.name,
                                                            service.options))

                else:
                    log.error('Not running as root, not setting up extra networking')
                    manual_net = False

                # Pop keywords so that we pass docker validation moving forward
                for keyw in Network.keywords():
                    try:
                        service.options.pop(keyw)
                    except KeyError:
                        pass

        plans = self._get_convergence_plans(services, strategy)

        containers = [
            container
            for service in services
            for container in service.execute_convergence_plan(
                plans[service.name],
                do_build=do_build,
                timeout=timeout
            )
        ]

        if manual_net:
            # Once the containers are up, collect the short id and client
            # object to do interface assignments as applicable
            for container in containers:
                if networking.get(container.service):
                    networking[container.service]['short_id'] = container.short_id
                    networking[container.service]['client'] = container.client

            # Only proceed if the configuration validates (note net should be
            # None):
            log.debug("Validating network config and executing convergence")
            if Network.validate_config(networking):
                net = Network(networking)
                net.execute_net_convergence()
                log.info('Setup extra networking configurations')

            else:
                log.warning('Detected invalid networking configuration, not processing extra net configs')

        if self.use_networking:
            self.ensure_network_exists()

        return containers

    def _get_convergence_plans(self, services, strategy):
        plans = {}

        for service in services:
            updated_dependencies = [
                name
                for name in service.get_dependency_names()
                if name in plans
                and plans[name].action == 'recreate'
            ]

            if updated_dependencies and strategy.allows_recreate:
                log.debug('%s has upstream changes (%s)',
                          service.name,
                          ", ".join(updated_dependencies))
                plan = service.convergence_plan(ConvergenceStrategy.always)
            else:
                plan = service.convergence_plan(strategy)

            plans[service.name] = plan

        return plans

    def pull(self, service_names=None, ignore_pull_failures=False):
        for service in self.get_services(service_names, include_deps=False):
            service.pull(ignore_pull_failures)

    def containers(self, service_names=None, stopped=False, one_off=False):
        if service_names:
            self.validate_service_names(service_names)
        else:
            service_names = self.service_names

        containers = list(filter(None, [
            Container.from_ps(self.client, container)
            for container in self.client.containers(
                all=stopped,
                filters={'label': self.labels(one_off=one_off)})]))

        def matches_service_names(container):
            return container.labels.get(LABEL_SERVICE) in service_names

        if not containers:
            check_for_legacy_containers(
                self.client,
                self.name,
                self.service_names,
            )

        return [c for c in containers if matches_service_names(c)]

    def get_network(self):
        networks = self.client.networks(names=[self.name])
        if networks:
            return networks[0]
        return None

    def ensure_network_exists(self):
        # TODO: recreate network if driver has changed?
        if self.get_network() is None:
            log.info(
                'Creating network "{}" with driver "{}"'
                .format(self.name, self.network_driver)
            )
            self.client.create_network(self.name, driver=self.network_driver)

    def remove_network(self):
        network = self.get_network()
        if network:
            self.client.remove_network(network['id'])

    def _inject_deps(self, acc, service):
        dep_names = service.get_dependency_names()

        if len(dep_names) > 0:
            dep_services = self.get_services(
                service_names=list(set(dep_names)),
                include_deps=True
            )
        else:
            dep_services = []

        dep_services.append(service)
        return acc + dep_services


def remove_links(service_dicts):
    services_with_links = [s for s in service_dicts if 'links' in s]
    if not services_with_links:
        return

    if len(services_with_links) == 1:
        prefix = '"{}" defines'.format(services_with_links[0]['name'])
    else:
        prefix = 'Some services ({}) define'.format(
            ", ".join('"{}"'.format(s['name']) for s in services_with_links))

    log.warn(
        '\n{} links, which are not compatible with Docker networking and will be ignored.\n'
        'Future versions of Docker will not support links - you should remove them for '
        'forwards-compatibility.\n'.format(prefix))

    for s in services_with_links:
        del s['links']


class NoSuchService(Exception):
    def __init__(self, name):
        self.name = name
        self.msg = "No such service: %s" % self.name

    def __str__(self):
        return self.msg


class DependencyError(ConfigurationError):
    pass
