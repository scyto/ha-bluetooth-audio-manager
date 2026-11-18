"""Reset the USB device behind a Bluetooth adapter — the software way of
unplugging it and plugging it back in.

``USBDEVFS_RESET`` makes the kernel reset the device and rebind its driver:
btusb lets go of the adapter, probes it again, and the controller firmware
is loaded anew — the same as a replug as far as the host can tell.  The one
difference: the stick keeps its 5 V supply, so a fault that only a power cut
clears is not cleared by this.

Needs write access to ``/dev/bus/usb/BBB/DDD`` (``usb: true`` in the app
config, plus an AppArmor rule) and read access to the adapter's sysfs
entries to find that node.
"""

from __future__ import annotations

import fcntl
import os

USBDEVFS_RESET = 0x5514  # _IO('U', 20) from <linux/usbdevice_fs.h>
SYSFS = "/sys"
DEVFS_USB = "/dev/bus/usb"


class UsbResetError(Exception):
    """The adapter's USB device could not be found or reset."""


def _read_int(path: str) -> int:
    with open(path) as f:
        return int(f.read().strip())


def usb_device_for_hci(hci: str, sysfs: str = SYSFS) -> tuple[int, int, str]:
    """``(busnum, devnum, sysfs_dir)`` of the USB device behind *hci* (e.g. ``hci1``).

    ``/sys/class/bluetooth/hciN/device`` points at the USB *interface*
    (``1-8.4:1.0``); its parent (``1-8.4``) is the device and carries
    ``busnum`` and ``devnum``.
    """
    link = os.path.join(sysfs, "class", "bluetooth", hci, "device")
    if not os.path.exists(link):
        raise UsbResetError(f"{hci} not found in sysfs")
    current = os.path.realpath(link)
    for _ in range(4):  # interface → device, with a little slack
        busnum = os.path.join(current, "busnum")
        devnum = os.path.join(current, "devnum")
        if os.path.isfile(busnum) and os.path.isfile(devnum):
            try:
                return _read_int(busnum), _read_int(devnum), current
            except (OSError, ValueError) as exc:
                raise UsbResetError(f"cannot read the USB address of {hci}: {exc}") from exc
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise UsbResetError(f"{hci} is not a USB adapter")


def usb_device_node(busnum: int, devnum: int, devfs: str = DEVFS_USB) -> str:
    return os.path.join(devfs, f"{busnum:03d}", f"{devnum:03d}")


def check_usb_device_access(busnum: int, devnum: int, devfs: str = DEVFS_USB) -> str:
    """Open the device node for writing and close it again; returns the node.

    A real open rather than ``os.access``: AppArmor only mediates opens, so
    ``access()`` can say yes where the reset itself would be refused.
    Opening the node does not touch the device.
    """
    node = usb_device_node(busnum, devnum, devfs)
    try:
        fd = os.open(node, os.O_WRONLY)
    except OSError as exc:
        raise UsbResetError(
            f"no write access to {node} ({exc.strerror or exc}) — the app needs "
            "USB access (usb: true and the AppArmor rule for /dev/bus/usb)"
        ) from exc
    os.close(fd)
    return node


def reset_usb_device(busnum: int, devnum: int, devfs: str = DEVFS_USB) -> str:
    """Issue ``USBDEVFS_RESET`` on the device node; returns the node path.

    Blocking (the kernel waits for the device to come back), so call it in a
    thread.  Raises ``OSError`` when the node cannot be opened or the reset
    is refused.
    """
    node = usb_device_node(busnum, devnum, devfs)
    fd = os.open(node, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)
    return node
