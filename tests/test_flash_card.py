"""Tests for tools/flash_card.py's choice of SD destinations (the safety check before anything is written).

The fields are the ones `diskutil info -plist` reports. No disk is touched.
Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from pathlib import Path

os.environ["FPP_BACKUP_KEYCHAIN"] = "off"  # tests never touch the Keychain (see tools/fpp_http.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import flash_card  # noqa: E402

DISKS = {
    # The Mac's own SSD: never a destination.
    "disk0": {"WholeDisk": True, "Internal": True, "RemovableMedia": False, "Ejectable": False,
              "BusProtocol": "Apple Fabric", "TotalSize": 2_001_111_162_880},
    # A card in the Mac's built-in SD slot: internal, but removable media.
    "disk4": {"WholeDisk": True, "Internal": True, "RemovableMedia": True, "Ejectable": True,
              "BusProtocol": "Secure Digital", "TotalSize": 31_914_983_424, "WritableMedia": True},
    # A card in a USB card reader.
    "disk5": {"WholeDisk": True, "Internal": False, "RemovableMedia": True, "Ejectable": True,
              "BusProtocol": "USB", "TotalSize": 63_864_569_856, "WritableMedia": True},
    # A mounted disk image: virtual, never a destination.
    "disk6": {"WholeDisk": True, "Internal": False, "RemovableMedia": True, "Ejectable": True,
              "BusProtocol": "Virtual Interface", "TotalSize": 7_340_032_000},
    # A partition, not a whole disk.
    "disk5s1": {"WholeDisk": False, "RemovableMedia": True, "Ejectable": True, "BusProtocol": "USB"},
    # An internal second drive that is neither removable nor ejectable.
    "disk7": {"WholeDisk": True, "Internal": True, "RemovableMedia": False, "Ejectable": False,
              "BusProtocol": "PCI-Express", "TotalSize": 500_107_862_016},
}


class SdDestinations(unittest.TestCase):
    def test_only_removable_physical_whole_disks(self):
        got = flash_card.sd_destinations(disks=list(DISKS), info_for=DISKS.__getitem__)
        self.assertEqual(got, ["/dev/rdisk4", "/dev/rdisk5"])

    def test_missing_fields_are_not_a_destination(self):
        self.assertFalse(flash_card.is_sd_destination({}))
        self.assertFalse(flash_card.is_sd_destination({"WholeDisk": True}))


if __name__ == "__main__":
    unittest.main()
