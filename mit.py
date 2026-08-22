import signal
import sys
import math
import re
import xml.etree.ElementTree as ET
import requests
from dataclasses import dataclass
from typing import Optional, List, Dict, Union
from enum import Enum


class _RequestTimedOut(Exception):
    """Internal signal-alarm sentinel; never escapes this module."""
    pass


class MNetHTTPError(Exception):
    """Gateway rejected a request with an HTTP error status."""
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class DriveState(Enum):
    ON = "ON"
    OFF = "OFF"

class Mode(Enum):
    COOL = "COOL"
    AUTOCOOL = "AUTOCOOL"
    HEAT = "HEAT"
    FAN = "FAN"
    DRY = "DRY"
    AUTO = "AUTO"
    # Read-only statuses reported while AUTO is selecting a direction.
    AUTOHEAT = "AUTOHEAT"

class FanSpeed(Enum):
    HIGH = "HIGH"
    LOW = "LOW"
    MID1 = "MID1"
    MID2 = "MID2"
    AUTO = "AUTO"
    # Likely also: LOW, AUTO


class AirDirection(Enum):
    HORIZONTAL = "HORIZONTAL"
    MID0 = "MID0"
    MID1 = "MID1"
    MID2 = "MID2"
    VERTICAL = "VERTICAL"
    SWING = "SWING"

@dataclass
class HVACUnit:
    """Represents the state of an HVAC unit"""
    group: int
    drive: Optional[DriveState] = None
    mode: Optional[Mode] = None
    set_temp: Optional[int] = None
    fan_speed: Optional[FanSpeed] = None
    air_direction: Optional[AirDirection] = None
    inlet_temp: Optional[float] = None  # Read-only
    filter_sign: Optional[str] = None
    error_sign: Optional[str] = None

    def to_mnet_attrs(self) -> Dict[str, str]:
        """Convert to M-NET XML attributes"""
        attrs = {"Group": str(self.group)}

        if self.drive is not None:
            attrs["Drive"] = self.drive.value
        if self.mode is not None:
            attrs["Mode"] = self.mode.value
        if self.set_temp is not None:
            attrs["SetTemp"] = str(self.set_temp)
        if self.fan_speed is not None:
            attrs["FanSpeed"] = self.fan_speed.value
        if self.air_direction is not None:
            attrs["AirDirection"] = self.air_direction.value
        if self.inlet_temp is not None:
            attrs["InletTemp"] = str(self.inlet_temp)

        return attrs

    @classmethod
    def from_mnet_attrs(cls, attrs: Dict[str, str]) -> 'HVACUnit':
        """Create HVACUnit from M-NET XML attributes"""
        unit = cls(group=int(attrs["Group"]))

        if "Drive" in attrs and attrs["Drive"] != "*":
            try:
                unit.drive = DriveState(attrs["Drive"])
            except ValueError:
                pass
        if "Mode" in attrs and attrs["Mode"] != "*":
            try:
                unit.mode = Mode(attrs["Mode"])
            except ValueError:
                pass
        if "SetTemp" in attrs and attrs["SetTemp"] != "*":
            try:
                set_temp = float(attrs["SetTemp"])
                if math.isfinite(set_temp) and set_temp.is_integer():
                    unit.set_temp = int(set_temp)
            except ValueError:
                pass
        if "FanSpeed" in attrs and attrs["FanSpeed"] != "*":
            try:
                unit.fan_speed = FanSpeed(attrs["FanSpeed"])
            except ValueError:
                pass
        if "AirDirection" in attrs and attrs["AirDirection"] != "*":
            try:
                unit.air_direction = AirDirection(attrs["AirDirection"])
            except ValueError:
                pass
        if "InletTemp" in attrs and attrs["InletTemp"] != "*":
            try:
                inlet_temp = float(attrs["InletTemp"])
                if math.isfinite(inlet_temp):
                    unit.inlet_temp = inlet_temp
            except ValueError:
                pass
        if attrs.get("FilterSign") not in (None, "*"):
            unit.filter_sign = attrs["FilterSign"]
        if attrs.get("ErrorSign") not in (None, "*"):
            unit.error_sign = attrs["ErrorSign"]

        return unit

class MNetController:
    """M-NET HVAC Controller"""

    # (connect timeout, read timeout) in seconds. Both are inactivity
    # timeouts - read resets on every chunk received, so on their own they
    # don't bound total request duration against a slow trickle. That's
    # what MAX_REQUEST_SECONDS below is for.
    REQUEST_TIMEOUT = (5, 10)

    # Real status/set responses are a few KB of XML; this is generous
    # headroom while still bounding memory use against a misbehaving or
    # malicious endpoint.
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    # Hard wall-clock ceiling for the whole request. A sender that keeps
    # individual gaps under the read timeout (e.g. one byte every few
    # seconds) could otherwise hold the connection open indefinitely while
    # staying under the byte cap - checking elapsed time between
    # iter_content() chunks doesn't reliably catch this, since a
    # content-length-less, non-chunked response can leave iter_content
    # blocked well past a single chunk's read timeout before it yields
    # anything at all. SIGALRM interrupts the blocking call directly
    # regardless of how requests/urllib3 buffers internally. Only safe to
    # call from the main thread - fine here, this is a short-lived,
    # single-threaded CLI helper, not a library meant for embedding in a
    # multi-threaded app.
    MAX_REQUEST_SECONDS = 15
    MAX_GROUP = 50

    def __init__(self, host: str = "192.168.1.2", port: int = 80):
        if not isinstance(host, str) or not host or re.search(r"[\s/?#@]", host):
            raise ValueError("Invalid M-NET gateway host")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("M-NET gateway port must be between 1 and 65535")
        self.host = host
        self.port = port
        self.debug = False
        url_host = host
        if ":" in host and not (host.startswith("[") and host.endswith("]")):
            url_host = f"[{host}]"
        self.url = f"http://{url_host}:{port}/servlet/MIMEReceiveServlet"
        self.headers = {
            "Content-Type": "text/xml",
            "Accept": "text/xml",
            "Connection": "close"
        }

    def _create_packet(self, command: str, units: List[HVACUnit]) -> str:
        """Create XML packet for M-NET communication"""
        packet = ET.Element("Packet")

        # Add command
        cmd_elem = ET.SubElement(packet, "Command")
        cmd_elem.text = command

        # Add database manager
        db_manager = ET.SubElement(packet, "DatabaseManager")

        # Add Mnet elements
        for unit in units:
            self._validate_group(unit.group)
            mnet = ET.SubElement(db_manager, "Mnet")
            for key, value in unit.to_mnet_attrs().items():
                mnet.set(key, value)

        # Format XML
        ET.indent(packet, space="  ")
        return ET.tostring(packet, encoding='unicode', xml_declaration=True)

    @classmethod
    def _validate_group(cls, group: int) -> None:
        if not isinstance(group, int) or isinstance(group, bool) or not 1 <= group <= cls.MAX_GROUP:
            raise ValueError(f"M-NET group must be between 1 and {cls.MAX_GROUP}")

    def _send_request(self, xml_data: str) -> requests.Response:
        """Send XML request to M-NET device"""
        old_delay, _ = signal.getitimer(signal.ITIMER_REAL)
        if old_delay > 0:
            # ITIMER_REAL is process-global. Refuse the request instead of
            # delaying the caller's alarm or weakening our wall-clock cap.
            raise RuntimeError(
                "Cannot start M-NET request while ITIMER_REAL is already in use"
            )
        if signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, set()):
            raise RuntimeError(
                "Cannot start M-NET request while SIGALRM is blocked"
            )
        if signal.SIGALRM in signal.sigpending():
            raise RuntimeError(
                "Cannot start M-NET request while SIGALRM is pending"
            )

        old_handler = signal.getsignal(signal.SIGALRM)
        response = None
        cleanup_error = None
        primary_error = None
        old_signal_mask = None

        def _on_alarm(signum, frame):
            raise _RequestTimedOut()

        try:
            signal.signal(signal.SIGALRM, _on_alarm)
            signal.setitimer(signal.ITIMER_REAL, self.MAX_REQUEST_SECONDS, 0.0)

            response = requests.post(
                self.url,
                headers=self.headers,
                data=xml_data,
                timeout=self.REQUEST_TIMEOUT,
                stream=True,
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                location = response.headers.get("Location", "<none>")
                raise ValueError(f"M-NET gateway returned unexpected redirect to {location!r}")
            response.raise_for_status()

            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=8192):
                total += len(chunk)
                if total > self.MAX_RESPONSE_BYTES:
                    raise ValueError(
                        f"M-NET response exceeded {self.MAX_RESPONSE_BYTES} byte limit"
                    )
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response._content_consumed = True

            if self.debug:
                print("Request XML:")
                print(xml_data)
                print("Response XML:")
                print(response.text)
            return response
        except _RequestTimedOut:
            raise Exception(f"M-NET request exceeded {self.MAX_REQUEST_SECONDS}s wall-clock limit")
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            raise MNetHTTPError(status, f"M-NET communication failed: {e}") from e
        except requests.RequestException as e:
            raise Exception(f"M-NET communication failed: {e}")
        finally:
            try:
                primary_error = sys.exc_info()[0]
                try:
                    if response is not None:
                        response.close()
                finally:
                    # Keep SIGALRM active through close(), then atomically
                    # block it before restoring process-global signal state.
                    old_signal_mask = signal.pthread_sigmask(
                        signal.SIG_BLOCK, {signal.SIGALRM}
                    )
            except _RequestTimedOut:
                if primary_error is None:
                    cleanup_error = Exception(
                        f"M-NET request exceeded {self.MAX_REQUEST_SECONDS}s wall-clock limit during cleanup"
                    )
            except Exception as e:
                if primary_error is None:
                    cleanup_error = Exception(f"M-NET communication failed while closing response: {e}")
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0, 0.0)
                if signal.SIGALRM in signal.sigpending():
                    signal.sigwait({signal.SIGALRM})
                signal.signal(signal.SIGALRM, old_handler)
                if old_signal_mask is not None:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_signal_mask)

            if cleanup_error is not None:
                raise cleanup_error

    def _parse_response(self, xml_response: str) -> List[HVACUnit]:
        """Parse XML response into HVACUnit objects"""
        # M-NET responses have no DTD. Rejecting one prevents internal entity
        # expansion without adding another runtime dependency.
        if "<!DOCTYPE" in xml_response.upper():
            raise Exception("M-NET response contains a forbidden DTD")

        try:
            root = ET.fromstring(xml_response)
            units = []

            for mnet in root.findall(".//Mnet"):
                try:
                    unit = HVACUnit.from_mnet_attrs(mnet.attrib)
                    if any((unit.drive, unit.mode, unit.set_temp is not None,
                            unit.fan_speed, unit.air_direction,
                            unit.inlet_temp is not None, unit.filter_sign,
                            unit.error_sign)):
                        units.append(unit)
                except (KeyError, TypeError, ValueError):
                    # A single malformed group must not discard every other
                    # valid zone in the same status response.
                    continue

            return units
        except ET.ParseError as e:
            raise Exception(f"Failed to parse M-NET response: {e}")

    def _parse_xml(self, xml_response: str) -> ET.Element:
        if "<!DOCTYPE" in xml_response.upper():
            raise Exception("M-NET response contains a forbidden DTD")
        try:
            return ET.fromstring(xml_response)
        except ET.ParseError as e:
            raise Exception(f"Failed to parse M-NET response: {e}") from e

    def get_group_inventory(self) -> Dict[int, Dict[str, object]]:
        """Return configured groups, controller names, models and addresses."""
        packet = (
            "<Packet><Command>getRequest</Command><DatabaseManager>"
            "<ControlGroup><MnetGroupList/><MnetList/></ControlGroup>"
            "</DatabaseManager></Packet>"
        )
        root = self._parse_xml(self._send_request(packet).text)
        groups: Dict[int, Dict[str, object]] = {}
        for record in root.findall(".//MnetGroupRecord"):
            try:
                group = int(record.get("Group", ""))
                self._validate_group(group)
            except (TypeError, ValueError):
                continue
            groups[group] = {
                "name": None,
                "model": record.get("Model"),
                "submodel": record.get("SubModel"),
                "address": record.get("Address"),
            }
        for record in root.findall(".//MnetRecord"):
            try:
                group = int(record.get("Group", ""))
                self._validate_group(group)
            except (TypeError, ValueError):
                continue
            groups.setdefault(group, {
                "name": None, "model": None, "submodel": None, "address": None,
            })
            groups[group]["name"] = (
                record.get("GroupNameWeb") or record.get("GroupNameLcd") or None
            )
        if not groups:
            raise RuntimeError("M-NET gateway returned no group inventory")
        return groups

    def get_group_names(self) -> Dict[int, Dict[str, object]]:
        """Older-firmware fallback that discovers groups through MnetList."""
        packet = (
            "<Packet><Command>getRequest</Command><DatabaseManager>"
            "<ControlGroup><MnetList/></ControlGroup>"
            "</DatabaseManager></Packet>"
        )
        root = self._parse_xml(self._send_request(packet).text)
        groups = {}
        for record in root.findall(".//MnetRecord"):
            try:
                group = int(record.get("Group", ""))
                self._validate_group(group)
            except (TypeError, ValueError):
                continue
            groups[group] = {
                "name": record.get("GroupNameWeb") or record.get("GroupNameLcd") or None,
                "model": None, "submodel": None, "address": None,
            }
        if not groups:
            raise RuntimeError("M-NET gateway returned no group names")
        return groups

    def get_group_models(self, groups: List[int]) -> Dict[int, str]:
        """Read Model for specified groups."""
        if not groups:
            return {}
        packet = ET.Element("Packet")
        ET.SubElement(packet, "Command").text = "getRequest"
        manager = ET.SubElement(packet, "DatabaseManager")
        for group in groups:
            self._validate_group(group)
            mnet = ET.SubElement(manager, "Mnet", Group=str(group))
            mnet.set("Model", "*")
        response = self._send_request(
            ET.tostring(packet, encoding="unicode", xml_declaration=True)
        )
        root = self._parse_xml(response.text)
        result = {}
        for mnet in root.findall(".//Mnet"):
            try:
                group = int(mnet.get("Group", ""))
                self._validate_group(group)
            except (TypeError, ValueError):
                continue
            model = mnet.get("Model")
            if model is not None and model != "*" and group in groups:
                result[group] = model
        return result

    def get_capabilities(self, groups: List[int]) -> Dict[int, Dict[str, str]]:
        """Read non-destructive per-group capability flags."""
        if not groups:
            return {}
        attributes = (
            "Model", "IcKind", "ModeStatus", "FanSpeedSW", "FanAutoSW",
            "AutoModeSW", "FanModeSW", "DryModeSW", "AirDirectionSW",
            "AirStageSW", "SwingSW", "FilterSign", "ErrorSign",
        )
        packet = ET.Element("Packet")
        ET.SubElement(packet, "Command").text = "getRequest"
        manager = ET.SubElement(packet, "DatabaseManager")
        for group in groups:
            self._validate_group(group)
            mnet = ET.SubElement(manager, "Mnet", Group=str(group))
            for attribute in attributes:
                mnet.set(attribute, "*")
        response = self._send_request(
            ET.tostring(packet, encoding="unicode", xml_declaration=True)
        )
        root = self._parse_xml(response.text)
        result = {}
        for mnet in root.findall(".//Mnet"):
            try:
                group = int(mnet.get("Group", ""))
            except (TypeError, ValueError):
                continue
            if group in groups:
                result[group] = dict(mnet.attrib)
        return result

    def get_alarm_status(self) -> List[Dict[str, object]]:
        """Return the controller's active detailed M-NET alarm records."""
        packet = (
            "<Packet><Command>getRequest</Command><DatabaseManager>"
            "<Mnet><AlarmStatusList/></Mnet>"
            "</DatabaseManager></Packet>"
        )
        root = self._parse_xml(self._send_request(packet).text)
        alarms = []
        for record in root.findall(".//AlarmStatusRecord"):
            try:
                address = int(record.get("Address", ""))
            except (TypeError, ValueError):
                continue
            alarm = {
                "address": address,
                "group": None,
                "model": record.get("Model"),
                "alarm_code": record.get("AlarmCode"),
                "detected": record.get("Detect"),
                "recovered": record.get("Recover"),
            }
            try:
                alarm["group"] = int(record.get("Group", ""))
            except (TypeError, ValueError):
                pass
            alarms.append(alarm)
        return alarms

    @staticmethod
    def _match_attribute(key: str, actual: Optional[str], expected: str) -> bool:
        if actual is None:
            return False
        if key == "SetTemp":
            try:
                actual_f = float(actual)
                expected_f = float(expected)
                if not (math.isfinite(actual_f) and math.isfinite(expected_f)):
                    return False
                return actual_f == expected_f
            except (ValueError, TypeError):
                return False
        return actual == expected

    def _set_confirmed(self, xml_response: str, group: int, **expected: str) -> bool:
        """Return whether a setResponse confirms this group and every value."""
        if "<!DOCTYPE" in xml_response.upper():
            return False
        try:
            root = ET.fromstring(xml_response)
        except ET.ParseError:
            return False
        if root.findtext("./Command", default="").strip() != "setResponse":
            return False
        return any(
            mnet.get("Group") == str(group)
            and all(self._match_attribute(key, mnet.get(key), value) for key, value in expected.items())
            for mnet in root.findall(".//Mnet")
        )

    # === CONTROL METHODS ===
    def turn_on(self, group: int) -> bool:
        """Turn on HVAC unit"""
        unit = HVACUnit(group=group, drive=DriveState.ON)
        xml_data = self._create_packet("setRequest", [unit])

        response = self._send_request(xml_data)
        return self._set_confirmed(response.text, group, Drive=DriveState.ON.value)

    def turn_off(self, group: int) -> bool:
        """Turn off HVAC unit"""
        unit = HVACUnit(group=group, drive=DriveState.OFF)
        xml_data = self._create_packet("setRequest", [unit])

        response = self._send_request(xml_data)
        return self._set_confirmed(response.text, group, Drive=DriveState.OFF.value)

    def set_temperature(self, group: int, temperature: int) -> bool:
        """Set target temperature for HVAC unit"""
        if not (16 <= temperature <= 30):  # Reasonable range
            raise ValueError("Temperature must be between 16-30°C")

        unit = HVACUnit(group=group, set_temp=temperature)
        xml_data = self._create_packet("setRequest", [unit])

        response = self._send_request(xml_data)
        return self._set_confirmed(response.text, group, SetTemp=str(temperature))

    def set_fan_speed(self, group: int, fan_speed: FanSpeed) -> bool:
        """Set fan speed for HVAC unit"""
        unit = HVACUnit(group=group, fan_speed=fan_speed)
        xml_data = self._create_packet("setRequest", [unit])

        response = self._send_request(xml_data)
        return self._set_confirmed(response.text, group, FanSpeed=fan_speed.value)

    def set_air_direction(self, group: int, direction: AirDirection) -> bool:
        """Set the outlet vane direction for a supporting group."""
        unit = HVACUnit(group=group, air_direction=direction)
        response = self._send_request(self._create_packet("setRequest", [unit]))
        return self._set_confirmed(
            response.text, group, AirDirection=direction.value
        )

    def set_mode(self, group: int, mode: Mode) -> bool:
        """Set operating mode for HVAC unit"""
        if mode in (Mode.AUTOCOOL, Mode.AUTOHEAT):
            raise ValueError(f"{mode.value} is a read-only status, not a settable mode")
        unit = HVACUnit(group=group, mode=mode)
        xml_data = self._create_packet("setRequest", [unit])

        response = self._send_request(xml_data)
        return self._set_confirmed(response.text, group, Mode=mode.value)

    # === QUERY METHODS ===

    def get_status(self, groups: Union[int, List[int]]) -> List[HVACUnit]:
        """Get status of HVAC units"""
        if isinstance(groups, int):
            groups = [groups]
        if not groups:
            return []

        # Create special query packet
        packet = ET.Element("Packet")
        cmd_elem = ET.SubElement(packet, "Command")
        cmd_elem.text = "getRequest"
        db_manager = ET.SubElement(packet, "DatabaseManager")

        for group in groups:
            self._validate_group(group)
            mnet = ET.SubElement(db_manager, "Mnet")
            mnet.set("Group", str(group))
            mnet.set("Drive", "*")
            mnet.set("Mode", "*")
            mnet.set("SetTemp", "*")
            mnet.set("FanSpeed", "*")
            mnet.set("AirDirection", "*")
            mnet.set("InletTemp", "*")
            mnet.set("FilterSign", "*")
            mnet.set("ErrorSign", "*")

        xml_data = ET.tostring(packet, encoding='unicode', xml_declaration=True)
        response = self._send_request(xml_data)

        return self._parse_response(response.text)

    def get_unit_status(self, group: int) -> HVACUnit:
        """Get status of a single HVAC unit"""
        units = self.get_status([group])
        if not units:
            raise Exception(f"No response for group {group}")
        return units[0]
