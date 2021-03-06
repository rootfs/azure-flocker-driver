import datetime
import uuid


class Vhd(object):

    def __init__():
        return

    @staticmethod
    def generate_vhd_footer(size):
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
        #

        """
        # TODO Are we taking any unreliable dependencies of the content of
        # the azure VHD footer?
        footer_dict = {}
        # the ascii string 'conectix'
        footer_dict['cookie'] = \
            bytearray([0x63, 0x6f, 0x6e, 0x65, 0x63, 0x74, 0x69, 0x78])
        # no features enabled
        footer_dict['features'] = bytearray([0x00, 0x00, 0x00, 0x02])
        # current file version
        footer_dict['version'] = bytearray([0x00, 0x01, 0x00, 0x00])
        # in the case of a fixed disk, this is set to -1
        footer_dict['data_offset'] = \
            bytearray([0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
                      0xff])
        # hex representation of seconds since january 1st 2000
        footer_dict['timestamp'] = Vhd._generate_timestamp()
        # ascii code for 'wa' = windowsazure
        footer_dict['creator_app'] = bytearray([0x77, 0x61, 0x00, 0x00])
        # ascii code for version of creator application
        footer_dict['creator_version'] = \
            bytearray([0x00, 0x07, 0x00, 0x00])
        # creator host os. windows or mac, ascii for 'wi2k'
        footer_dict['creator_os'] = \
            bytearray([0x57, 0x69, 0x32, 0x6b])
        footer_dict['original_size'] = \
            bytearray.fromhex(hex(size).replace('0x', '').zfill(16))
        footer_dict['current_size'] = \
            bytearray.fromhex(hex(size).replace('0x', '').zfill(16))
        # ox820=2080 cylenders, 0x10=16 heads, 0x3f=63 sectors per cylndr,
        footer_dict['disk_geometry'] = \
            bytearray([0x08, 0x20, 0x10, 0x3f])
        # 0x2 = fixed hard disk
        footer_dict['disk_type'] = bytearray([0x00, 0x00, 0x00, 0x02])
        # a uuid
        footer_dict['unique_id'] = bytearray.fromhex(uuid.uuid4().hex)
        # saved state and reserved
        footer_dict['saved_reserved'] = bytearray(428)

        footer_dict['checksum'] = Vhd._compute_checksum(footer_dict)

        return bytes(Vhd._combine_byte_arrays(footer_dict))

    @staticmethod
    def _generate_timestamp():
        hevVal = hex(long(datetime.datetime.now().strftime("%s")) - 946684800)
        return bytearray.fromhex(hevVal.replace(
            'L', '').replace('0x', '').zfill(8))

    @staticmethod
    def _compute_checksum(vhd_data):

        if 'checksum' in vhd_data:
            del vhd_data['checksum']

        wholeArray = Vhd._combine_byte_arrays(vhd_data)

        total = 0
        for byte in wholeArray:
            total += byte

        # ones compliment
        total = ~total

        def tohex(val, nbits):
            return hex((val + (1 << nbits)) % (1 << nbits))

        return bytearray.fromhex(tohex(total, 32).replace('0x', ''))

    @staticmethod
    def _combine_byte_arrays(vhd_data):
        wholeArray = vhd_data['cookie'] \
            + vhd_data['features'] \
            + vhd_data['version'] \
            + vhd_data['data_offset'] \
            + vhd_data['timestamp'] \
            + vhd_data['creator_app'] \
            + vhd_data['creator_version'] \
            + vhd_data['creator_os'] \
            + vhd_data['original_size'] \
            + vhd_data['current_size'] \
            + vhd_data['disk_geometry'] \
            + vhd_data['disk_type']

        if 'checksum' in vhd_data:
            wholeArray += vhd_data['checksum']

        wholeArray += vhd_data['unique_id'] \
            + vhd_data['saved_reserved']

        return wholeArray
