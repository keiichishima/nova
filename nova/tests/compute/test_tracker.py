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

import contextlib
import copy

import mock

from nova.compute import power_state
from nova.compute import resource_tracker
from nova.compute import task_states
from nova.compute import vm_states
from nova import objects
from nova import test

_VIRT_DRIVER_AVAIL_RESOURCES = {
    'vcpus': 4,
    'memory_mb': 512,
    'local_gb': 6,
    'vcpus_used': 0,
    'memory_mb_used': 0,
    'local_gb_used': 0,
    'hypervisor_type': 'fake',
    'hypervisor_version': 0,
    'hypervisor_hostname': 'fakehost',
    'cpu_info': '',
    'numa_topology': None,
}

_INSTANCE_TYPE_FIXTURES = {
    1: {
        'memory_mb': 128,
        'vcpus': 1,
        'root_gb': 1,
        'ephemeral_gb': 0,
    },
    2: {
        'memory_mb': 256,
        'vcpus': 2,
        'root_gb': 5,
        'ephemeral_gb': 0,
    },
}


def flavor_get_by_id_mock(_context, flavorid):
    flavor = _INSTANCE_TYPE_FIXTURES[flavorid]
    return objects.Flavor(**flavor)


# A collection of system_metadata attributes that would exist in instances
# that have the instance type ID matching the dictionary key.
_INSTANCE_TYPE_SYS_META = {
    1: dict(
        ('instance_type_' + key, value) for key, value
        in _INSTANCE_TYPE_FIXTURES[1].items()
    ),
    2: dict(
        ('instance_type_' + key, value) for key, value
        in _INSTANCE_TYPE_FIXTURES[2].items()
    ),
}

_INSTANCE_FIXTURES = [
    objects.Instance(
        id=1,
        uuid='c17741a5-6f3d-44a8-ade8-773dc8c29124',
        memory_mb=128,
        vcpus=1,
        root_gb=1,
        ephemeral_gb=0,
        numa_topology=None,
        instance_type_id=1,
        vm_state=vm_states.ACTIVE,
        power_state=power_state.RUNNING,
        task_state=None,
        os_type='fake-os',  # Used by the stats collector.
        project_id='fake-project',  # Used by the stats collector.
    ),
    objects.Instance(
        id=2,
        uuid='33805b54-dea6-47b8-acb2-22aeb1b57919',
        memory_mb=256,
        vcpus=2,
        root_gb=5,
        ephemeral_gb=0,
        numa_topology=None,
        instance_type_id=2,
        vm_state=vm_states.DELETED,
        power_state=power_state.SHUTDOWN,
        task_state=None,
        os_type='fake-os',
        project_id='fake-project-2',
    ),
]

_MIGRATION_FIXTURES = {
    # A migration that has only this compute node as the source host
    'source-only': objects.Migration(
        id=1,
        instance_uuid='f15ecfb0-9bf6-42db-9837-706eb2c4bf08',
        source_compute='fake-host',
        dest_compute='other-host',
        source_node='fake-node',
        dest_node='other-node',
        old_instance_type_id=1,
        new_instance_type_id=2,
        status='migrating'
    ),
    # A migration that has only this compute node as the dest host
    'dest-only': objects.Migration(
        id=2,
        instance_uuid='f6ed631a-8645-4b12-8e1e-2fff55795765',
        source_compute='other-host',
        dest_compute='fake-host',
        source_node='other-node',
        dest_node='fake-node',
        old_instance_type_id=1,
        new_instance_type_id=2,
        status='migrating'
    ),
    # A migration that has this compute node as both the source and dest host
    'source-and-dest': objects.Migration(
        id=3,
        instance_uuid='f4f0bfea-fe7e-4264-b598-01cb13ef1997',
        source_compute='fake-host',
        dest_compute='fake-host',
        source_node='fake-node',
        dest_node='fake-node',
        old_instance_type_id=1,
        new_instance_type_id=2,
        status='migrating'
    ),
}

_MIGRATION_INSTANCE_FIXTURES = {
    # source-only
    'f15ecfb0-9bf6-42db-9837-706eb2c4bf08': objects.Instance(
        id=101,
        uuid='f15ecfb0-9bf6-42db-9837-706eb2c4bf08',
        memory_mb=128,
        vcpus=1,
        root_gb=1,
        ephemeral_gb=0,
        numa_topology=None,
        instance_type_id=1,
        vm_state=vm_states.ACTIVE,
        power_state=power_state.RUNNING,
        task_state=task_states.RESIZE_MIGRATING,
        system_metadata=_INSTANCE_TYPE_SYS_META[1],
        os_type='fake-os',
        project_id='fake-project',
    ),
    # dest-only
    'f6ed631a-8645-4b12-8e1e-2fff55795765': objects.Instance(
        id=102,
        uuid='f6ed631a-8645-4b12-8e1e-2fff55795765',
        memory_mb=256,
        vcpus=2,
        root_gb=5,
        ephemeral_gb=0,
        numa_topology=None,
        instance_type_id=2,
        vm_state=vm_states.ACTIVE,
        power_state=power_state.RUNNING,
        task_state=task_states.RESIZE_MIGRATING,
        system_metadata=_INSTANCE_TYPE_SYS_META[2],
        os_type='fake-os',
        project_id='fake-project',
    ),
    # source-and-dest
    'f4f0bfea-fe7e-4264-b598-01cb13ef1997': objects.Instance(
        id=3,
        uuid='f4f0bfea-fe7e-4264-b598-01cb13ef1997',
        memory_mb=256,
        vcpus=2,
        root_gb=5,
        ephemeral_gb=0,
        numa_topology=None,
        instance_type_id=2,
        vm_state=vm_states.ACTIVE,
        power_state=power_state.RUNNING,
        task_state=task_states.RESIZE_MIGRATING,
        system_metadata=_INSTANCE_TYPE_SYS_META[2],
        os_type='fake-os',
        project_id='fake-project',
    ),
}


def overhead_zero(instance):
    # Emulate that the driver does not adjust the memory
    # of the instance...
    return {
        'memory_mb': 0
    }


class BaseTestCase(test.NoDBTestCase):

    def setUp(self):
        super(BaseTestCase, self).setUp()
        self.rt = None
        self.flags(my_ip='fake-ip')

    def _setup_rt(self, virt_resources=_VIRT_DRIVER_AVAIL_RESOURCES,
                  estimate_overhead=overhead_zero):
        """Sets up the resource tracker instance with mock fixtures.

        :param virt_resources: Optional override of the resource representation
                               returned by the virt driver's
                               `get_available_resource()` method.
        :param estimate_overhead: Optional override of a function that should
                                  return overhead of memory given an instance
                                  object. Defaults to returning zero overhead.
        """
        self.cond_api_mock = mock.MagicMock()
        self.sched_client_mock = mock.MagicMock()
        self.notifier_mock = mock.MagicMock()
        vd = mock.MagicMock()
        # Make sure we don't change any global fixtures during tests
        virt_resources = copy.deepcopy(virt_resources)
        vd.get_available_resource.return_value = virt_resources
        vd.estimate_instance_overhead.side_effect = estimate_overhead
        self.driver_mock = vd

        with contextlib.nested(
                mock.patch('nova.conductor.API',
                           return_value=self.cond_api_mock),
                mock.patch('nova.scheduler.client.SchedulerClient',
                           return_value=self.sched_client_mock),
                mock.patch('nova.rpc.get_notifier',
                           return_value=self.notifier_mock)):
            self.rt = resource_tracker.ResourceTracker('fake-host',
                                                       vd,
                                                       'fake-node')


class TestUpdateAvailableResources(BaseTestCase):

    def _update_available_resources(self):
        # We test RT._sync_compute_node separately, since the complexity
        # of the update_available_resource() function is high enough as
        # it is, we just want to focus here on testing the resources
        # parameter that update_available_resource() eventually passes
        # to _sync_compute_node().
        with mock.patch.object(self.rt, '_sync_compute_node') as sync_mock:
            self.rt.update_available_resource(mock.sentinel.ctx)
        return sync_mock

    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_no_instances_no_migrations_no_reserved(self, get_mock):
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = []
        capi = self.cond_api_mock
        migr_mock = capi.migration_get_in_progress_by_host_and_node
        migr_mock.return_value = []

        sync_mock = self._update_available_resources()

        vd = self.driver_mock
        vd.get_available_resource.assert_called_once_with('fake-node')
        get_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                         'fake-node',
                                         expected_attrs=[
                                             'system_metadata',
                                             'numa_topology'])
        migr_mock.assert_called_once_with(mock.sentinel.ctx, 'fake-host',
                                          'fake-node')

        expected_resources = {
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 6,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 512,
            'memory_mb_used': 0,
            'pci_stats': '[]',
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 0,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        sync_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_no_instances_no_migrations_reserved_disk_and_ram(
            self, get_mock):
        self.flags(reserved_host_disk_mb=1024,
                   reserved_host_memory_mb=512)
        self._setup_rt()

        get_mock.return_value = []
        capi = self.cond_api_mock
        migr_mock = capi.migration_get_in_progress_by_host_and_node
        migr_mock.return_value = []

        sync_mock = self._update_available_resources()

        expected_resources = {
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 5,  # 6GB avail - 1 GB reserved
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 0,  # 512MB avail - 512MB reserved
            'memory_mb_used': 512,  # 0MB used + 512MB reserved
            'pci_stats': '[]',
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 1,  # 0GB used + 1 GB reserved
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        sync_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_some_instances_no_migrations(self, get_mock):
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = _INSTANCE_FIXTURES
        capi = self.cond_api_mock
        migr_mock = capi.migration_get_in_progress_by_host_and_node
        migr_mock.return_value = []

        sync_mock = self._update_available_resources()

        expected_resources = {
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 5,  # 6 - 1 used
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 384,  # 512 - 128 used
            'memory_mb_used': 128,
            'pci_stats': '[]',
            # NOTE(jaypipes): Due to the design of the ERT, which now is used
            #                 track VCPUs, the actual used VCPUs isn't
            #                 "written" to the resources dictionary that is
            #                 passed to _sync_compute_node() like all the other
            #                 resources are. Instead, _sync_compute_node()
            #                 calls the ERT's write_resources() method, which
            #                 then queries each resource handler plugin for the
            #                 changes in its resource usage and the plugin
            #                 writes changes to the supplied "values" dict. For
            #                 this reason, all other resources except VCPUs
            #                 are accurate here. :(
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 1,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 1  # One active instance
        }
        sync_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_orphaned_instances_no_migrations(self, get_mock):
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = []
        capi = self.cond_api_mock
        migr_mock = capi.migration_get_in_progress_by_host_and_node
        migr_mock.return_value = []

        # Orphaned instances are those that the virt driver has on
        # record as consuming resources on the compute node, but the
        # Nova database has no record of the instance being active
        # on the host. For some reason, the resource tracker only
        # considers orphaned instance's memory usage in its calculations
        # of free resources...
        orphaned_usages = {
            '71ed7ef6-9d2e-4c65-9f4e-90bb6b76261d': {
                # Yes, the return result format of get_per_instance_usage
                # is indeed this stupid and redundant. Also note that the
                # libvirt driver just returns an empty dict always for this
                # method and so who the heck knows whether this stuff
                # actually works.
                'uuid': '71ed7ef6-9d2e-4c65-9f4e-90bb6b76261d',
                'memory_mb': 64
            }
        }
        vd = self.driver_mock
        vd.get_per_instance_usage.return_value = orphaned_usages

        sync_mock = self._update_available_resources()

        expected_resources = {
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 6,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 448,  # 512 - 64 orphaned usage
            'memory_mb_used': 64,
            'pci_stats': '[]',
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 0,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            # Yep, for some reason, orphaned instances are not counted
            # as running VMs...
            'running_vms': 0
        }
        sync_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.Flavor.get_by_id')
    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_no_instances_source_migration(self, get_mock, get_inst_mock,
                                           flavor_get_mock):
        # We test the behavior of update_available_resource() when
        # there is an active migration that involves this compute node
        # as the source host not the destination host, and the resource
        # tracker does not have any instances assigned to it. This is
        # the case when a migration from this compute host to another
        # has been completed, but the user has not confirmed the resize
        # yet, so the resource tracker must continue to keep the resources
        # for the original instance type available on the source compute
        # node in case of a revert of the resize.
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = []
        capi = self.cond_api_mock
        migr_mock = capi.migration_get_in_progress_by_host_and_node
        migr_obj = _MIGRATION_FIXTURES['source-only']
        migr_mock.return_value = [migr_obj]
        # Migration.instance property is accessed in the migration
        # processing code, and this property calls
        # objects.Instance.get_by_uuid, so we have the migration return
        inst_uuid = migr_obj.instance_uuid
        get_inst_mock.return_value = _MIGRATION_INSTANCE_FIXTURES[inst_uuid]
        flavor_get_mock.side_effect = flavor_get_by_id_mock

        sync_mock = self._update_available_resources()

        expected_resources = {
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 5,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 384,  # 512 total - 128 for possible revert of orig
            'memory_mb_used': 128,  # 128 possible revert amount
            'pci_stats': '[]',
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 1,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        sync_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.Flavor.get_by_id')
    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_no_instances_dest_migration(self, get_mock, get_inst_mock,
                                         flavor_get_mock):
        # We test the behavior of update_available_resource() when
        # there is an active migration that involves this compute node
        # as the destination host not the source host, and the resource
        # tracker does not yet have any instances assigned to it. This is
        # the case when a migration to this compute host from another host
        # is in progress, but the user has not confirmed the resize
        # yet, so the resource tracker must reserve the resources
        # for the possibly-to-be-confirmed instance's instance type
        # node in case of a confirm of the resize.
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        get_mock.return_value = []
        capi = self.cond_api_mock
        migr_mock = capi.migration_get_in_progress_by_host_and_node
        migr_obj = _MIGRATION_FIXTURES['dest-only']
        migr_mock.return_value = [migr_obj]
        inst_uuid = migr_obj.instance_uuid
        get_inst_mock.return_value = _MIGRATION_INSTANCE_FIXTURES[inst_uuid]
        flavor_get_mock.side_effect = flavor_get_by_id_mock

        sync_mock = self._update_available_resources()

        expected_resources = {
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            'free_disk_gb': 1,
            'hypervisor_version': 0,
            'local_gb': 6,
            'free_ram_mb': 256,  # 512 total - 256 for possible confirm of new
            'memory_mb_used': 256,  # 256 possible confirmed amount
            'pci_stats': '[]',
            'vcpus_used': 0,  # See NOTE(jaypipes) above about why this is 0
            'hypervisor_type': 'fake',
            'local_gb_used': 5,
            'memory_mb': 512,
            'current_workload': 0,
            'vcpus': 4,
            'running_vms': 0
        }
        sync_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)

    @mock.patch('nova.objects.Flavor.get_by_id')
    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.objects.InstanceList.get_by_host_and_node')
    def test_some_instances_source_and_dest_migration(
            self, get_mock, get_inst_mock, flavor_get_mock):
        # We test the behavior of update_available_resource() when
        # there is an active migration that involves this compute node
        # as the destination host AND the source host, and the resource
        # tracker has a few instances assigned to it, including the
        # instance that is resizing to this same compute node. The tracking
        # of resource amounts takes into account both the old and new
        # resize instance types as taking up space on the node.
        self.flags(reserved_host_disk_mb=0,
                   reserved_host_memory_mb=0)
        self._setup_rt()

        capi = self.cond_api_mock
        migr_mock = capi.migration_get_in_progress_by_host_and_node
        migr_obj = _MIGRATION_FIXTURES['source-and-dest']
        migr_mock.return_value = [migr_obj]
        inst_uuid = migr_obj.instance_uuid
        # The resizing instance has already had its instance type
        # changed to the *new* instance type (the bigger one, instance type 2)
        resizing_instance = _MIGRATION_INSTANCE_FIXTURES[inst_uuid]
        all_instances = _INSTANCE_FIXTURES + [resizing_instance]
        get_mock.return_value = all_instances
        get_inst_mock.return_value = resizing_instance
        flavor_get_mock.side_effect = flavor_get_by_id_mock

        sync_mock = self._update_available_resources()

        expected_resources = {
            'host_ip': 'fake-ip',
            'numa_topology': None,
            'metrics': '[]',
            'cpu_info': '',
            'hypervisor_hostname': 'fakehost',
            # 6 total - 1G existing - 5G new flav - 1G old flav
            'free_disk_gb': -1,
            'hypervisor_version': 0,
            'local_gb': 6,
            # 512 total - 128 existing - 256 new flav - 128 old flav
            'free_ram_mb': 0,
            'memory_mb_used': 512,  # 128 exist + 256 new flav + 128 old flav
            'pci_stats': '[]',
            # See NOTE(jaypipes) above for reason why this isn't accurate until
            # _sync_compute_node() is called.
            'vcpus_used': 0,
            'hypervisor_type': 'fake',
            'local_gb_used': 7,  # 1G existing, 5G new flav + 1 old flav
            'memory_mb': 512,
            'current_workload': 1,  # One migrating instance...
            'vcpus': 4,
            'running_vms': 2
        }
        sync_mock.assert_called_once_with(mock.sentinel.ctx,
                expected_resources)
