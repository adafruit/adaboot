- Serial recovery: added the mcumgr OS group "Bootloader information" command
  (command ID 8, gated by `CONFIG_BOOT_MGMT_BOOTLOADER_INFO`, no default),
  matching the command Zephyr's SMP server provides from the application side.
  An empty request reports the bootloader name (`"bootloader": "Adaboot"`);
  a `"mode"` query reports the operating mode as a value of `enum mcuboot_mode`
  (mirroring the BLINFO_MODE TLV `boot_record.c` writes); a `"slot"` query
  reports the active slot (BLINFO_RUNNING_SLOT semantics).