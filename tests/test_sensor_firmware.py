# SPDX-License-Identifier: GPL-2.0-only
"""Exercise the production C container validator with synthetic sensor data."""
import ctypes
import errno
import hashlib
import importlib.util
from pathlib import Path
import struct
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sensor_firmware_pack_test', ROOT/'scripts/prepare-s5k-firmware.py')
packer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packer)


class SensorFirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = Path(cls.tmp.name)
        driver = (ROOT/'drivers/s10-camera-v4l2/s5k-cis.c').read_text()
        reg_struct = re.search(r'struct s5k_reg \{.*?\n\};', driver, re.S).group()
        start = driver.index('static int s5k_decode_table(')
        decode = driver[start:driver.index('static int s5k_load_firmware(',start)]
        prefix = '#include "s5k-fw.h"\n#include <stdlib.h>\n'+reg_struct+'''
struct device { int unused; };
#define GFP_KERNEL 0
static int allocation_fails;
static void *devm_kcalloc(struct device *dev, size_t n, size_t bytes, int flags) {
    (void)dev; (void)flags;
    return allocation_fails ? NULL : calloc(n,bytes);
}
'''+decode
        (folder/'check.c').write_text(prefix+'''
int decode_table(const unsigned char *p, size_t n, unsigned short id,
                 unsigned short rev, unsigned int modes, int master,
                 unsigned int index, int fail, const struct s5k_reg **out, unsigned int *count) {
    struct s5k_fw_view view;
    int ret = s5k_fw_validate(p,n,id,rev,modes,master,&view);
    if (ret || index >= view.tables) return -EINVAL;
    allocation_fails = fail;
    return s5k_decode_table(NULL,&view,index,out,count);
}
void release_table(void *p) { free(p); }
int check(const unsigned char *p, size_t n, unsigned short id,
          unsigned short rev, unsigned int modes, int master) {
    struct s5k_fw_view view = {p, 123, 9, 9};
    int ret = s5k_fw_validate(p,n,id,rev,modes,master,&view);
    if (ret && (view.data != p || view.size != 123 || view.tables != 9 || view.bursts != 9))
        return -999;
    return ret;
}
''')
        subprocess.run(['cc','-shared','-fPIC','-std=c11','-Wall','-Wextra','-Werror',
                        '-I'+str(ROOT/'drivers/s10-camera-v4l2'),str(folder/'check.c'),
                        '-o',str(folder/'check.so')],check=True)
        cls.library = ctypes.CDLL(str(folder/'check.so'))
        cls.check = cls.library.check
        cls.check.argtypes = [ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint16,
                             ctypes.c_uint16,ctypes.c_uint,ctypes.c_int]
        cls.check.restype = ctypes.c_int
        class Reg(ctypes.Structure):
            _fields_ = [('reg',ctypes.c_uint16),('val',ctypes.c_uint16),('length',ctypes.c_uint8)]
        cls.Reg = Reg
        cls.decode = cls.library.decode_table
        cls.decode.argtypes = [ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint16,ctypes.c_uint16,
                               ctypes.c_uint,ctypes.c_int,ctypes.c_uint,ctypes.c_int,
                               ctypes.POINTER(ctypes.POINTER(Reg)),ctypes.POINTER(ctypes.c_uint)]
        cls.decode.restype = ctypes.c_int
        cls.library.release_table.argtypes = [ctypes.c_void_p]
        cls.library.release_table.restype = None

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def fixture(self):
        return packer.pack(0x3109,0xa101,[(0,[(0xfcfc,0x4000,2),(0xffff,3000,0)]),
                                      (0x100,[(0x0100,1,1)])],[])

    def valid(self, data, model=0x3109, revision=0xa101, modes=1, master=0):
        buffer = ctypes.create_string_buffer(data)
        result = self.check(buffer,len(data),model,revision,modes,master)
        self.assertNotEqual(result,-999,'validation modified its output on failure')
        return result == 0

    def test_valid_container_and_explicit_wire_header(self):
        data = self.fixture()
        self.assertEqual(data[:16],b'S10S5K1\0\x01\x00\x09\x31\x01\xa1\x02\x00')
        self.assertTrue(self.valid(data))

    def test_multiple_modes_master_and_bursts(self):
        data=packer.pack(0x3109,0xa101,[(0,[(0xfffe,0,0)]),(0x100,[(1,2,2)]),
                                     (0x101,[(2,3,2)]),(1,[(3,4,1)])],[b'\x6f\x12\x00\x01'])
        self.assertTrue(self.valid(data,modes=2,master=1))
        self.assertFalse(self.valid(data,modes=1,master=1))

    def test_every_truncated_prefix_is_rejected(self):
        data=self.fixture()
        for length in range(len(data)):
            with self.subTest(length=length):self.assertFalse(self.valid(data[:length]))

    def test_identity_version_reserved_and_counts_are_checked(self):
        data=self.fixture()
        self.assertFalse(self.valid(data,model=0x20c4))
        self.assertFalse(self.valid(data,revision=0xa102))
        for offset,value in [(8,2),(14,4),(16,17),(18,1),(24,1),(26,1)]:
            altered=bytearray(data);struct.pack_into('<H',altered,offset,value)
            with self.subTest(offset=offset):self.assertFalse(self.valid(bytes(altered)))

    def test_overlapping_holes_overflow_and_trailing_data_are_rejected(self):
        data=self.fixture()
        for offset,value in [(20,0xffffffff),(28,0),(28,49),(28,0xffffffff),
                             (32,0xffffffff),(40,0),(44,0xffffffff)]:
            altered=bytearray(data);struct.pack_into('<I',altered,offset,value)
            with self.subTest(offset=offset,value=value):self.assertFalse(self.valid(bytes(altered)))
        altered=bytearray(data+b'X');struct.pack_into('<I',altered,20,len(altered))
        self.assertFalse(self.valid(bytes(altered)))

    def test_invalid_register_width_value_delay_and_burst_index(self):
        cases=[(1,256,1),(1,2,0),(1,2,3),(0xffff,65001,0),(0xffff,1,2),
               (0xfffe,0,0),(0xfffe,0,2)]
        for op in cases:
            with self.subTest(op=op):
                data=packer.pack(0x3109,0xa101,[(0,[op]),(0x100,[(1,2,2)])],[])
                self.assertFalse(self.valid(data))

    def test_burst_bounds_and_canonical_offsets(self):
        for burst in [b'',b'X',b'X'*65536]:
            data=packer.pack(0x3109,0xa101,[(0,[(0xfffe,0,0)]),(0x100,[(1,2,2)])],[burst])
            self.assertFalse(self.valid(data))
        data=bytearray(packer.pack(0x3109,0xa101,[(0,[(0xfffe,0,0)]),(0x100,[(1,2,2)])],[b'ab']))
        struct.pack_into('<I',data,48,0)
        self.assertFalse(self.valid(bytes(data)))

    def decoded(self, data, model, revision, modes, master, index, fail=0):
        buffer=ctypes.create_string_buffer(data)
        out=ctypes.POINTER(self.Reg)(); count=ctypes.c_uint()
        result=self.decode(buffer,len(data),model,revision,modes,master,index,fail,
                           ctypes.byref(out),ctypes.byref(count))
        entries=[(out[i].reg,out[i].val,out[i].length) for i in range(count.value)]
        self.library.release_table(out)
        return result,entries

    def test_actual_driver_decoder_preserves_register_fields(self):
        self.assertEqual(self.decoded(self.fixture(),0x3109,0xa101,1,0,0),
                         (0,[(0xfcfc,0x4000,2),(0xffff,3000,0)]))
        self.assertEqual(self.decoded(self.fixture(),0x3109,0xa101,1,0,1),
                         (0,[(0x0100,1,1)]))

    def test_actual_driver_decoder_reports_allocation_failure(self):
        self.assertEqual(self.decoded(self.fixture(),0x3109,0xa101,1,0,0,fail=1),
                         (-errno.ENOMEM,[]))

    def test_manifest_contains_identity_hash_only(self):
        data=self.fixture();text=packer.manifest({'s5k3p9':data})
        self.assertIn(hashlib.sha256(data).hexdigest(),text)
        self.assertNotIn('0xfcfc',text)


if __name__ == '__main__':unittest.main()
