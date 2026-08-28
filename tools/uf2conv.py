#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# Vendored from https://github.com/microsoft/uf2 (utils/uf2conv.py).
# Upstream is MIT licensed. Local additions (clearly delimited below) add
# support for UF2 extension tags (flag 0x8000), which upstream does not
# produce; everything else matches upstream so a refresh still only needs to
# re-apply the marked blocks. Used by the Makefile `uf2` target to convert a
# signed MCUboot image to UF2 format (run with `-c` so it writes a file
# instead of flashing a mounted drive).
import sys
import struct
import subprocess
import re
import os
import os.path
import argparse
import json
from time import sleep


UF2_MAGIC_START0 = 0x0A324655 # "UF2\n"
UF2_MAGIC_START1 = 0x9E5D5157 # Randomly selected
UF2_MAGIC_END    = 0x0AB16F30 # Ditto

# --- local addition: extension-tag support (upstream has none) ---
UF2_FLAG_EXTENSION_TAGS = 0x00008000
# Max bytes available for extension tags after the 256-byte payload in a
# 476-byte data region (512 - 32 header - 256 payload - 4 trailing magic).
UF2_EXT_MAX_BYTES = 476 - 256
# Adaboot board-identity extension tag (24-bit type, stored LE on the wire).
# Chosen at random to avoid the standard tag set in the UF2 spec.
UF2_EXT_TAG_BOARD_ID = 0x4D7C3A
# --- end local addition ---

INFO_FILE = "/INFO_UF2.TXT"

appstartaddr = 0x2000
familyid = 0x0
extensions = b""  # local addition: extension-tag blob set from --ext


def is_uf2(buf):
    w = struct.unpack("<II", buf[0:8])
    return w[0] == UF2_MAGIC_START0 and w[1] == UF2_MAGIC_START1

def is_hex(buf):
    try:
        w = buf[0:30].decode("utf-8")
    except UnicodeDecodeError:
        return False
    if w[0] == ':' and re.match(rb"^[:0-9a-fA-F\r\n]+$", buf):
        return True
    return False

def convert_from_uf2(buf):
    global appstartaddr
    global familyid
    numblocks = len(buf) // 512
    curraddr = None
    currfamilyid = None
    families_found = {}
    prev_flag = None
    all_flags_same = True
    outp = []
    for blockno in range(numblocks):
        ptr = blockno * 512
        block = buf[ptr:ptr + 512]
        hd = struct.unpack(b"<IIIIIIII", block[0:32])
        if hd[0] != UF2_MAGIC_START0 or hd[1] != UF2_MAGIC_START1:
            print("Skipping block at " + ptr + "; bad magic")
            continue
        if hd[2] & 1:
            # NO-flash flag set; skip block
            continue
        datalen = hd[4]
        if datalen > 476:
            assert False, "Invalid UF2 data size at " + ptr
        newaddr = hd[3]
        if (hd[2] & 0x2000) and (currfamilyid == None):
            currfamilyid = hd[7]
        if curraddr == None or ((hd[2] & 0x2000) and hd[7] != currfamilyid):
            currfamilyid = hd[7]
            curraddr = newaddr
            if familyid == 0x0 or familyid == hd[7]:
                appstartaddr = newaddr
        padding = newaddr - curraddr
        if padding < 0:
            assert False, "Block out of order at " + ptr
        if padding > 10*1024*1024:
            assert False, "More than 10M of padding needed at " + ptr
        if padding % 4 != 0:
            assert False, "Non-word padding size at " + ptr
        while padding > 0:
            padding -= 4
            outp.append(b"\x00\x00\x00\x00")
        if familyid == 0x0 or ((hd[2] & 0x2000) and familyid == hd[7]):
            outp.append(block[32 : 32 + datalen])
        curraddr = newaddr + datalen
        if hd[2] & 0x2000:
            if hd[7] in families_found.keys():
                if families_found[hd[7]] > newaddr:
                    families_found[hd[7]] = newaddr
            else:
                families_found[hd[7]] = newaddr
        if prev_flag == None:
            prev_flag = hd[2]
        if prev_flag != hd[2]:
            all_flags_same = False
        if blockno == (numblocks - 1):
            print("--- UF2 File Header Info ---")
            families = load_families()
            for family_hex in families_found.keys():
                family_short_name = ""
                for name, value in families.items():
                    if value == family_hex:
                        family_short_name = name
                print("Family ID is {:s}, hex value is 0x{:08x}".format(family_short_name,family_hex))
                print("Target Address is 0x{:08x}".format(families_found[family_hex]))
            if all_flags_same:
                print("All block flag values consistent, 0x{:04x}".format(hd[2]))
            else:
                print("Flags were not all the same")
            print("----------------------------")
            if len(families_found) > 1 and familyid == 0x0:
                outp = []
                appstartaddr = 0x0
    return b"".join(outp)

def convert_to_carray(file_content):
    outp = "const unsigned long bindata_len = %d;\n" % len(file_content)
    outp += "const unsigned char bindata[] __attribute__((aligned(16))) = {"
    for i in range(len(file_content)):
        if i % 16 == 0:
            outp += "\n"
        outp += "0x%02x, " % file_content[i]
    outp += "\n};\n"
    return bytes(outp, "utf-8")

# --- local addition: extension-tag packing helpers (upstream has none) ---
def pack_extension_tag(tag, payload):
    """Pack one UF2 extension tag record, padded to a 4-byte boundary.

    Wire layout (per UF2 spec "Extension tags"):
        1 byte  total size (size field + 3-byte type + payload)
        3 bytes tag type, little-endian
        N bytes payload
        0-3   zero pad to a 4-byte boundary
    """
    size = 4 + len(payload)
    if size > 255:
        raise ValueError("extension tag too large (max 255 bytes incl. header)")
    if not (0 <= tag <= 0xFFFFFF):
        raise ValueError("extension tag type must be a 24-bit value")
    rec = bytes([size]) + tag.to_bytes(3, "little") + payload
    pad = (-len(rec)) % 4
    return rec + b"\x00" * pad


def build_extension_blob(specs):
    """Build the extension-tag region: packed tags + a 4-byte zero terminator.

    specs: list of (tag_type:int, payload:bytes). The result is placed right
    after the block payload (at data offset == payloadSize) in every block.
    """
    blob = bytearray()
    for tag, payload in specs:
        blob += pack_extension_tag(tag, payload)
    blob += b"\x00\x00\x00\x00"  # terminator record (size 0, type 0)
    return bytes(blob)
# --- end local addition ---


def convert_to_uf2(file_content):
    global familyid, extensions
    datapadding = b""
    while len(datapadding) < 512 - 256 - 32 - 4:
        datapadding += b"\x00\x00\x00\x00"
    # local addition: extension tags sit right after the 256-byte payload, so
    # shrink the trailing zero-fill by that many bytes.
    if extensions:
        if len(extensions) > len(datapadding):
            raise ValueError(
                "extension tags (%d bytes) do not fit in a UF2 block "
                "(max %d bytes after the 256-byte payload)"
                % (len(extensions), len(datapadding)))
        datapadding = datapadding[:len(datapadding) - len(extensions)]
    numblocks = (len(file_content) + 255) // 256
    outp = []
    for blockno in range(numblocks):
        ptr = 256 * blockno
        chunk = file_content[ptr:ptr + 256]
        flags = 0x0
        if familyid:
            flags |= 0x2000
        if extensions:  # local addition
            flags |= UF2_FLAG_EXTENSION_TAGS
        hd = struct.pack(b"<IIIIIIII",
            UF2_MAGIC_START0, UF2_MAGIC_START1,
            flags, ptr + appstartaddr, 256, blockno, numblocks, familyid)
        while len(chunk) < 256:
            chunk += b"\x00"
        block = hd + chunk + extensions + datapadding + struct.pack(b"<I", UF2_MAGIC_END)
        assert len(block) == 512
        outp.append(block)
    return b"".join(outp)

class Block:
    def __init__(self, addr, default_data=0xFF):
        self.addr = addr
        self.bytes = bytearray([default_data] * 256)

    def encode(self, blockno, numblocks):
        global familyid, extensions
        flags = 0x0
        if familyid:
            flags |= 0x2000
        if extensions:  # local addition
            flags |= UF2_FLAG_EXTENSION_TAGS
        hd = struct.pack("<IIIIIIII",
            UF2_MAGIC_START0, UF2_MAGIC_START1,
            flags, self.addr, 256, blockno, numblocks, familyid)
        hd += self.bytes[0:256]
        if extensions:  # local addition
            hd += extensions
        while len(hd) < 512 - 4:
            hd += b"\x00"
        hd += struct.pack("<I", UF2_MAGIC_END)
        return hd

def convert_from_hex_to_uf2(buf):
    global appstartaddr
    appstartaddr = None
    upper = 0
    currblock = None
    blocks = []
    for line in buf.split('\n'):
        if line[0] != ":":
            continue
        i = 1
        rec = []
        while i < len(line) - 1:
            rec.append(int(line[i:i+2], 16))
            i += 2
        tp = rec[3]
        if tp == 4:
            upper = ((rec[4] << 8) | rec[5]) << 16
        elif tp == 2:
            upper = ((rec[4] << 8) | rec[5]) << 4
        elif tp == 1:
            break
        elif tp == 0:
            addr = upper + ((rec[1] << 8) | rec[2])
            if appstartaddr == None:
                appstartaddr = addr
            i = 4
            while i < len(rec) - 1:
                if not currblock or currblock.addr & ~0xff != addr & ~0xff:
                    currblock = Block(addr & ~0xff)
                    blocks.append(currblock)
                currblock.bytes[addr & 0xff] = rec[i]
                addr += 1
                i += 1
    numblocks = len(blocks)
    resfile = b""
    for i in range(0, numblocks):
        resfile += blocks[i].encode(i, numblocks)
    return resfile

def to_str(b):
    return b.decode("utf-8")

def get_drives():
    drives = []
    if sys.platform == "win32":
        r = subprocess.check_output([
            "powershell",
            "-Command",
            '(Get-WmiObject Win32_LogicalDisk -Filter "FileSystem=\'FAT\'").DeviceID'
            ])
        drives = [drive.strip() for drive in to_str(r).splitlines()]
    else:
        searchpaths = ["/mnt", "/media"]
        if sys.platform == "darwin":
            searchpaths = ["/Volumes"]
        elif sys.platform == "linux":
            searchpaths += ["/media/" + os.environ["USER"], "/run/media/" + os.environ["USER"]]
            if "SUDO_USER" in os.environ.keys():
                searchpaths += ["/media/" + os.environ["SUDO_USER"]]
                searchpaths += ["/run/media/" + os.environ["SUDO_USER"]]

        for rootpath in searchpaths:
            if os.path.isdir(rootpath):
                for d in os.listdir(rootpath):
                    if os.path.isdir(os.path.join(rootpath, d)):
                        drives.append(os.path.join(rootpath, d))


    def has_info(d):
        try:
            return os.path.isfile(d + INFO_FILE)
        except:
            return False

    return list(filter(has_info, drives))


def board_id(path):
    with open(path + INFO_FILE, mode='r') as file:
        file_content = file.read()
    return re.search(r"Board-ID: ([^\r\n]*)", file_content).group(1)


def list_drives():
    for d in get_drives():
        print(d, board_id(d))


def write_file(name, buf):
    with open(name, "wb") as f:
        f.write(buf)
    print("Wrote %d bytes to %s" % (len(buf), name))


def load_families():
    # The expectation is that the `uf2families.json` file is in the same
    # directory as this script. Make a path that works using `__file__`
    # which contains the full path to this script.
    filename = "uf2families.json"
    pathname = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(pathname) as f:
        raw_families = json.load(f)

    families = {}
    for family in raw_families:
        families[family["short_name"]] = int(family["id"], 0)

    return families


def main():
    global appstartaddr, familyid, extensions
    def error(msg):
        print(msg, file=sys.stderr)
        sys.exit(1)
    parser = argparse.ArgumentParser(description='Convert to UF2 or flash directly.')
    parser.add_argument('input', metavar='INPUT', type=str, nargs='?',
                        help='input file (HEX, BIN or UF2)')
    parser.add_argument('-b', '--base', dest='base', type=str,
                        default="0x2000",
                        help='set base address of application for BIN format (default: 0x2000)')
    parser.add_argument('-f', '--family', dest='family', type=str,
                        default="0x0",
                        help='specify familyID - number or name (default: 0x0)')
    parser.add_argument('-o', '--output', metavar="FILE", dest='output', type=str,
                        help='write output to named file; defaults to "flash.uf2" or "flash.bin" where sensible')
    parser.add_argument('-d', '--device', dest="device_path",
                        help='select a device path to flash')
    parser.add_argument('-l', '--list', action='store_true',
                        help='list connected devices')
    parser.add_argument('-c', '--convert', action='store_true',
                        help='do not flash, just convert')
    parser.add_argument('-D', '--deploy', action='store_true',
                        help='just flash, do not convert')
    parser.add_argument('-w', '--wait', action='store_true',
                        help='wait for device to flash')
    parser.add_argument('-C', '--carray', action='store_true',
                        help='convert binary file to a C array, not UF2')
    parser.add_argument('-i', '--info', action='store_true',
                        help='display header information from UF2, do not convert')
    # local addition: extension tags (upstream has none)
    parser.add_argument('--ext', metavar='TYPE:VALUE', dest='ext',
                        action='append', default=[],
                        help='append a UF2 extension tag (flag 0x8000) to every '
                             'block. TYPE is a 24-bit hex tag type (e.g. '
                             '0x4D7C3A for the Adaboot board-id tag); VALUE is '
                             'the tag payload -- a UTF-8 string, or raw hex '
                             'bytes when prefixed with 0x. May be repeated to '
                             'add multiple tags. BIN input only.')
    # end local addition
    args = parser.parse_args()
    appstartaddr = int(args.base, 0)

    families = load_families()

    if args.family.upper() in families:
        familyid = families[args.family.upper()]
    else:
        try:
            familyid = int(args.family, 0)
        except ValueError:
            error("Family ID needs to be a number or one of: " + ", ".join(families.keys()))

    # local addition: parse --ext TYPE:VALUE into the extension-tag blob.
    ext_specs = []
    for spec in args.ext:
        if ":" not in spec:
            error("--ext expects TYPE:VALUE, got %r" % spec)
        typ, val = spec.split(":", 1)
        try:
            tag = int(typ, 0)
        except ValueError:
            error("--ext TYPE must be a hex number, got %r" % typ)
        if not (0 <= tag <= 0xFFFFFF):
            error("--ext TYPE must be a 24-bit value, got 0x%x" % tag)
        if val[:2].lower() == "0x":
            try:
                payload = bytes.fromhex(val[2:])
            except ValueError:
                error("--ext VALUE hex is invalid: %r" % val)
        else:
            payload = val.encode("utf-8")
        ext_specs.append((tag, payload))
    try:
        extensions = build_extension_blob(ext_specs) if ext_specs else b""
    except ValueError as e:
        error(str(e))
    if extensions and len(extensions) > UF2_EXT_MAX_BYTES:
        error("extension tags total %d bytes; max %d after the 256-byte payload"
              % (len(extensions), UF2_EXT_MAX_BYTES))
    # end local addition

    if args.list:
        list_drives()
    else:
        if not args.input:
            error("Need input file")
        with open(args.input, mode='rb') as f:
            inpbuf = f.read()
        from_uf2 = is_uf2(inpbuf)
        ext = "uf2"
        if args.deploy:
            outbuf = inpbuf
        elif from_uf2 and not args.info:
            outbuf = convert_from_uf2(inpbuf)
            ext = "bin"
        elif from_uf2 and args.info:
            outbuf = ""
            convert_from_uf2(inpbuf)
        elif is_hex(inpbuf):
            outbuf = convert_from_hex_to_uf2(inpbuf.decode("utf-8"))
        elif args.carray:
            outbuf = convert_to_carray(inpbuf)
            ext = "h"
        else:
            outbuf = convert_to_uf2(inpbuf)
        if not args.deploy and not args.info:
            print("Converted to %s, output size: %d, start address: 0x%x" %
                  (ext, len(outbuf), appstartaddr))
        if args.convert or ext != "uf2":
            if args.output == None:
                args.output = "flash." + ext
        if args.output:
            write_file(args.output, outbuf)
        if ext == "uf2" and not args.convert and not args.info:
            drives = get_drives()
            if len(drives) == 0:
                if args.wait:
                    print("Waiting for drive to deploy...")
                    while len(drives) == 0:
                        sleep(0.1)
                        drives = get_drives()
                elif not args.output:
                    error("No drive to deploy.")
            for d in drives:
                print("Flashing %s (%s)" % (d, board_id(d)))
                write_file(d + "/NEW.UF2", outbuf)


if __name__ == "__main__":
    main()
