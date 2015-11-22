##############################################################################
# Copyright (c) 2015 Ericsson AB and others.
#
# All rights reserved. This program and the accompanying materials
# are made available under the terms of the Apache License, Version 2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0
##############################################################################

""" Heat template and stack management
"""

import os
import time
import datetime
import getpass
import socket
import logging
import pkg_resources
import json
import heatclient.client
import keystoneclient

from yardstick.common import template_format


log = logging.getLogger(__name__)


class HeatObject(object):
    ''' base class for template and stack'''
    def __init__(self):
        self._keystone_client = None
        self._heat_client = None
        self.uuid = None

    def _get_keystone_client(self):
        '''returns a keystone client instance'''

        if self._keystone_client is None:
            self._keystone_client = keystoneclient.v2_0.client.Client(
                auth_url=os.environ.get('OS_AUTH_URL'),
                username=os.environ.get('OS_USERNAME'),
                password=os.environ.get('OS_PASSWORD'),
                tenant_name=os.environ.get('OS_TENANT_NAME'))

        return self._keystone_client

    def _get_heat_client(self):
        '''returns a heat client instance'''

        if self._heat_client is None:
            keystone = self._get_keystone_client()
            heat_endpoint = keystone.service_catalog.url_for(
                service_type='orchestration')
            self._heat_client = heatclient.client.Client(
                '1', endpoint=heat_endpoint, token=keystone.auth_token)

        return self._heat_client

    def status(self):
        '''returns stack state as a string'''
        heat = self._get_heat_client()
        stack = heat.stacks.get(self.uuid)
        return getattr(stack, 'stack_status')


class HeatStack(HeatObject):
    ''' Represents a Heat stack (deployed template) '''
    stacks = []

    def __init__(self, name):
        super(HeatStack, self).__init__()
        self.uuid = None
        self.name = name
        self.outputs = None
        HeatStack.stacks.append(self)

    @staticmethod
    def stacks_exist():
        '''check if any stack has been deployed'''
        return len(HeatStack.stacks) > 0

    def _delete(self):
        '''deletes a stack from the target cloud using heat'''
        if self.uuid is None:
            return

        log.info("Deleting stack '%s', uuid:%s", self.name, self.uuid)
        heat = self._get_heat_client()
        template = heat.stacks.get(self.uuid)
        start_time = time.time()
        template.delete()
        status = self.status()

        while status != u'DELETE_COMPLETE':
            log.debug("stack state %s", status)
            if status == u'DELETE_FAILED':
                raise RuntimeError(
                    heat.stacks.get(self.uuid).stack_status_reason)

            time.sleep(2)
            status = self.status()

        end_time = time.time()
        log.info("Deleted stack '%s' in %d secs", self.name,
                 end_time - start_time)
        self.uuid = None

    def delete(self, block=True, retries=3):
        '''deletes a stack in the target cloud using heat (with retry)
        Sometimes delete fail with "InternalServerError" and the next attempt
        succeeds. So it is worthwhile to test a couple of times.
        '''
        if self.uuid is None:
            return

        if not block:
            self._delete()
            return

        i = 0
        while i < retries:
            try:
                self._delete()
                break
            except RuntimeError as err:
                log.warn(err.args)
                time.sleep(2)
            i += 1

        # if still not deleted try once more and let it fail everything
        if self.uuid is not None:
            self._delete()

        HeatStack.stacks.remove(self)

    @staticmethod
    def delete_all():
        for stack in HeatStack.stacks[:]:
            stack.delete()

    def update(self):
        '''update a stack'''
        raise RuntimeError("not implemented")


class HeatTemplate(HeatObject):
    '''Describes a Heat template and a method to deploy template to a stack'''

    def _init_template(self):
        self._template = {}
        self._template['heat_template_version'] = '2013-05-23'

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._template['description'] = \
            '''Stack built by the yardstick framework for %s on host %s %s.
            All referred generated resources are prefixed with the template
            name (i.e. %s).''' % (getpass.getuser(), socket.gethostname(),
                                  timestamp, self.name)

        # short hand for resources part of template
        self.resources = self._template['resources'] = {}

        self._template['outputs'] = {}

    def __init__(self, name, template_file=None, heat_parameters=None):
        super(HeatTemplate, self).__init__()
        self.name = name
        self.state = "NOT_CREATED"
        self.keystone_client = None
        self.heat_client = None
        self.heat_parameters = {}

        # heat_parameters is passed to heat in stack create, empty dict when
        # yardstick creates the template (no get_param in resources part)
        if heat_parameters:
            self.heat_parameters = heat_parameters

        if template_file:
            with open(template_file) as stream:
                print "Parsing external template:", template_file
                template_str = stream.read()
                self._template = template_format.parse(template_str)
            self._parameters = heat_parameters
        else:
            self._init_template()

        # holds results of requested output after deployment
        self.outputs = {}

        log.debug("template object '%s' created", name)

    def add_network(self, name):
        '''add to the template a Neutron Net'''
        log.debug("adding Neutron::Net '%s'", name)
        self.resources[name] = {
            'type': 'OS::Neutron::Net',
            'properties': {'name': name}
        }

    def add_subnet(self, name, network, cidr):
        '''add to the template a Neutron Subnet'''
        log.debug("adding Neutron::Subnet '%s' in network '%s', cidr '%s'",
                  name, network, cidr)
        self.resources[name] = {
            'type': 'OS::Neutron::Subnet',
            'depends_on': network,
            'properties': {
                'name': name,
                'cidr': cidr,
                'network_id': {'get_resource': network}
            }
        }

        self._template['outputs'][name] = {
            'description': 'subnet %s ID' % name,
            'value': {'get_resource': name}
        }

    def add_router(self, name, ext_gw_net, subnet_name):
        '''add to the template a Neutron Router and interface'''
        log.debug("adding Neutron::Router:'%s', gw-net:'%s'", name, ext_gw_net)

        self.resources[name] = {
            'type': 'OS::Neutron::Router',
            'depends_on': [subnet_name],
            'properties': {
                'name': name,
                'external_gateway_info': {
                    'network': ext_gw_net
                }
            }
        }

    def add_router_interface(self, name, router_name, subnet_name):
        '''add to the template a Neutron RouterInterface and interface'''
        log.debug("adding Neutron::RouterInterface '%s' router:'%s', "
                  "subnet:'%s'", name, router_name, subnet_name)

        self.resources[name] = {
            'type': 'OS::Neutron::RouterInterface',
            'depends_on': [router_name, subnet_name],
            'properties': {
                'router_id': {'get_resource': router_name},
                'subnet_id': {'get_resource': subnet_name}
            }
        }

    def add_port(self, name, network_name, subnet_name, sec_group_id=None):
        '''add to the template a named Neutron Port'''
        log.debug("adding Neutron::Port '%s', network:'%s', subnet:'%s', "
                  "secgroup:%s", name, network_name, subnet_name, sec_group_id)
        self.resources[name] = {
            'type': 'OS::Neutron::Port',
            'depends_on': [subnet_name],
            'properties': {
                'name': name,
                'fixed_ips': [{'subnet': {'get_resource': subnet_name}}],
                'network_id': {'get_resource': network_name},
                'replacement_policy': 'AUTO',
            }
        }

        if sec_group_id:
            self.resources[name]['depends_on'].append(sec_group_id)
            self.resources[name]['properties']['security_groups'] = \
                [sec_group_id]

        self._template['outputs'][name] = {
            'description': 'Address for interface %s' % name,
            'value': {'get_attr': [name, 'fixed_ips', 0, 'ip_address']}
        }

    def add_floating_ip(self, name, network_name, port_name, router_if_name,
                        secgroup_name=None):
        '''add to the template a Neutron FloatingIP resource
        see: https://bugs.launchpad.net/heat/+bug/1299259
        '''
        log.debug("adding Neutron::FloatingIP '%s', network '%s', port '%s', "
                  "rif '%s'", name, network_name, port_name, router_if_name)

        self.resources[name] = {
            'type': 'OS::Neutron::FloatingIP',
            'depends_on': [port_name, router_if_name],
            'properties': {
                'floating_network': network_name,
                'port_id': {'get_resource': port_name}
            }
        }

        if secgroup_name:
            self.resources[name]["depends_on"].append(secgroup_name)

        self._template['outputs'][name] = {
            'description': 'floating ip %s' % name,
            'value': {'get_attr': [name, 'floating_ip_address']}
        }

    def add_keypair(self, name):
        '''add to the template a Nova KeyPair'''
        log.debug("adding Nova::KeyPair '%s'", name)
        self.resources[name] = {
            'type': 'OS::Nova::KeyPair',
            'properties': {
                'name': name,
                'public_key': pkg_resources.resource_string(
                    'yardstick.resources', 'files/yardstick_key.pub')
            }
        }

    def add_servergroup(self, name, policy):
        '''add to the template a Nova ServerGroup'''
        log.debug("adding Nova::ServerGroup '%s', policy '%s'", name, policy)
        if policy not in ["anti-affinity", "affinity"]:
            raise ValueError(policy)

        self.resources[name] = {
            'type': 'OS::Nova::ServerGroup',
            'properties': {
                'name': name,
                'policies': [policy]
            }
        }

        self._template['outputs'][name] = {
            'description': 'ID Server Group %s' % name,
            'value': {'get_resource': name}
        }

    def add_security_group(self, name):
        '''add to the template a Neutron SecurityGroup'''
        log.debug("adding Neutron::SecurityGroup '%s'", name)
        self.resources[name] = {
            'type': 'OS::Neutron::SecurityGroup',
            'properties': {
                'name': name,
                'description': "Group allowing icmp and upd/tcp on all ports",
                'rules': [
                    {'remote_ip_prefix': '0.0.0.0/0',
                     'protocol': 'tcp'},
                    {'remote_ip_prefix': '0.0.0.0/0',
                     'protocol': 'udp'},
                    {'remote_ip_prefix': '0.0.0.0/0',
                     'protocol': 'icmp'}
                ]
            }
        }

        self._template['outputs'][name] = {
            'description': 'ID of Security Group',
            'value': {'get_resource': name}
        }

    def add_server(self, name, image, flavor, ports=None, networks=None,
                   scheduler_hints=None, key_name=None, user_data=None,
                   metadata=None, additional_properties=None):
        '''add to the template a Nova Server'''
        log.debug("adding Nova::Server '%s', image '%s', flavor '%s', "
                  "ports %s", name, image, flavor, ports)

        self.resources[name] = {
            'type': 'OS::Nova::Server'
        }

        server_properties = {
            'name': name,
            'image': image,
            'flavor': flavor,
            'networks': []  # list of dictionaries
        }

        if key_name:
            self.resources[name]['depends_on'] = [key_name]
            server_properties['key_name'] = {'get_resource': key_name}

        if ports:
            self.resources[name]['depends_on'] = ports

            for port in ports:
                server_properties['networks'].append(
                    {'port': {'get_resource': port}}
                )

        if networks:
            for i in range(len(networks)):
                server_properties['networks'].append({'network': networks[i]})

        if scheduler_hints:
            server_properties['scheduler_hints'] = scheduler_hints

        if user_data:
            server_properties['user_data'] = user_data

        if metadata:
            assert type(metadata) is dict
            server_properties['metadata'] = metadata

        if additional_properties:
            assert type(additional_properties) is dict
            for prop in additional_properties:
                server_properties[prop] = additional_properties[prop]

        server_properties['config_drive'] = True

        self.resources[name]['properties'] = server_properties

        self._template['outputs'][name] = {
            'description': 'VM UUID',
            'value': {'get_resource': name}
        }

    def create(self, block=True):
        '''creates a template in the target cloud using heat
        returns a dict with the requested output values from the template'''
        log.info("Creating stack '%s'", self.name)

        # create stack early to support cleanup, e.g. ctrl-c while waiting
        stack = HeatStack(self.name)

        heat = self._get_heat_client()
        json_template = json.dumps(self._template)
        start_time = time.time()
        stack.uuid = self.uuid = heat.stacks.create(
            stack_name=self.name, template=json_template,
            parameters=self.heat_parameters)['stack']['id']

        status = self.status()

        if block:
            while status != u'CREATE_COMPLETE':
                log.debug("stack state %s", status)
                if status == u'CREATE_FAILED':
                    raise RuntimeError(getattr(heat.stacks.get(self.uuid),
                                               'stack_status_reason'))

                time.sleep(2)
                status = self.status()

            end_time = time.time()
            outputs = getattr(heat.stacks.get(self.uuid), 'outputs')

        for output in outputs:
            self.outputs[output["output_key"].encode("ascii")] = \
                output["output_value"].encode("ascii")

        log.info("Created stack '%s' in %d secs",
                 self.name, end_time - start_time)

        stack.outputs = self.outputs
        return stack
