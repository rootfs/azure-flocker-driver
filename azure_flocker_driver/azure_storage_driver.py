import time
import datetime
import uuid
from uuid import UUID
import socket
import re
import os
import subprocess

from bitmath import Byte, GiB
from azure.servicemanagement import ServiceManagementService
from azure.storage import BlobService
from eliot import Message, Logger
from zope.interface import implementer
from twisted.python.filepath import FilePath

from flocker.node.agents.blockdevice import AlreadyAttachedVolume, \
    IBlockDeviceAPI, BlockDeviceVolume, UnknownVolume, UnattachedVolume

# Eliot is transitioning away from the "Logger instances all over the place"
# approach.  And it's hard to put Logger instances on PRecord subclasses which
# we have a lot of.  So just use this global logger for now.

_logger = Logger()

# Azure's allocation granularity is 1GB

ALLOCATION_GRANULARITY = 1


class Lun(object):

    device_path = ''
    lun = ''

    def __init__():
        return

    @staticmethod
    def rescan_scsi():
        with open(os.devnull, 'w') as shutup:
            subprocess.call(['fdisk', '-l'], stdout=shutup, stderr=shutup)

    @staticmethod
    def compute_next_lun():
        """
        Computes the next available LUN on this node (excluding LUN=0)
        return int: The available LUN
        """
        # force the latest scsci info
        Lun.rescan_scsi()
        disk_info_string = subprocess.check_output('lsscsi')
        parts = disk_info_string.strip('\n').split('\n')

        lun = 0
        count = 0
        for i in range(0, len(parts)):

            line = parts[i]
            segments = re.split(':|]|\[', line)

            if int(segments[1]) != 5:
                continue

            next_lun = int(segments[4])

            if next_lun - count >= 1:
                lun = next_lun - 1
                break

            if i == len(parts) - 1:
                lun = next_lun + 1
                break
            count += 1
            lun = next_lun

        return lun

    # Returns a string representing the block device path based
    # on a provided lun slot

    @staticmethod
    def get_device_path_for_lun(lun):
        """
        Returns a FilePath representing the path of the device
        with the sepcified LUN. TODO: Is it valid to predict the
        path based on the LUN?
        return FilePath: The FilePath representing the attached disk
        """
        Lun.rescan_scsi()
        if lun > 31:
            raise Exception('valid lun parameter is 0 - 31, inclusive')
        base = '/dev/sd'

        # luns go 0-31
        ascii_base = ord('c')

        return FilePath(base + chr(ascii_base + lun), False)


class UnsupportedVolumeSize(Exception):
    """
    The volume size is not supported
    Needs to be 8GB allocation granularity
    :param unicode dataset_id: The volume dataset_id
    """

    def __init__(self, dataset_id):
        if not isinstance(dataset_id, UUID):
            raise TypeError(
                'Unexpected dataset_id type. '
                + 'Expected unicode. Got {!r}.'.format(
                    dataset_id))
        Exception.__init__(self, dataset_id)
        self.dataset_id = dataset_id


@implementer(IBlockDeviceAPI)
class AzureStorageBlockDeviceAPI(object):
    """
    An ``IBlockDeviceAsyncAPI`` which uses Azure Storage Backed Block Devices
    Current Support: Azure SMS API
    """

    def __init__(
            self, azure_client, azure_storage_client, service_name,
            deployment_name, storage_account_name, disk_container_name):
        """
        :param ServiceManagement azure_client: an instance of the azure
        serivce managment api client.
        :param String service_name: The name of the cloud service
        :param
            names of Azure volumes to identify cluster
        :returns: A ``BlockDeviceVolume``.
        """

        self._instance_id = self.compute_instance_id()
        self._azure_service_client = azure_client
        self._service_name = service_name
        self._azure_storage_client = azure_storage_client
        self._storage_account_name = storage_account_name
        self._disk_container_name = disk_container_name

    def allocation_unit(self):
        """
        1GiB is the minimum allocation unit for azure disks
        return int: 1 GiB
        """

        return int(GiB(1).to_Byte().value)

    def compute_instance_id(self):
        """
        Azure Stored a UUID in the SDC kernel module.
        """

        # Node host names should be unique within a vnet

        return unicode(socket.gethostname())

    def create_volume(self, dataset_id, size):
        """
        Create a new volume.
        :param UUID dataset_id: The Flocker dataset ID of the dataset on this
            volume.
        :param int size: The size of the new volume in bytes.
        :returns: A ``Deferred`` that fires with a ``BlockDeviceVolume`` when
            the volume has been created.
        """

        size_in_gb = Byte(size).to_GiB().value

        if size_in_gb % 1 != 0:
            raise UnsupportedVolumeSize(dataset_id)

        # Create a new page blob as a blank disk
        self._azure_storage_client.put_blob(
            container_name=self._disk_container_name,
            blob_name=self._disk_label_for_dataset_id(dataset_id),
            blob=None,
            x_ms_blob_type='PageBlob',
            x_ms_blob_content_type='application/octet-stream',
            x_ms_blob_content_length=size)

        # for disk to be a valid vhd it requires a vhd footer
        # on the last 512 bytes
        vhd_footer = self._generate_vhd_footer(size)

        self._azure_storage_client.put_page(
            container_name=self._disk_container_name,
            blob_name=self._disk_label_for_dataset_id(dataset_id),
            page=vhd_footer,
            x_ms_page_write='update',
            x_ms_range='bytes=' + str((size - 512)) + '-' + str(size - 1))

        label = self._disk_label_for_blockdevice_id(str(dataset_id))
        return BlockDeviceVolume(
            blockdevice_id=unicode(label),
            size=size,
            attached_to=None,
            dataset_id=self._dataset_id_for_disk_label(label))

    def destroy_volume(self, blockdevice_id):
        """
        Destroy an existing volume.
        :param unicode blockdevice_id: The unique identifier for the volume to
            destroy.
        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.
        :return: ``None``
        """
        Message.new(Info='Destorying block device: '
                    + str(blockdevice_id)).write(_logger)

        (target_disk, role_name, lun) = \
            self._get_disk_vmname_lun(blockdevice_id)

        if target_disk is None:
            blobs = self._azure_storage_client.list_blobs(
                self._disk_container_name,
                prefix='flocker-')

            for b in blobs:
                if b.name == str(blockdevice_id):

                    self._azure_storage_client.delete_blob(
                        container_name=self._disk_container_name,
                        blob_name=b.name,
                        x_ms_delete_snapshots='include')
                    return

            raise UnknownVolume(blockdevice_id)

        if lun is not None:
            request = \
                self._azure_service_client.delete_data_disk(
                    service_name=self._service_name,
                    deployment_name=self._service_name,
                    role_name=target_disk.name, lun=lun, delete_vhd=True)
        else:
            if target_disk.__class__.__name__ == 'Blob':
                # unregistered disk
                self._azure_storage_client.delete_blob(
                    self._disk_container_name, target_disk.name)
            else:
                request = self._azure_service_client.delete_disk(
                    target_disk.name, True)
                self._wait_for_async(request.request_id, 5000)

    def attach_volume(self, blockdevice_id, attach_to):
        """
        Attach ``blockdevice_id`` to ``host``.
        :param unicode blockdevice_id: The unique identifier for the block
            device being attached.
        :param unicode attach_to: An identifier like the one returned by the
            ``compute_instance_id`` method indicating the node to which to
            attach the volume.
        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.
        :raises AlreadyAttachedVolume: If the supplied ``blockdevice_id`` is
            already attached.
        :returns: A ``BlockDeviceVolume`` with a ``host`` attribute set to
            ``host``.
        """

        (target_disk, role_name, lun) = \
            self._get_disk_vmname_lun(blockdevice_id)

        if target_disk is None:
            raise UnknownVolume(blockdevice_id)

        if lun is not None:
            raise AlreadyAttachedVolume(blockdevice_id)

        request = None
        disk_size = 0
        remote_lun = self._compute_next_remote_lun(str(attach_to))

        Message.new(Info='Attempting to attach ' + str(blockdevice_id)
                    + ' to ' + str(attach_to)).write(_logger)
        if target_disk.__class__.__name__ == 'Blob':
            disk_size = target_disk.properties.content_length
            request = \
                self._azure_service_client.add_data_disk(
                    service_name=self._service_name,
                    deployment_name=self._service_name,
                    role_name=str(attach_to), lun=remote_lun,
                    disk_label=blockdevice_id,
                    source_media_link='https://' + self._storage_account_name
                    + '.blob.core.windows.net/' + self._disk_container_name
                    + '/' + blockdevice_id)
        else:

            request = \
                self._azure_service_client.add_data_disk(
                    service_name=self._service_name,
                    deployment_name=self._service_name,
                    role_name=str(attach_to), lun=remote_lun,
                    disk_name=target_disk.name)

        self._wait_for_async(request.request_id, 5000)

        Message.new(Info='waiting for azure to report '
                    + ' disk as attached...').write(_logger)

        timeout_count = 0

        while lun is None:
            (target_disk, role_name, lun) = \
                self._get_disk_vmname_lun(blockdevice_id)
            time.sleep(1)
            timeout_count += 1

            if timeout_count > 5000:
                break
        Message.new(Info='disk attached').write(_logger)

        if target_disk.__class__.__name__ == 'Blob':
            return self._blockdevicevolume_from_azure_volume(blockdevice_id,
                                                             disk_size,
                                                             attach_to)
        else:
            return self._blockdevicevolume_from_azure_volume(
                target_disk.label,
                self._gibytes_to_bytes(target_disk.logical_disk_size_in_gb),
                attach_to)

    def detach_volume(self, blockdevice_id):
        """
        Detach ``blockdevice_id`` from whatever host it is attached to.
        :param unicode blockdevice_id: The unique identifier for the block
            device being detached.
        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.
        :raises UnattachedVolume: If the supplied ``blockdevice_id`` is
            not attached to anything.
        :returns: ``None``
        """

        (target_disk, role_name, lun) = \
            self._get_disk_vmname_lun(blockdevice_id)

        if target_disk is None:
            raise UnknownVolume(blockdevice_id)

        if lun is None:
            raise UnattachedVolume(blockdevice_id)

        # contrary to function name it doesn't delete by default, just detachs

        request = \
            self._azure_service_client.delete_data_disk(
                service_name=self._service_name,
                deployment_name=self._service_name,
                role_name=role_name, lun=lun)

        self._wait_for_async(request.request_id, 5000)

        timeout_count = 0
        Message.new(Info='waiting for azure to report '
                    + 'disk as attached...').write(_logger)
        while role_name is not None or lun is not None:
            (target_disk, role_name, lun) = \
                self._get_disk_vmname_lun(blockdevice_id)
            time.sleep(1)
            timeout_count += 1

            if timeout_count > 5000:
                break

        Message.new(Info='disk detached').write(_logger)

    def get_device_path(self, blockdevice_id):
        """
        Return the device path that has been allocated to the block device on
        the host to which it is currently attached.
        :param unicode blockdevice_id: The unique identifier for the block
            device.
        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.
        :raises UnattachedVolume: If the supplied ``blockdevice_id`` is
            not attached to a host.
        :returns: A ``FilePath`` for the device.
        """

        (target_disk_or_blob, role_name, lun) = \
            self._get_disk_vmname_lun(blockdevice_id)

        if target_disk_or_blob is None:
            raise UnknownVolume(blockdevice_id)

        if lun is None:
            raise UnattachedVolume(blockdevice_id)

        return Lun.get_device_path_for_lun(lun)

    def list_volumes(self):
        """
        List all the block devices available via the back end API.
        :returns: A ``list`` of ``BlockDeviceVolume``s.
        """
        media_url_prefix = 'https://' + self._storage_account_name \
            + '.blob.core.windows.net/' + self._disk_container_name
        disks = self._azure_service_client.list_disks()
        disk_list = []
        blobs = self._azure_storage_client.list_blobs(
            self._disk_container_name,
            prefix='flocker-')
        all_blobs = []
        for b in blobs:
            # todo - this could be big!
            all_blobs.append(b)

        for d in disks:

            if media_url_prefix not in d.media_link or \
                    'flocker-' not in d.label:
                    continue

            role_name = None

            if d.attached_to is not None \
                    and d.attached_to.role_name is not None:

                    role_name = d.attached_to.role_name

            disk_list.append(self._blockdevicevolume_from_azure_volume(
                d.label, self._gibytes_to_bytes(d.logical_disk_size_in_gb),
                role_name))
            for i in range(0, len(all_blobs)):
                if d.label == all_blobs[i].name:
                    # filter blobs that are already registered
                    del all_blobs[i]
                    break

        for b in all_blobs:
            # include unregistered 'disk' blobs
            disk_list.append(self._blockdevicevolume_from_azure_volume(
                b.name, b.properties.content_length, None))

        return disk_list

    def detach_delete_all_disks(self):

        Message.new(Info='Cleaning Up Detaching/Disks').write(_logger)
        deployment = self._azure_service_client.get_deployment_by_name(
            self._service_name, self._service_name)

        for r in deployment.role_instance_list:
            vm_info = self._azure_service_client.get_role(
                self._service_name, self._service_name, r.role_name)
        deleted_disk_names = []

        for d in vm_info.data_virtual_hard_disks:
            if 'flocker-' in d.disk_label:
                request = self._azure_service_client.delete_data_disk(
                    service_name=self._service_name,
                    deployment_name=self._service_name,
                    role_name=r.role_name,
                    lun=d.lun,
                    delete_vhd=True)

                Message.new(Info='Deleting Disk: ' + str(d.disk_label)
                            + ' ' + str(d.disk_name)).write(_logger)
                self._wait_for_async(request.request_id, 5000)
                deleted_disk_names.append(d.disk_name)

                Message.new(Info='waiting for azure to '
                            + 'report disk as detached...').write(_logger)

                role_name = r.role_name
                lun = d.lun
                timeout_count = 0
                while role_name is not None or lun is not None:
                    (target_disk, role_name, lun) = \
                        self._get_disk_vmname_lun(d.disk_label)
                    time.sleep(1)
                    timeout_count += 1

                    if timeout_count > 5000:
                        break
                Message.new(Info='Disk Detached: ' + str(d.disk_label)
                            + ' ' + str(d.disk_name)).write(_logger)

        for d in self._azure_service_client.list_disks():
            # only disks labels/blob names with flocker- prefix
            # only those disks within the designated disks container
            # only those disks which have not already been deleted
            container_link = 'https://' + self._storage_account_name \
                + '.blob.core.windows.net/' + self._disk_container_name + '/'

            if 'flocker-' in d.label and (container_link) in d.media_link \
                    and not any(d.name in s for s in deleted_disk_names):

                        Message.new(Info='Deleting Disk: ' + str(d.disk_label)
                                    + ' ' + str(d.disk_name)).write(_logger)
                        self._azure_service_client.delete_disk(
                            d.name,
                            delete_vhd=True)
        # all the blobs left over should be unregistered disks
        for b in self._azure_storage_client.list_blobs(
                self._disk_container_name, 'flocker-'):
                    Message.new(Info='Deleting Disk: '
                                + str(b.name)).write(_logger)
                    self._azure_storage_client.delete_blob(
                        self._disk_container_name,
                        b.name)

    def _generate_vhd_footer(self, size):
        """
        Generate a binary VHD Footer
        # Fixed VHD Footer Format Specification
        # spec:
        # https://technet.microsoft.com/en-us/virtualization/bb676673.aspx#E3B
        # Field         Size (bytes)
        # Cookie        8
        # Features      4
        # Version       4
        # Data Offset   4
        # TimeStamp     4
        # Creator App   4
        # Creator Ver   4
        # CreatorHostOS 4
        # Original Size 8
        # Current Size  8
        # Disk Geo      4
        # Disk Type     4
        # Checksum      4
        # Unique ID     16
        # Saved State   1
        # Reserved      427
        # # the ascii string 'conectix'
        """
        # TODO Are we taking any unreliable dependencies of the content of
        # the azure VHD footer?
        cookie = bytearray([0x63, 0x6f, 0x6e, 0x65, 0x63, 0x74, 0x69, 0x78])
        # no features enabled
        features = bytearray([0x00, 0x00, 0x00, 0x02])
        # current file version
        version = bytearray([0x00, 0x01, 0x00, 0x00])
        # in the case of a fixed disk, this is set to -1
        data_offset = bytearray([0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
                                 0xff])
        # hex representation of seconds since january 1st 2000
        timestamp = bytearray.fromhex(hex(
            long(datetime.datetime.now().strftime("%s")) - 946684800).replace(
                'L', '').replace('0x', '').zfill(8))
        # ascii code for 'wa' = windowsazure
        creator_app = bytearray([0x77, 0x61, 0x00, 0x00])
        # ascii code for version of creator application
        creator_version = bytearray([0x00, 0x07, 0x00, 0x00])
        # creator host os. windows or mac, ascii for 'wi2k'
        creator_os = bytearray([0x57, 0x69, 0x32, 0x6b])
        original_size = bytearray.fromhex(hex(size).replace('0x',
                                                            '').zfill(16))
        current_size = bytearray.fromhex(hex(size).replace('0x', '').zfill(16))
        # ox820=2080 cylenders, 0x10=16 heads, 0x3f=63 sectors per cylndr,
        disk_geometry = bytearray([0x08, 0x20, 0x10, 0x3f])
        # 0x2 = fixed hard disk
        disk_type = bytearray([0x00, 0x00, 0x00, 0x02])
        # a uuid
        unique_id = bytearray.fromhex(uuid.uuid4().hex)
        # saved state and reserved
        saved_reserved = bytearray(428)

        to_checksum_array = cookie \
            + features \
            + version \
            + data_offset \
            + timestamp \
            + creator_app \
            + creator_version \
            + creator_os \
            + original_size \
            + current_size \
            + disk_geometry \
            + disk_type \
            + unique_id \
            + saved_reserved

        total = 0
        for b in to_checksum_array:
            total += b

        total = ~total

        def tohex(val, nbits):
            return hex((val + (1 << nbits)) % (1 << nbits))

        checksum = bytearray.fromhex(tohex(total, 32).replace('0x', ''))

        blob_data = cookie \
            + features \
            + version \
            + data_offset \
            + timestamp \
            + creator_app \
            + creator_version \
            + creator_os \
            + original_size \
            + current_size \
            + disk_geometry \
            + disk_type \
            + checksum \
            + unique_id \
            + saved_reserved

        return bytes(blob_data)

    def _disk_label_for_dataset_id(self, dataset_id):
        """
        Returns a disk label for a given Dataset ID
        :param unicode dataset_id: The identifier of the dataset
        :returns string: A string representing the disk label
        """

        label = 'flocker-' + str(dataset_id)
        return label

    def _dataset_id_for_disk_label(self, disk_label):
        """
        Returns a UUID representing the Dataset ID for the given disk
        label
        :param string disk_label: The disk label
        :returns UUID: The UUID of the dataset
        """
        return UUID(disk_label.replace('flocker-', ''))

    def _get_disk_vmname_lun(self, blockdevice_id):
        target_disk = None
        target_lun = None
        role_name = None
        disk_list = self._azure_service_client.list_disks()

        for d in disk_list:

            if not 'flocker-' not in d.label:
                continue
            if d.label == str(blockdevice_id):
                target_disk = d
                break

        if target_disk is None:
            # check for unregisterd disk
            blobs = self._azure_storage_client.list_blobs(
                self._disk_container_name,
                prefix='flocker-')
            for b in blobs:
                if b.name == str(blockdevice_id):
                    return b, None, None
            return None, None, None

        vm_info = None

        if hasattr(target_disk.attached_to, 'role_name'):
            vm_info = self._azure_service_client.get_role(
                self._service_name, self._service_name,
                target_disk.attached_to.role_name)

            for d in vm_info.data_virtual_hard_disks:
                if d.disk_name == target_disk.name:
                    target_lun = d.lun
                    break

            role_name = target_disk.attached_to.role_name

        return (target_disk, role_name, target_lun)

    def _wait_for_async(self, request_id, timeout):
        count = 0
        result = self._azure_service_client.get_operation_status(request_id)
        while result.status == 'InProgress':
            count = count + 1
            if count > timeout:
                Message.new(Info='Timed out waiting for '
                            + 'async operation to complete.').write(_logger)
                return
            time.sleep(5)
            Message.new(Info='.').write(_logger)
            result = self._azure_service_client.get_operation_status(
                request_id)
            if result.error:
                Message.new(Error=str(result.error.code)).write(_logger)

        Message.new(Info=result.status
                    + ' in ' + str(count * 5) + 's').write(_logger)

    def _gibytes_to_bytes(self, size):

        return int(GiB(size).to_Byte().value)

    def _blockdevicevolume_from_azure_volume(self, label, size,
                                             attached_to_name):

        # azure will report the disk size excluding the 512 byte footer
        # however flocker expects the exact value it requested for disk size
        # so offset the reported size to flocker by 512 bytes
        return BlockDeviceVolume(
            blockdevice_id=unicode(label),
            size=int(size),
            attached_to=attached_to_name,
            dataset_id=self._dataset_id_for_disk_label(label)
        )  # disk labels are formatted as flocker-<data_set_id>

    def _compute_next_remote_lun(self, role_name):
        vm_info = self._azure_service_client.get_role(self._service_name,
                                                      self._service_name,
                                                      role_name)
        vm_info.data_virtual_hard_disks = \
            sorted(vm_info.data_virtual_hard_disks, key=lambda obj:
                   obj.lun)
        lun = 0
        for i in range(0, len(vm_info.data_virtual_hard_disks)):
            next_lun = vm_info.data_virtual_hard_disks[i].lun

            if next_lun - i >= 1:
                lun = next_lun - 1
                break

            if i == len(vm_info.data_virtual_hard_disks) - 1:
                lun = next_lun + 1
                break

        return lun


def azure_driver_from_configuration(
        service_name, subscription_id, storage_account_name,
        storage_account_key,
        disk_container_name,
        certificate_data_path,
        debug=None):
    """
    Returns Flocker Azure BlockDeviceAPI from plugin config yml.
        :param uuid cluster_id: The UUID of the cluster
        :param string username: The username for Azure Driver,
            this will be used to login and enable requests to
            be made to the underlying Azure BlockDeviceAPI
        :param string password: The username for Azure Driver,
            this will be used to login and enable requests to be
            made to the underlying Azure BlockDeviceAPI
        :param unicode mdm_ip: The Main MDM IP address. Azure
            Driver will communicate with the Azure Gateway
            Node to issue REST API commands.
        :param integer port: MDM Gateway The port
        :param string protection_domain: The protection domain
            for this driver instance
        :param string storage_pool: The storage pool used for
            this driver instance
        :param FilePath certificate: An optional certificate
            to be used for optional authentication methods.
            The presence of this certificate will change verify
            to True inside the requests.
        :param boolean ssl: use SSL?
        :param boolean debug: verbosity
    """

    # todo return azure storage driver api

    if not os.path.isfile(certificate_data_path):
        raise IOError(
            'Certificate ' + certificate_data_path + ' does not exist')
    sms = ServiceManagementService(subscription_id, certificate_data_path)
    storage_client = BlobService(storage_account_name, storage_account_key)
    return AzureStorageBlockDeviceAPI(sms, storage_client, service_name,
                                      service_name, storage_account_name,
                                      disk_container_name)
