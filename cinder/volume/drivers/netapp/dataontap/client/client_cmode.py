# Copyright (c) 2014 Alex Meade.  All rights reserved.
# Copyright (c) 2014 Clinton Knight.  All rights reserved.
# Copyright (c) 2015 Tom Barron.  All rights reserved.
# Copyright (c) 2016 Mike Rooney. All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import copy
import math
import re

from oslo_log import log as logging
import six

from cinder import exception
from cinder.i18n import _, _LW
from cinder import utils
from cinder.volume.drivers.netapp.dataontap.client import api as netapp_api
from cinder.volume.drivers.netapp.dataontap.client import client_base
from cinder.volume.drivers.netapp import utils as na_utils

from oslo_utils import strutils


LOG = logging.getLogger(__name__)
DELETED_PREFIX = 'deleted_cinder_'
DEFAULT_MAX_PAGE_LENGTH = 50


@six.add_metaclass(utils.TraceWrapperMetaclass)
class Client(client_base.Client):

    def __init__(self, **kwargs):
        super(Client, self).__init__(**kwargs)
        self.vserver = kwargs.get('vserver', None)
        self.connection.set_vserver(self.vserver)

        # Default values to run first api
        self.connection.set_api_version(1, 15)
        (major, minor) = self.get_ontapi_version(cached=False)
        self.connection.set_api_version(major, minor)
        self._init_features()

    def _init_features(self):
        super(Client, self)._init_features()

        ontapi_version = self.get_ontapi_version()   # major, minor

        ontapi_1_20 = ontapi_version >= (1, 20)
        ontapi_1_2x = (1, 20) <= ontapi_version < (1, 30)
        ontapi_1_30 = ontapi_version >= (1, 30)

        self.features.add_feature('USER_CAPABILITY_LIST',
                                  supported=ontapi_1_20)
        self.features.add_feature('SYSTEM_METRICS', supported=ontapi_1_2x)
        self.features.add_feature('FAST_CLONE_DELETE', supported=ontapi_1_30)
        self.features.add_feature('SYSTEM_CONSTITUENT_METRICS',
                                  supported=ontapi_1_30)

    def _invoke_vserver_api(self, na_element, vserver):
        server = copy.copy(self.connection)
        server.set_vserver(vserver)
        result = server.invoke_successfully(na_element, True)
        return result

    def _has_records(self, api_result_element):
        num_records = api_result_element.get_child_content('num-records')
        return bool(num_records and '0' != num_records)

    def _get_record_count(self, api_result_element):
        try:
            return int(api_result_element.get_child_content('num-records'))
        except TypeError:
            msg = _('Missing record count for NetApp iterator API invocation.')
            raise exception.NetAppDriverException(msg)

    def set_vserver(self, vserver):
        self.connection.set_vserver(vserver)

    def send_iter_request(self, api_name, api_args=None, enable_tunneling=True,
                          max_page_length=DEFAULT_MAX_PAGE_LENGTH):
        """Invoke an iterator-style getter API."""

        if not api_args:
            api_args = {}

        api_args['max-records'] = max_page_length

        # Get first page
        result = self.send_request(
            api_name, api_args, enable_tunneling=enable_tunneling)

        # Most commonly, we can just return here if there is no more data
        next_tag = result.get_child_content('next-tag')
        if not next_tag:
            return result

        # Ensure pagination data is valid and prepare to store remaining pages
        num_records = self._get_record_count(result)
        attributes_list = result.get_child_by_name('attributes-list')
        if not attributes_list:
            msg = _('Missing attributes list for API %s.') % api_name
            raise exception.NetAppDriverException(msg)

        # Get remaining pages, saving data into first page
        while next_tag is not None:
            next_api_args = copy.deepcopy(api_args)
            next_api_args['tag'] = next_tag
            next_result = self.send_request(
                api_name, next_api_args, enable_tunneling=enable_tunneling)

            next_attributes_list = next_result.get_child_by_name(
                'attributes-list') or netapp_api.NaElement('none')

            for record in next_attributes_list.get_children():
                attributes_list.add_child_elem(record)

            num_records += self._get_record_count(next_result)
            next_tag = next_result.get_child_content('next-tag')

        result.get_child_by_name('num-records').set_content(
            six.text_type(num_records))
        result.get_child_by_name('next-tag').set_content('')
        return result

    def get_iscsi_target_details(self):
        """Gets the iSCSI target portal details."""
        iscsi_if_iter = netapp_api.NaElement('iscsi-interface-get-iter')
        result = self.connection.invoke_successfully(iscsi_if_iter, True)
        tgt_list = []
        num_records = result.get_child_content('num-records')
        if num_records and int(num_records) >= 1:
            attr_list = result.get_child_by_name('attributes-list')
            iscsi_if_list = attr_list.get_children()
            for iscsi_if in iscsi_if_list:
                d = dict()
                d['address'] = iscsi_if.get_child_content('ip-address')
                d['port'] = iscsi_if.get_child_content('ip-port')
                d['tpgroup-tag'] = iscsi_if.get_child_content('tpgroup-tag')
                d['interface-enabled'] = iscsi_if.get_child_content(
                    'is-interface-enabled')
                tgt_list.append(d)
        return tgt_list

    def set_iscsi_chap_authentication(self, iqn, username, password):
        """Provides NetApp host's CHAP credentials to the backend."""
        initiator_exists = self.check_iscsi_initiator_exists(iqn)

        command_template = ('iscsi security %(mode)s -vserver %(vserver)s '
                            '-initiator-name %(iqn)s -auth-type CHAP '
                            '-user-name %(username)s')

        if initiator_exists:
            LOG.debug('Updating CHAP authentication for %(iqn)s.',
                      {'iqn': iqn})
            command = command_template % {
                'mode': 'modify',
                'vserver': self.vserver,
                'iqn': iqn,
                'username': username,
            }
        else:
            LOG.debug('Adding initiator %(iqn)s with CHAP authentication.',
                      {'iqn': iqn})
            command = command_template % {
                'mode': 'create',
                'vserver': self.vserver,
                'iqn': iqn,
                'username': username,
            }

        try:
            with self.ssh_client.ssh_connect_semaphore:
                ssh_pool = self.ssh_client.ssh_pool
                with ssh_pool.item() as ssh:
                    self.ssh_client.execute_command_with_prompt(ssh,
                                                                command,
                                                                'Password:',
                                                                password)
        except Exception as e:
            msg = _('Failed to set CHAP authentication for target IQN %(iqn)s.'
                    ' Details: %(ex)s') % {
                'iqn': iqn,
                'ex': e,
            }
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def check_iscsi_initiator_exists(self, iqn):
        """Returns True if initiator exists."""
        initiator_exists = True
        try:
            auth_list = netapp_api.NaElement('iscsi-initiator-get-auth')
            auth_list.add_new_child('initiator', iqn)
            self.connection.invoke_successfully(auth_list, True)
        except netapp_api.NaApiError:
            initiator_exists = False

        return initiator_exists

    def get_fc_target_wwpns(self):
        """Gets the FC target details."""
        wwpns = []
        port_name_list_api = netapp_api.NaElement('fcp-port-name-get-iter')
        port_name_list_api.add_new_child('max-records', '100')
        result = self.connection.invoke_successfully(port_name_list_api, True)
        num_records = result.get_child_content('num-records')
        if num_records and int(num_records) >= 1:
            for port_name_info in result.get_child_by_name(
                    'attributes-list').get_children():

                if port_name_info.get_child_content('is-used') != 'true':
                    continue

                wwpn = port_name_info.get_child_content('port-name').lower()
                wwpns.append(wwpn)

        return wwpns

    def get_iscsi_service_details(self):
        """Returns iscsi iqn."""
        iscsi_service_iter = netapp_api.NaElement('iscsi-service-get-iter')
        result = self.connection.invoke_successfully(iscsi_service_iter, True)
        if result.get_child_content('num-records') and\
                int(result.get_child_content('num-records')) >= 1:
            attr_list = result.get_child_by_name('attributes-list')
            iscsi_service = attr_list.get_child_by_name('iscsi-service-info')
            return iscsi_service.get_child_content('node-name')
        LOG.debug('No iSCSI service found for vserver %s', self.vserver)
        return None

    def get_lun_list(self):
        """Gets the list of LUNs on filer.

        Gets the LUNs from cluster with vserver.
        """

        luns = []
        tag = None
        while True:
            api = netapp_api.NaElement('lun-get-iter')
            api.add_new_child('max-records', '100')
            if tag:
                api.add_new_child('tag', tag, True)
            lun_info = netapp_api.NaElement('lun-info')
            lun_info.add_new_child('vserver', self.vserver)
            query = netapp_api.NaElement('query')
            query.add_child_elem(lun_info)
            api.add_child_elem(query)
            result = self.connection.invoke_successfully(api, True)
            if result.get_child_by_name('num-records') and\
                    int(result.get_child_content('num-records')) >= 1:
                attr_list = result.get_child_by_name('attributes-list')
                luns.extend(attr_list.get_children())
            tag = result.get_child_content('next-tag')
            if tag is None:
                break
        return luns

    def get_lun_map(self, path):
        """Gets the LUN map by LUN path."""
        tag = None
        map_list = []
        while True:
            lun_map_iter = netapp_api.NaElement('lun-map-get-iter')
            lun_map_iter.add_new_child('max-records', '100')
            if tag:
                lun_map_iter.add_new_child('tag', tag, True)
            query = netapp_api.NaElement('query')
            lun_map_iter.add_child_elem(query)
            query.add_node_with_children('lun-map-info', **{'path': path})
            result = self.connection.invoke_successfully(lun_map_iter, True)
            tag = result.get_child_content('next-tag')
            if result.get_child_content('num-records') and \
                    int(result.get_child_content('num-records')) >= 1:
                attr_list = result.get_child_by_name('attributes-list')
                lun_maps = attr_list.get_children()
                for lun_map in lun_maps:
                    lun_m = dict()
                    lun_m['initiator-group'] = lun_map.get_child_content(
                        'initiator-group')
                    lun_m['lun-id'] = lun_map.get_child_content('lun-id')
                    lun_m['vserver'] = lun_map.get_child_content('vserver')
                    map_list.append(lun_m)
            if tag is None:
                break
        return map_list

    def _get_igroup_by_initiator_query(self, initiator, tag):
        igroup_get_iter = netapp_api.NaElement('igroup-get-iter')
        igroup_get_iter.add_new_child('max-records', '100')
        if tag:
            igroup_get_iter.add_new_child('tag', tag, True)

        query = netapp_api.NaElement('query')
        igroup_info = netapp_api.NaElement('initiator-group-info')
        query.add_child_elem(igroup_info)
        igroup_info.add_new_child('vserver', self.vserver)
        initiators = netapp_api.NaElement('initiators')
        igroup_info.add_child_elem(initiators)
        igroup_get_iter.add_child_elem(query)
        initiators.add_node_with_children(
            'initiator-info', **{'initiator-name': initiator})

        # limit results to just the attributes of interest
        desired_attrs = netapp_api.NaElement('desired-attributes')
        desired_igroup_info = netapp_api.NaElement('initiator-group-info')
        desired_igroup_info.add_node_with_children(
            'initiators', **{'initiator-info': None})
        desired_igroup_info.add_new_child('vserver', None)
        desired_igroup_info.add_new_child('initiator-group-name', None)
        desired_igroup_info.add_new_child('initiator-group-type', None)
        desired_igroup_info.add_new_child('initiator-group-os-type', None)
        desired_attrs.add_child_elem(desired_igroup_info)
        igroup_get_iter.add_child_elem(desired_attrs)

        return igroup_get_iter

    def get_igroup_by_initiators(self, initiator_list):
        """Get igroups exactly matching a set of initiators."""
        tag = None
        igroup_list = []
        if not initiator_list:
            return igroup_list

        initiator_set = set(initiator_list)

        while True:
            # C-mode getter APIs can't do an 'and' query, so match the first
            # initiator (which will greatly narrow the search results) and
            # filter the rest in this method.
            query = self._get_igroup_by_initiator_query(initiator_list[0], tag)
            result = self.connection.invoke_successfully(query, True)

            tag = result.get_child_content('next-tag')
            num_records = result.get_child_content('num-records')
            if num_records and int(num_records) >= 1:

                for igroup_info in result.get_child_by_name(
                        'attributes-list').get_children():

                    initiator_set_for_igroup = set()
                    for initiator_info in igroup_info.get_child_by_name(
                            'initiators').get_children():

                        initiator_set_for_igroup.add(
                            initiator_info.get_child_content('initiator-name'))

                    if initiator_set == initiator_set_for_igroup:
                        igroup = {'initiator-group-os-type':
                                  igroup_info.get_child_content(
                                      'initiator-group-os-type'),
                                  'initiator-group-type':
                                  igroup_info.get_child_content(
                                      'initiator-group-type'),
                                  'initiator-group-name':
                                  igroup_info.get_child_content(
                                      'initiator-group-name')}
                        igroup_list.append(igroup)

            if tag is None:
                break

        return igroup_list

    def clone_lun(self, volume, name, new_name, space_reserved='true',
                  qos_policy_group_name=None, src_block=0, dest_block=0,
                  block_count=0, source_snapshot=None):
        # zAPI can only handle 2^24 blocks per range
        bc_limit = 2 ** 24  # 8GB
        # zAPI can only handle 32 block ranges per call
        br_limit = 32
        z_limit = br_limit * bc_limit  # 256 GB
        z_calls = int(math.ceil(block_count / float(z_limit)))
        zbc = block_count
        if z_calls == 0:
            z_calls = 1
        for _call in range(0, z_calls):
            if zbc > z_limit:
                block_count = z_limit
                zbc -= z_limit
            else:
                block_count = zbc

            zapi_args = {
                'volume': volume,
                'source-path': name,
                'destination-path': new_name,
                'space-reserve': space_reserved,
            }
            if source_snapshot:
                zapi_args['snapshot-name'] = source_snapshot
            clone_create = netapp_api.NaElement.create_node_with_children(
                'clone-create', **zapi_args)
            if qos_policy_group_name is not None:
                clone_create.add_new_child('qos-policy-group-name',
                                           qos_policy_group_name)
            if block_count > 0:
                block_ranges = netapp_api.NaElement("block-ranges")
                segments = int(math.ceil(block_count / float(bc_limit)))
                bc = block_count
                for _segment in range(0, segments):
                    if bc > bc_limit:
                        block_count = bc_limit
                        bc -= bc_limit
                    else:
                        block_count = bc
                    block_range =\
                        netapp_api.NaElement.create_node_with_children(
                            'block-range',
                            **{'source-block-number':
                               six.text_type(src_block),
                               'destination-block-number':
                               six.text_type(dest_block),
                               'block-count':
                               six.text_type(block_count)})
                    block_ranges.add_child_elem(block_range)
                    src_block += int(block_count)
                    dest_block += int(block_count)
                clone_create.add_child_elem(block_ranges)
            self.connection.invoke_successfully(clone_create, True)

    def get_lun_by_args(self, **args):
        """Retrieves LUN with specified args."""
        lun_iter = netapp_api.NaElement('lun-get-iter')
        lun_iter.add_new_child('max-records', '100')
        query = netapp_api.NaElement('query')
        lun_iter.add_child_elem(query)
        query.add_node_with_children('lun-info', **args)
        luns = self.connection.invoke_successfully(lun_iter, True)
        attr_list = luns.get_child_by_name('attributes-list')
        if not attr_list:
            return []
        return attr_list.get_children()

    def file_assign_qos(self, flex_vol, qos_policy_group_name, file_path):
        """Assigns the named QoS policy-group to a file."""
        api_args = {
            'volume': flex_vol,
            'qos-policy-group-name': qos_policy_group_name,
            'file': file_path,
            'vserver': self.vserver,
        }
        return self.send_request('file-assign-qos', api_args, False)

    def provision_qos_policy_group(self, qos_policy_group_info):
        """Create QOS policy group on the backend if appropriate."""
        if qos_policy_group_info is None:
            return

        # Legacy QOS uses externally provisioned QOS policy group,
        # so we don't need to create one on the backend.
        legacy = qos_policy_group_info.get('legacy')
        if legacy is not None:
            return

        spec = qos_policy_group_info.get('spec')
        if spec is not None:
            if not self.qos_policy_group_exists(spec['policy_name']):
                self.qos_policy_group_create(spec['policy_name'],
                                             spec['max_throughput'])
            else:
                self.qos_policy_group_modify(spec['policy_name'],
                                             spec['max_throughput'])

    def qos_policy_group_exists(self, qos_policy_group_name):
        """Checks if a QOS policy group exists."""
        api_args = {
            'query': {
                'qos-policy-group-info': {
                    'policy-group': qos_policy_group_name,
                },
            },
            'desired-attributes': {
                'qos-policy-group-info': {
                    'policy-group': None,
                },
            },
        }
        result = self.send_request('qos-policy-group-get-iter',
                                   api_args,
                                   False)
        return self._has_records(result)

    def qos_policy_group_create(self, qos_policy_group_name, max_throughput):
        """Creates a QOS policy group."""
        api_args = {
            'policy-group': qos_policy_group_name,
            'max-throughput': max_throughput,
            'vserver': self.vserver,
        }
        return self.send_request('qos-policy-group-create', api_args, False)

    def qos_policy_group_modify(self, qos_policy_group_name, max_throughput):
        """Modifies a QOS policy group."""
        api_args = {
            'policy-group': qos_policy_group_name,
            'max-throughput': max_throughput,
        }
        return self.send_request('qos-policy-group-modify', api_args, False)

    def qos_policy_group_delete(self, qos_policy_group_name):
        """Attempts to delete a QOS policy group."""
        api_args = {'policy-group': qos_policy_group_name}
        return self.send_request('qos-policy-group-delete', api_args, False)

    def qos_policy_group_rename(self, qos_policy_group_name, new_name):
        """Renames a QOS policy group."""
        api_args = {
            'policy-group-name': qos_policy_group_name,
            'new-name': new_name,
        }
        return self.send_request('qos-policy-group-rename', api_args, False)

    def mark_qos_policy_group_for_deletion(self, qos_policy_group_info):
        """Do (soft) delete of backing QOS policy group for a cinder volume."""
        if qos_policy_group_info is None:
            return

        spec = qos_policy_group_info.get('spec')

        # For cDOT we want to delete the QoS policy group that we created for
        # this cinder volume.  Because the QoS policy may still be "in use"
        # after the zapi call to delete the volume itself returns successfully,
        # we instead rename the QoS policy group using a specific pattern and
        # later attempt on a best effort basis to delete any QoS policy groups
        # matching that pattern.
        if spec is not None:
            current_name = spec['policy_name']
            new_name = DELETED_PREFIX + current_name
            try:
                self.qos_policy_group_rename(current_name, new_name)
            except netapp_api.NaApiError as ex:
                msg = _LW('Rename failure in cleanup of cDOT QOS policy group '
                          '%(name)s: %(ex)s')
                LOG.warning(msg, {'name': current_name, 'ex': ex})

        # Attempt to delete any QoS policies named "delete-openstack-*".
        self.remove_unused_qos_policy_groups()

    def remove_unused_qos_policy_groups(self):
        """Deletes all QOS policy groups that are marked for deletion."""
        api_args = {
            'query': {
                'qos-policy-group-info': {
                    'policy-group': '%s*' % DELETED_PREFIX,
                    'vserver': self.vserver,
                }
            },
            'max-records': 3500,
            'continue-on-failure': 'true',
            'return-success-list': 'false',
            'return-failure-list': 'false',
        }

        try:
            self.send_request('qos-policy-group-delete-iter', api_args, False)
        except netapp_api.NaApiError as ex:
            msg = 'Could not delete QOS policy groups. Details: %(ex)s'
            msg_args = {'ex': ex}
            LOG.debug(msg % msg_args)

    def set_lun_qos_policy_group(self, path, qos_policy_group):
        """Sets qos_policy_group on a LUN."""
        api_args = {
            'path': path,
            'qos-policy-group': qos_policy_group,
        }
        return self.send_request('lun-set-qos-policy-group', api_args)

    def get_if_info_by_ip(self, ip):
        """Gets the network interface info by ip."""
        net_if_iter = netapp_api.NaElement('net-interface-get-iter')
        net_if_iter.add_new_child('max-records', '10')
        query = netapp_api.NaElement('query')
        net_if_iter.add_child_elem(query)
        query.add_node_with_children(
            'net-interface-info',
            **{'address': na_utils.resolve_hostname(ip)})
        result = self.connection.invoke_successfully(net_if_iter, True)
        num_records = result.get_child_content('num-records')
        if num_records and int(num_records) >= 1:
            attr_list = result.get_child_by_name('attributes-list')
            return attr_list.get_children()
        raise exception.NotFound(
            _('No interface found on cluster for ip %s') % ip)

    def get_vol_by_junc_vserver(self, vserver, junction):
        """Gets the volume by junction path and vserver."""
        vol_iter = netapp_api.NaElement('volume-get-iter')
        vol_iter.add_new_child('max-records', '10')
        query = netapp_api.NaElement('query')
        vol_iter.add_child_elem(query)
        vol_attrs = netapp_api.NaElement('volume-attributes')
        query.add_child_elem(vol_attrs)
        vol_attrs.add_node_with_children(
            'volume-id-attributes',
            **{'junction-path': junction,
               'owning-vserver-name': vserver})
        des_attrs = netapp_api.NaElement('desired-attributes')
        des_attrs.add_node_with_children('volume-attributes',
                                         **{'volume-id-attributes': None})
        vol_iter.add_child_elem(des_attrs)
        result = self._invoke_vserver_api(vol_iter, vserver)
        num_records = result.get_child_content('num-records')
        if num_records and int(num_records) >= 1:
            attr_list = result.get_child_by_name('attributes-list')
            vols = attr_list.get_children()
            vol_id = vols[0].get_child_by_name('volume-id-attributes')
            return vol_id.get_child_content('name')
        msg_fmt = {'vserver': vserver, 'junction': junction}
        raise exception.NotFound(_("No volume on cluster with vserver "
                                   "%(vserver)s and junction path "
                                   "%(junction)s ") % msg_fmt)

    def clone_file(self, flex_vol, src_path, dest_path, vserver,
                   dest_exists=False):
        """Clones file on vserver."""
        LOG.debug("Cloning with params volume %(volume)s, src %(src_path)s, "
                  "dest %(dest_path)s, vserver %(vserver)s",
                  {'volume': flex_vol, 'src_path': src_path,
                   'dest_path': dest_path, 'vserver': vserver})
        clone_create = netapp_api.NaElement.create_node_with_children(
            'clone-create',
            **{'volume': flex_vol, 'source-path': src_path,
               'destination-path': dest_path})
        major, minor = self.connection.get_api_version()
        if major == 1 and minor >= 20 and dest_exists:
            clone_create.add_new_child('destination-exists', 'true')
        self._invoke_vserver_api(clone_create, vserver)

    def get_file_usage(self, path, vserver):
        """Gets the file unique bytes."""
        LOG.debug('Getting file usage for %s', path)
        file_use = netapp_api.NaElement.create_node_with_children(
            'file-usage-get', **{'path': path})
        res = self._invoke_vserver_api(file_use, vserver)
        unique_bytes = res.get_child_content('unique-bytes')
        LOG.debug('file-usage for path %(path)s is %(bytes)s',
                  {'path': path, 'bytes': unique_bytes})
        return unique_bytes

    def get_vserver_ips(self, vserver):
        """Get ips for the vserver."""
        result = netapp_api.invoke_api(
            self.connection, api_name='net-interface-get-iter',
            is_iter=True, tunnel=vserver)
        if_list = []
        for res in result:
            records = res.get_child_content('num-records')
            if records > 0:
                attr_list = res['attributes-list']
                ifs = attr_list.get_children()
                if_list.extend(ifs)
        return if_list

    def check_cluster_api(self, object_name, operation_name, api):
        """Checks the availability of a cluster API.

        Returns True if the specified cluster API exists and may be called by
        the current user. The API is *called* on Data ONTAP versions prior to
        8.2, while versions starting with 8.2 utilize an API designed for
        this purpose.
        """

        if not self.features.USER_CAPABILITY_LIST:
            return self._check_cluster_api_legacy(api)
        else:
            return self._check_cluster_api(object_name, operation_name, api)

    def _check_cluster_api(self, object_name, operation_name, api):
        """Checks the availability of a cluster API.

        Returns True if the specified cluster API exists and may be called by
        the current user.  This method assumes Data ONTAP 8.2 or higher.
        """

        api_args = {
            'query': {
                'capability-info': {
                    'object-name': object_name,
                    'operation-list': {
                        'operation-info': {
                            'name': operation_name,
                        },
                    },
                },
            },
            'desired-attributes': {
                'capability-info': {
                    'operation-list': {
                        'operation-info': {
                            'api-name': None,
                        },
                    },
                },
            },
        }
        result = self.send_request(
            'system-user-capability-get-iter', api_args, False)

        if not self._has_records(result):
            return False

        capability_info_list = result.get_child_by_name(
            'attributes-list') or netapp_api.NaElement('none')

        for capability_info in capability_info_list.get_children():

            operation_list = capability_info.get_child_by_name(
                'operation-list') or netapp_api.NaElement('none')

            for operation_info in operation_list.get_children():
                api_name = operation_info.get_child_content('api-name') or ''
                api_names = api_name.split(',')
                if api in api_names:
                    return True

        return False

    def _check_cluster_api_legacy(self, api):
        """Checks the availability of a cluster API.

        Returns True if the specified cluster API exists and may be called by
        the current user.  This method should only be used for Data ONTAP 8.1,
        and only getter APIs may be tested because the API is actually called
        to perform the check.
        """

        if not re.match(".*-get$|.*-get-iter$|.*-list-info$", api):
            raise ValueError(_('Non-getter API passed to API test method.'))

        try:
            self.send_request(api, enable_tunneling=False)
        except netapp_api.NaApiError as ex:
            if ex.code in (netapp_api.EAPIPRIVILEGE, netapp_api.EAPINOTFOUND):
                return False

        return True

    def get_operational_network_interface_addresses(self):
        """Gets the IP addresses of operational LIFs on the vserver."""

        api_args = {
            'query': {
                'net-interface-info': {
                    'operational-status': 'up'
                }
            },
            'desired-attributes': {
                'net-interface-info': {
                    'address': None,
                }
            }
        }
        result = self.send_request('net-interface-get-iter', api_args)

        lif_info_list = result.get_child_by_name(
            'attributes-list') or netapp_api.NaElement('none')

        return [lif_info.get_child_content('address') for lif_info in
                lif_info_list.get_children()]

    def get_flexvol_capacity(self, flexvol_path=None, flexvol_name=None):
        """Gets total capacity and free capacity, in bytes, of the flexvol."""

        volume_id_attributes = {}
        if flexvol_path:
            volume_id_attributes['junction-path'] = flexvol_path
        if flexvol_name:
            volume_id_attributes['name'] = flexvol_name

        api_args = {
            'query': {
                'volume-attributes': {
                    'volume-id-attributes': volume_id_attributes,
                }
            },
            'desired-attributes': {
                'volume-attributes': {
                    'volume-space-attributes': {
                        'size-available': None,
                        'size-total': None,
                    }
                }
            },
        }

        result = self.send_request('volume-get-iter', api_args)
        if self._get_record_count(result) != 1:
            msg = _('Volume %s not found.')
            msg_args = flexvol_path or flexvol_name
            raise exception.NetAppDriverException(msg % msg_args)

        attributes_list = result.get_child_by_name('attributes-list')
        volume_attributes = attributes_list.get_child_by_name(
            'volume-attributes')
        volume_space_attributes = volume_attributes.get_child_by_name(
            'volume-space-attributes')

        size_available = float(
            volume_space_attributes.get_child_content('size-available'))
        size_total = float(
            volume_space_attributes.get_child_content('size-total'))

        return {
            'size-total': size_total,
            'size-available': size_available,
        }

    @utils.trace_method
    def delete_file(self, path_to_file):
        """Delete file at path."""

        api_args = {
            'path': path_to_file,
        }
        # Use fast clone deletion engine if it is supported.
        if self.features.FAST_CLONE_DELETE:
            api_args['is-clone-file'] = 'true'
        self.send_request('file-delete-file', api_args, True)

    def _get_aggregates(self, aggregate_names=None, desired_attributes=None):

        query = {
            'aggr-attributes': {
                'aggregate-name': '|'.join(aggregate_names),
            }
        } if aggregate_names else None

        api_args = {}
        if query:
            api_args['query'] = query
        if desired_attributes:
            api_args['desired-attributes'] = desired_attributes

        result = self.send_request('aggr-get-iter',
                                   api_args,
                                   enable_tunneling=False)
        if not self._has_records(result):
            return []
        else:
            return result.get_child_by_name('attributes-list').get_children()

    def get_node_for_aggregate(self, aggregate_name):
        """Get home node for the specified aggregate.

        This API could return None, most notably if it was sent
        to a Vserver LIF, so the caller must be able to handle that case.
        """

        if not aggregate_name:
            return None

        desired_attributes = {
            'aggr-attributes': {
                'aggregate-name': None,
                'aggr-ownership-attributes': {
                    'home-name': None,
                },
            },
        }

        try:
            aggrs = self._get_aggregates(aggregate_names=[aggregate_name],
                                         desired_attributes=desired_attributes)
        except netapp_api.NaApiError as e:
            if e.code == netapp_api.EAPINOTFOUND:
                return None
            else:
                raise e

        if len(aggrs) < 1:
            return None

        aggr_ownership_attrs = aggrs[0].get_child_by_name(
            'aggr-ownership-attributes') or netapp_api.NaElement('none')
        return aggr_ownership_attrs.get_child_content('home-name')

    def get_performance_instance_uuids(self, object_name, node_name):
        """Get UUIDs of performance instances for a cluster node."""

        api_args = {
            'objectname': object_name,
            'query': {
                'instance-info': {
                    'uuid': node_name + ':*',
                }
            }
        }

        result = self.send_request('perf-object-instance-list-info-iter',
                                   api_args,
                                   enable_tunneling=False)

        uuids = []

        instances = result.get_child_by_name(
            'attributes-list') or netapp_api.NaElement('None')

        for instance_info in instances.get_children():
            uuids.append(instance_info.get_child_content('uuid'))

        return uuids

    def get_performance_counters(self, object_name, instance_uuids,
                                 counter_names):
        """Gets or or more cDOT performance counters."""

        api_args = {
            'objectname': object_name,
            'instance-uuids': [
                {'instance-uuid': instance_uuid}
                for instance_uuid in instance_uuids
            ],
            'counters': [
                {'counter': counter} for counter in counter_names
            ],
        }

        result = self.send_request('perf-object-get-instances',
                                   api_args,
                                   enable_tunneling=False)

        counter_data = []

        timestamp = result.get_child_content('timestamp')

        instances = result.get_child_by_name(
            'instances') or netapp_api.NaElement('None')
        for instance in instances.get_children():

            instance_name = instance.get_child_content('name')
            instance_uuid = instance.get_child_content('uuid')
            node_name = instance_uuid.split(':')[0]

            counters = instance.get_child_by_name(
                'counters') or netapp_api.NaElement('None')
            for counter in counters.get_children():

                counter_name = counter.get_child_content('name')
                counter_value = counter.get_child_content('value')

                counter_data.append({
                    'instance-name': instance_name,
                    'instance-uuid': instance_uuid,
                    'node-name': node_name,
                    'timestamp': timestamp,
                    counter_name: counter_value,
                })

        return counter_data

    def get_snapshot(self, volume_name, snapshot_name):
        """Gets a single snapshot."""
        api_args = {
            'query': {
                'snapshot-info': {
                    'name': snapshot_name,
                    'volume': volume_name,
                },
            },
            'desired-attributes': {
                'snapshot-info': {
                    'name': None,
                    'volume': None,
                    'busy': None,
                    'snapshot-owners-list': {
                        'snapshot-owner': None,
                    }
                },
            },
        }
        result = self.send_request('snapshot-get-iter', api_args)

        self._handle_get_snapshot_return_failure(result, snapshot_name)

        attributes_list = result.get_child_by_name(
            'attributes-list') or netapp_api.NaElement('none')
        snapshot_info_list = attributes_list.get_children()

        self._handle_snapshot_not_found(result, snapshot_info_list,
                                        snapshot_name, volume_name)

        snapshot_info = snapshot_info_list[0]
        snapshot = {
            'name': snapshot_info.get_child_content('name'),
            'volume': snapshot_info.get_child_content('volume'),
            'busy': strutils.bool_from_string(
                snapshot_info.get_child_content('busy')),
        }

        snapshot_owners_list = snapshot_info.get_child_by_name(
            'snapshot-owners-list') or netapp_api.NaElement('none')
        snapshot_owners = set([
            snapshot_owner.get_child_content('owner')
            for snapshot_owner in snapshot_owners_list.get_children()])
        snapshot['owners'] = snapshot_owners

        return snapshot

    def _handle_get_snapshot_return_failure(self, result, snapshot_name):
        error_record_list = result.get_child_by_name(
            'volume-errors') or netapp_api.NaElement('none')
        errors = error_record_list.get_children()

        if errors:
            error = errors[0]
            error_code = error.get_child_content('errno')
            error_reason = error.get_child_content('reason')
            msg = _('Could not read information for snapshot %(name)s. '
                    'Code: %(code)s. Reason: %(reason)s')
            msg_args = {
                'name': snapshot_name,
                'code': error_code,
                'reason': error_reason,
            }
            if error_code == netapp_api.ESNAPSHOTNOTALLOWED:
                raise exception.SnapshotUnavailable(data=msg % msg_args)
            else:
                raise exception.VolumeBackendAPIException(data=msg % msg_args)

    def _handle_snapshot_not_found(self, result, snapshot_info_list,
                                   snapshot_name, volume_name):
        if not self._has_records(result):
            raise exception.SnapshotNotFound(snapshot_id=snapshot_name)
        elif len(snapshot_info_list) > 1:
            msg = _('Could not find unique snapshot %(snap)s on '
                    'volume %(vol)s.')
            msg_args = {'snap': snapshot_name, 'vol': volume_name}
            raise exception.VolumeBackendAPIException(data=msg % msg_args)
