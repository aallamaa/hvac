import signal
import sys
import math
import xml.etree.ElementTree as ET
import requests
from dataclasses import dataclass
from typing import Optional, List, Dict, Union
from enum import Enum


class _RequestTimedOut(Exception):
    """Internal signal-alarm sentinel; never escapes this module."""
    pass


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
    # Add more modes as discovered: HEAT, DRY, FAN, AUTO, etc.

class FanSpeed(Enum):
    HIGH = "HIGH"
    LOW = "LOW"
    MID1 = "MID1" 
    MID2 = "MID2"
    AUTO = "AUTO"
    # Likely also: LOW, AUTO

@dataclass
class HVACUnit:
    """Represents the state of an HVAC unit"""
    group: int
    drive: Optional[DriveState] = None
    mode: Optional[Mode] = None
    set_temp: Optional[int] = None
    fan_speed: Optional[FanSpeed] = None
    inlet_temp: Optional[float] = None  # Read-only
    
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
        if self.inlet_temp is not None:
            attrs["InletTemp"] = str(self.inlet_temp)
            
        return attrs
    
    @classmethod
    def from_mnet_attrs(cls, attrs: Dict[str, str]) -> 'HVACUnit':
        """Create HVACUnit from M-NET XML attributes"""
        unit = cls(group=int(attrs["Group"]))
        
        if "Drive" in attrs and attrs["Drive"] != "*":
            unit.drive = DriveState(attrs["Drive"])
        if "Mode" in attrs and attrs["Mode"] != "*":
            unit.mode = Mode(attrs["Mode"])
        if "SetTemp" in attrs and attrs["SetTemp"] != "*":
            unit.set_temp = int(attrs["SetTemp"])
        if "FanSpeed" in attrs and attrs["FanSpeed"] != "*":
            unit.fan_speed = FanSpeed(attrs["FanSpeed"])
        if "InletTemp" in attrs and attrs["InletTemp"] != "*":
            inlet_temp = float(attrs["InletTemp"])
            if math.isfinite(inlet_temp):
                unit.inlet_temp = inlet_temp
            
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

    def __init__(self, host: str = "192.168.1.2", port: int = 80):
        self.host = host
        self.port = port
        self.debug = False
        self.url = f"http://{host}:{port}/servlet/MIMEReceiveServlet"
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
            mnet = ET.SubElement(db_manager, "Mnet")
            for key, value in unit.to_mnet_attrs().items():
                mnet.set(key, value)
        
        # Format XML
        ET.indent(packet, space="  ")
        return ET.tostring(packet, encoding='unicode', xml_declaration=True)
    
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
                attrs = mnet.attrib
                unit = HVACUnit.from_mnet_attrs(attrs)
                units.append(unit)
            
            return units
        except ET.ParseError as e:
            raise Exception(f"Failed to parse M-NET response: {e}")
    
    # === CONTROL METHODS ===
    
    def turn_all_on(self) -> Dict[int, bool]:
        """Turn on all HVAC units"""
        results = {}
        for group in [1, 2, 3, 4, 5]:
            try:
                results[group] = self.turn_on(group)
            except Exception as e:
                results[group] = False
        return results

    def turn_all_off(self) -> Dict[int, bool]:
        """Turn off all HVAC units"""
        results = {}
        for group in [1, 2, 3, 4, 5]:
            try:
                results[group] = self.turn_off(group)
            except Exception as e:
                results[group] = False
        return results

    def turn_on(self, group: int) -> bool:
        """Turn on HVAC unit"""
        unit = HVACUnit(group=group, drive=DriveState.ON)
        xml_data = self._create_packet("setRequest", [unit])
        
        response = self._send_request(xml_data)
        # Check if response confirms the setting
        return "setResponse" in response.text and f'Group="{group}"' in response.text
    
    def turn_off(self, group: int) -> bool:
        """Turn off HVAC unit"""
        unit = HVACUnit(group=group, drive=DriveState.OFF)
        xml_data = self._create_packet("setRequest", [unit])
        
        response = self._send_request(xml_data)
        return "setResponse" in response.text and f'Group="{group}"' in response.text
    
    def set_temperature(self, group: int, temperature: int) -> bool:
        """Set target temperature for HVAC unit"""
        if not (16 <= temperature <= 30):  # Reasonable range
            raise ValueError("Temperature must be between 16-30°C")
            
        unit = HVACUnit(group=group, set_temp=temperature)
        xml_data = self._create_packet("setRequest", [unit])
        
        response = self._send_request(xml_data)
        return "setResponse" in response.text and f'SetTemp="{temperature}"' in response.text
    
    def set_fan_speed(self, group: int, fan_speed: FanSpeed) -> bool:
        """Set fan speed for HVAC unit"""
        unit = HVACUnit(group=group, fan_speed=fan_speed)
        xml_data = self._create_packet("setRequest", [unit])
        
        response = self._send_request(xml_data)
        return "setResponse" in response.text and f'FanSpeed="{fan_speed.value}"' in response.text
    
    def set_mode(self, group: int, mode: Mode) -> bool:
        """Set operating mode for HVAC unit"""
        unit = HVACUnit(group=group, mode=mode)
        xml_data = self._create_packet("setRequest", [unit])
        
        response = self._send_request(xml_data)
        return "setResponse" in response.text and f'Mode="{mode.value}"' in response.text
    
    def set_multiple(self, group: int, **kwargs) -> bool:
        """Set multiple parameters at once"""
        unit = HVACUnit(group=group)
        
        if 'drive' in kwargs:
            unit.drive = DriveState(kwargs['drive']) if isinstance(kwargs['drive'], str) else kwargs['drive']
        if 'mode' in kwargs:
            unit.mode = Mode(kwargs['mode']) if isinstance(kwargs['mode'], str) else kwargs['mode']
        if 'set_temp' in kwargs:
            unit.set_temp = kwargs['set_temp']
        if 'fan_speed' in kwargs:
            unit.fan_speed = FanSpeed(kwargs['fan_speed']) if isinstance(kwargs['fan_speed'], str) else kwargs['fan_speed']
        
        xml_data = self._create_packet("setRequest", [unit])
        response = self._send_request(xml_data)
        return "setResponse" in response.text
    
    # === QUERY METHODS ===
    
    def get_status(self, groups: Union[int, List[int]] = None) -> List[HVACUnit]:
        """Get status of HVAC units"""
        if groups is None:
            # Query all groups (1-5 based on observed data)
            groups = [1, 2, 3, 4, 5]
        elif isinstance(groups, int):
            groups = [groups]
        
        # Create query units with wildcards
        query_units = []
        for group in groups:
            unit = HVACUnit(
                group=group,
                drive=None,  # Will be converted to "*" in XML
                mode=None,
                set_temp=None,
                fan_speed=None,
                inlet_temp=None
            )
            # Override to_mnet_attrs for query
            query_units.append(unit)
        
        # Create special query packet
        packet = ET.Element("Packet")
        cmd_elem = ET.SubElement(packet, "Command")
        cmd_elem.text = "getRequest"
        db_manager = ET.SubElement(packet, "DatabaseManager")
        
        for group in groups:
            mnet = ET.SubElement(db_manager, "Mnet")
            mnet.set("Group", str(group))
            mnet.set("Drive", "*")
            mnet.set("Mode", "*") 
            mnet.set("SetTemp", "*")
            mnet.set("FanSpeed", "*")
            mnet.set("InletTemp", "*")
        
        xml_data = ET.tostring(packet, encoding='unicode', xml_declaration=True)
        response = self._send_request(xml_data)
        
        return self._parse_response(response.text)
    
    def get_unit_status(self, group: int) -> HVACUnit:
        """Get status of a single HVAC unit"""
        units = self.get_status([group])
        if not units:
            raise Exception(f"No response for group {group}")
        return units[0]
    
    # === CONVENIENCE METHODS ===
    
    def cool_mode(self, group: int, temperature: int, fan_speed: FanSpeed = FanSpeed.MID2) -> bool:
        """Set unit to cooling mode with specified temperature and fan speed"""
        return self.set_multiple(
            group=group,
            drive=DriveState.ON,
            mode=Mode.COOL,
            set_temp=temperature,
            fan_speed=fan_speed
        )
    
    def emergency_stop_all(self) -> Dict[int, bool]:
        """Turn off all HVAC units"""
        results = {}
        for group in [1, 2, 3, 4, 5]:
            try:
                results[group] = self.turn_off(group)
            except Exception as e:
                results[group] = False
        return results
    
    def status_summary(self) -> Dict[int, Dict]:
        """Get a summary of all unit statuses"""
        units = self.get_status()
        summary = {}
        
        for unit in units:
            summary[unit.group] = {
                'power': unit.drive.value if unit.drive else 'Unknown',
                'mode': unit.mode.value if unit.mode else 'Unknown',
                'set_temp': unit.set_temp if unit.set_temp else 'Unknown',
                'current_temp': unit.inlet_temp if unit.inlet_temp else 'Unknown',
                'fan_speed': unit.fan_speed.value if unit.fan_speed else 'Unknown'
            }
        
        return summary

# === USAGE EXAMPLES ===

def main():
    """Example usage of the M-NET controller"""
    
    # Initialize controller
    controller = MNetController("192.168.1.2")
    
    try:
        print("=== M-NET HVAC Controller Examples ===")
        
        # 1. Get current status of all units
        print("\n1. Getting status of all units:")
        status = controller.status_summary()
        for group, info in status.items():
            print(f"   Group {group}: {info}")
        
        # 2. Turn on unit and set cooling
        print(f"\n2. Setting Group 4 to cooling mode (22°C, MID2 fan):")
        success = controller.cool_mode(group=4, temperature=22, fan_speed=FanSpeed.MID2)
        print(f"   Success: {success}")
        
        # 3. Change individual settings
        print(f"\n3. Changing fan speed to HIGH:")
        success = controller.set_fan_speed(group=4, fan_speed=FanSpeed.HIGH)
        print(f"   Success: {success}")
        
        # 4. Check single unit status
        print(f"\n4. Checking Group 4 status after changes:")
        unit = controller.get_unit_status(group=4)
        print(f"   Power: {unit.drive.value if unit.drive else 'Unknown'}")
        print(f"   Temperature: {unit.set_temp}°C (Current: {unit.inlet_temp}°C)")
        print(f"   Fan: {unit.fan_speed.value if unit.fan_speed else 'Unknown'}")
        
        # 5. Turn off unit
        print(f"\n5. Turning off Group 4:")
        success = controller.turn_off(group=4)
        print(f"   Success: {success}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    pass #main()
