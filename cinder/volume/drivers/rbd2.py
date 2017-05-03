#    Copyright 2013 OpenStack Foundation
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
"""RADOS Block Device Driver"""

from __future__ import absolute_import

from eventlet import tpool
from oslo_config import cfg
from oslo_log import log as logging

from cinder import exception
from cinder.i18n import _, _LE, _LI, _LW
from cinder import interface
from cinder import utils

from cinder.volume.drivers.rbd import RBDDriver

try:
    import rados
    import rbd
except ImportError:
    rados = None
    rbd = None


LOG = logging.getLogger(__name__)

RBD_OPTS = [
    cfg.StrOpt('rbd_cluster_name',
               default='ceph',
               help='The name of ceph cluster'),
    cfg.StrOpt('rbd_pool',
               default='rbd',
               help='The RADOS pool where rbd volumes are stored'),
    cfg.StrOpt('rbd_user',
               help='The RADOS client name for accessing rbd volumes '
                    '- only set when using cephx authentication'),
    cfg.StrOpt('rbd_ceph_conf',
               default='',  # default determined by librados
               help='Path to the ceph configuration file'),
    cfg.BoolOpt('rbd_flatten_volume_from_snapshot',
                default=False,
                help='Flatten volumes created from snapshots to remove '
                     'dependency from volume to snapshot'),
    cfg.StrOpt('rbd_secret_uuid',
               help='The libvirt uuid of the secret for the rbd_user '
                    'volumes'),
    cfg.StrOpt('volume_tmp_dir',
               help='Directory where temporary image files are stored '
                    'when the volume driver does not write them directly '
                    'to the volume.  Warning: this option is now deprecated, '
                    'please use image_conversion_dir instead.'),
    cfg.IntOpt('rbd_max_clone_depth',
               default=5,
               help='Maximum number of nested volume clones that are '
                    'taken before a flatten occurs. Set to 0 to disable '
                    'cloning.'),
    cfg.IntOpt('rbd_store_chunk_size', default=4,
               help='Volumes will be chunked into objects of this size '
                    '(in megabytes).'),
    cfg.IntOpt('rados_connect_timeout', default=-1,
               help='Timeout value (in seconds) used when connecting to '
                    'ceph cluster. If value < 0, no timeout is set and '
                    'default librados value is used.'),
    cfg.IntOpt('rados_connection_retries', default=3,
               help='Number of retries if connection to ceph cluster '
                    'failed.'),
    cfg.IntOpt('rados_connection_interval', default=5,
               help='Interval value (in seconds) between connection '
                    'retries to ceph cluster.')
]

CONF = cfg.CONF
CONF.register_opts(RBD_OPTS)


@interface.volumedriver
class RBDDriver2(RBDDriver):
    """Implements RADOS block device (RBD) volume commands."""

    VERSION = '1.2.0'

    # ThirdPartySystems wiki page
    CI_WIKI_NAME = "Cinder_Jenkins"

    def __init__(self, *args, **kwargs):
        super(RBDDriver2, self).__init__(*args, **kwargs)

    def initialize_connection(self, volume, connector):
        hosts, ports = self._get_mon_addrs()
        data = {
            'driver_volume_type': 'rbd',
            'data': {
                'name': '%s/%s' % (self.configuration.rbd_pool,
                                   volume.name),
                'hosts': hosts,
                'ports': ports,
                'cluster_name': self.configuration.rbd_cluster_name,
                'auth_enabled': (self.configuration.rbd_user is not None),
                'auth_username': self.configuration.rbd_user,
                'secret_type': 'ceph',
                'secret_uuid': self.configuration.rbd_secret_uuid,
                'volume_id': volume.id,
                'encrypted': True if volume.encryption_key_id else False,
                'do_local_attach': True if volume.encryption_key_id else False,
            }
        }
        LOG.debug('connection data: %s', data)
        return data

    def _connect_device(self, conn):
        # Use Brick's code to do attach/detach
        use_multipath = self.configuration.use_multipath_for_image_xfer
        device_scan_attempts = self.configuration.num_volume_device_scan_tries
        protocol = conn['driver_volume_type']
        connector = utils.brick_get_connector(
            protocol,
            use_multipath=use_multipath,
            device_scan_attempts=device_scan_attempts,
            conn=conn)
        device = connector.connect_volume(conn['data'])
        attach_info = {'conn': conn, 'device': device['path'], 'connector': connector}

        if conn['data']['encrypted']:
            symlink_dev = '/dev/rbd-volume-%s' % conn['data']['volume_id']
            utils.execute('ln', '--symbolic', '--force',
                          device['path'], symlink_dev, run_as_root=True)
            
            attach_info = {'conn': conn, 'device': {'path': symlink_dev}, 'connector': connector}

        if not conn['data']['encrypted']:
            host_device = device['path']
            unavailable = True
            try:
                # Secure network file systems will NOT run as root.
                root_access = not self.secure_file_operations_enabled()
                unavailable = not connector.check_valid_device(host_device,
                                                               root_access)
            except Exception:
                LOG.exception(_LE('Could not validate device %s'), host_device)
    
            if unavailable and conn['encrypted']:
                raise exception.DeviceUnavailable(path=host_device,
                                                  attach_info=attach_info,
                                                  reason=(_("Unable to access "
                                                            "the backend storage "
                                                            "via the path "
                                                            "%(path)s.") %
                                                          {'path': host_device}))
        return attach_info

    def _detach_volume(self, context, attach_info, volume, properties,
                       force=False, remote=False):
        """Disconnect the volume from the host."""
        
        super(RBDDriver2, self)._detach_volume(context, attach_info, volume, properties, force, remote)
        if attach_info['conn']['data']['encrypted']:
            utils.execute('rm', '--force', attach_info['device']['path'], run_as_root=True)



