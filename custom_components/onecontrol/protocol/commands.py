"""Command builder for OneControl MyRvLink protocol.

Builds raw command byte arrays for COBS-encoding and BLE transmission.
A monotonic ``command_id`` counter ensures each command has a unique 16-bit
sequence number (LE order) for correlating ack events (0x02).

Wire format for all commands:
  [CmdId_LSB][CmdId_MSB][CommandType][...payload...]

Reference: INTERNALS.md § Command Building, MyRvLinkCommandBuilder.kt
"""

from __future__ import annotations

import struct
import threading


class CommandBuilder:
    """Construct OneControl MyRvLink command byte arrays."""

    # Command type constants
    CMD_GET_DEVICES = 0x01
    CMD_GET_DEVICES_METADATA = 0x02
    CMD_ACTION_SWITCH = 0x40
    CMD_ACTION_HBRIDGE = 0x41
    CMD_ACTION_GENERATOR = 0x42
    CMD_ACTION_DIMMABLE = 0x43
    CMD_ACTION_RGB = 0x44
    CMD_ACTION_HVAC = 0x45

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._command_id: int = 0

    def _next_id(self) -> int:
        """Return next 16-bit command ID (wraps at 0xFFFF)."""
        with self._lock:
            cid = self._command_id
            self._command_id = (self._command_id + 1) & 0xFFFF
            return cid

    @staticmethod
    def _id_bytes(cid: int) -> bytes:
        return struct.pack("<H", cid & 0xFFFF)

    # ------------------------------------------------------------------
    # GetDevices (0x01)
    # ------------------------------------------------------------------

    def build_get_devices(self, device_table_id: int) -> bytes:
        """Build a GetDevices command (6 bytes).

        Requests the gateway to broadcast status for all known devices
        in the given device table.
        """
        cid = self._next_id()
        return (
            self._id_bytes(cid)
            + bytes([self.CMD_GET_DEVICES, device_table_id & 0xFF, 0x00, 0xFF])
        )

    # ------------------------------------------------------------------
    # GetDevicesMetadata (0x02)
    # ------------------------------------------------------------------

    def build_get_devices_metadata(
        self, device_table_id: int, start_id: int = 0, count: int = 0xFF
    ) -> bytes:
        """Build a GetDevicesMetadata command (6 bytes).

        Response arrives as event type 0x02 with function_name / instance
        per device (big-endian function name field).

        Reference: INTERNALS.md § Device Metadata Retrieval
        """
        cid = self._next_id()
        return (
            self._id_bytes(cid)
            + bytes([
                self.CMD_GET_DEVICES_METADATA,
                device_table_id & 0xFF,
                start_id & 0xFF,
                count & 0xFF,
            ])
        )

    # ------------------------------------------------------------------
    # Switch (0x40) — relay on/off for one or more devices
    # ------------------------------------------------------------------

    def build_action_switch(
        self, device_table_id: int, state: bool, device_ids: list[int]
    ) -> bytes:
        """Build an ActionSwitch command.

        ``device_ids`` — list of device IDs to set.  Allows bulk-switching
        multiple relays on the same table in a single BLE write.
        """
        cid = self._next_id()
        state_byte = 0x01 if state else 0x00
        header = (
            self._id_bytes(cid)
            + bytes([self.CMD_ACTION_SWITCH, device_table_id & 0xFF, state_byte])
        )
        return header + bytes(d & 0xFF for d in device_ids)

    # ------------------------------------------------------------------
    # Dimmable Light (0x43)
    # ------------------------------------------------------------------

    def build_action_dimmable(
        self,
        device_table_id: int,
        device_id: int,
        brightness: int,
    ) -> bytes:
        """Build an ActionDimmable command (8 bytes).

        ``brightness`` 0 → off (mode=0x00), 1-255 → on (mode=0x01).
        """
        cid = self._next_id()
        mode = 0x00 if brightness == 0 else 0x01
        return (
            self._id_bytes(cid)
            + bytes([
                self.CMD_ACTION_DIMMABLE,
                device_table_id & 0xFF,
                device_id & 0xFF,
                mode,
                min(max(brightness, 0), 255),
                0x00,  # reserved
            ])
        )

    # ------------------------------------------------------------------
    # HVAC (0x45)
    # ------------------------------------------------------------------

    def build_action_hvac(
        self,
        device_table_id: int,
        device_id: int,
        heat_mode: int = 0,
        heat_source: int = 0,
        fan_mode: int = 0,
        low_trip_f: int = 65,
        high_trip_f: int = 78,
    ) -> bytes:
        """Build an ActionHvac command (8 bytes).

        Command byte packing (from INTERNALS.md):
          bits 0-2 = heat_mode  (0=Off,1=Heat,2=Cool,3=Both,4=Schedule)
          bits 4-5 = heat_source (0=Gas,1=HeatPump,2=Other)
          bits 6-7 = fan_mode   (0=Auto,1=High,2=Low)
        """
        cid = self._next_id()
        cmd_byte = (
            (heat_mode & 0x07)
            | ((heat_source & 0x03) << 4)
            | ((fan_mode & 0x03) << 6)
        )
        return (
            self._id_bytes(cid)
            + bytes([
                self.CMD_ACTION_HVAC,
                device_table_id & 0xFF,
                device_id & 0xFF,
                cmd_byte & 0xFF,
                min(max(low_trip_f, 0), 255),
                min(max(high_trip_f, 0), 255),
            ])
        )

    # ------------------------------------------------------------------
    # Generator Genie (0x42) — start/stop
    # ------------------------------------------------------------------

    def build_action_generator(
        self, device_table_id: int, device_id: int, run: bool
    ) -> bytes:
        """Build an ActionGeneratorGenie command (6 bytes)."""
        cid = self._next_id()
        state_byte = 0x01 if run else 0x00
        return (
            self._id_bytes(cid)
            + bytes([
                self.CMD_ACTION_GENERATOR,
                device_table_id & 0xFF,
                device_id & 0xFF,
                state_byte,
            ])
        )

    # ------------------------------------------------------------------
    # RGB Light (0x44) — variable length per mode
    # ------------------------------------------------------------------

    # RGB mode constants
    RGB_MODE_OFF = 0x00
    RGB_MODE_SOLID = 0x01
    RGB_MODE_BLINK = 0x02
    RGB_MODE_TRANSITION_SOLID = 0x04
    RGB_MODE_TRANSITION_BLINK = 0x05
    RGB_MODE_TRANSITION_BREATHE = 0x06
    RGB_MODE_TRANSITION_MARQUEE = 0x07
    RGB_MODE_TRANSITION_RAINBOW = 0x08
    RGB_MODE_RESTORE = 0x7F

    def build_action_rgb(
        self,
        device_table_id: int,
        device_id: int,
        mode: int = 0x01,
        red: int = 255,
        green: int = 255,
        blue: int = 255,
        auto_off: int = 0,
        blink_on_interval: int = 0,
        blink_off_interval: int = 0,
        transition_interval: int = 1000,
    ) -> bytes:
        """Build an ActionRgb command (variable length by mode).

        Reference: Android RgbCommandBuilder.kt

        Modes:
          0x00 (Off), 0x7F (Restore) → 6 bytes (header only)
          0x01 (Solid) → 10 bytes: header + R + G + B + autoOff
          0x02 (Blink) → 12 bytes: header + R + G + B + autoOff + onIntv + offIntv
          0x04-0x08 (Transitions) → 9 bytes: header + autoOff + interval(BE16)
        """
        cid = self._next_id()
        header = (
            self._id_bytes(cid)
            + bytes([
                self.CMD_ACTION_RGB,
                device_table_id & 0xFF,
                device_id & 0xFF,
                mode & 0xFF,
            ])
        )

        if mode in (self.RGB_MODE_OFF, self.RGB_MODE_RESTORE):
            # Off / Restore — header only (6 bytes)
            return header

        if mode == self.RGB_MODE_BLINK:
            # Blink — 12 bytes: header + R + G + B + autoOff + onInterval + offInterval
            return header + bytes([
                red & 0xFF,
                green & 0xFF,
                blue & 0xFF,
                auto_off & 0xFF,
                blink_on_interval & 0xFF,
                blink_off_interval & 0xFF,
            ])

        if mode >= self.RGB_MODE_TRANSITION_SOLID:
            # Transitions (4-8) — 9 bytes: header + autoOff + interval(BE16)
            return header + bytes([
                auto_off & 0xFF,
                (transition_interval >> 8) & 0xFF,
                transition_interval & 0xFF,
            ])

        # Solid (0x01) — 10 bytes: header + R + G + B + autoOff
        return header + bytes([
            red & 0xFF,
            green & 0xFF,
            blue & 0xFF,
            auto_off & 0xFF,
        ])

    # ------------------------------------------------------------------
    # Dimmable Light Effect (0x43 extended — 12-byte blink/swell)
    # ------------------------------------------------------------------

    # Dimmable effect mode constants
    DIMMABLE_MODE_OFF = 0x00
    DIMMABLE_MODE_ON = 0x01
    DIMMABLE_MODE_BLINK = 0x02
    DIMMABLE_MODE_SWELL = 0x03

    def build_action_dimmable_effect(
        self,
        device_table_id: int,
        device_id: int,
        mode: int = 0x02,
        brightness: int = 255,
        duration: int = 0,
        cycle_time1: int = 1055,
        cycle_time2: int = 1055,
    ) -> bytes:
        """Build an ActionDimmable effect command (12 bytes).

        Reference: Android DimmableCommandBuilder.kt (effect path)

        ``mode`` — 2=Blink, 3=Swell
        ``duration`` — 0 = infinite, else minutes
        ``cycle_time1`` / ``cycle_time2`` — milliseconds (BE16)
        Speed presets: Fast=220ms, Medium=1055ms, Slow=2447ms
        """
        cid = self._next_id()
        return (
            self._id_bytes(cid)
            + bytes([
                self.CMD_ACTION_DIMMABLE,
                device_table_id & 0xFF,
                device_id & 0xFF,
                mode & 0xFF,
                min(max(brightness, 0), 255),
                duration & 0xFF,
                (cycle_time1 >> 8) & 0xFF,
                cycle_time1 & 0xFF,
                (cycle_time2 >> 8) & 0xFF,
                cycle_time2 & 0xFF,
            ])
        )
