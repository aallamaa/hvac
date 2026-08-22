import http.server
import math
import signal
import socketserver
import threading
import unittest
from unittest import mock

import requests

from mit import (
    AirDirection,
    MNetController,
    HVACUnit,
    DriveState,
    Mode,
    FanSpeed,
    _RequestTimedOut,
)


class DummyStreamResponse:
    """Mock requests.Response for deterministic stream testing."""

    def __init__(self, chunks, status_code=200, headers=None, close_side_effect=None):
        self.chunks = chunks
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/xml"}
        self.encoding = "utf-8"
        self._content = None
        self._content_consumed = False
        self.close_called = False
        self.close_call_count = 0
        self.close_side_effect = close_side_effect

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            http_error = requests.exceptions.HTTPError(f"{self.status_code} Error")
            http_error.response = self
            raise http_error

    def iter_content(self, chunk_size=8192):
        for c in self.chunks:
            if isinstance(c, BaseException):
                raise c
            yield c

    def close(self):
        self.close_called = True
        self.close_call_count += 1
        if self.close_side_effect is not None:
            if isinstance(self.close_side_effect, BaseException):
                raise self.close_side_effect
            elif callable(self.close_side_effect):
                self.close_side_effect()
            else:
                raise Exception(str(self.close_side_effect))

    @property
    def text(self):
        if self._content is None:
            return ""
        return self._content.decode(self.encoding or "utf-8")

    @property
    def content(self):
        return self._content


class DummyDirectSession:
    """Keep existing post mocks while exposing Session lifecycle behavior."""
    instances = []

    def __init__(self):
        self.trust_env = True
        self.close_called = False
        self.instances.append(self)

    def post(self, *args, **kwargs):
        return requests.post(*args, **kwargs)

    def close(self):
        self.close_called = True


class TestMNetControllerSendRequest(unittest.TestCase):
    def setUp(self):
        self.ctrl = MNetController("127.0.0.1", 80)
        DummyDirectSession.instances = []
        self.session_patch = mock.patch("requests.Session", DummyDirectSession)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        # Ensure clean signal state before each test
        signal.setitimer(signal.ITIMER_REAL, 0, 0)

    def tearDown(self):
        signal.setitimer(signal.ITIMER_REAL, 0, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)

    def test_send_request_success_closes_response_and_returns_buffered_content(self):
        sample_xml = b"<Packet><Command>setRequest</Command><DatabaseManager><Mnet Group=\"1\" Drive=\"ON\"/></DatabaseManager></Packet>"
        mock_resp = DummyStreamResponse([sample_xml[:30], sample_xml[30:]], status_code=200)

        with mock.patch("requests.post", return_value=mock_resp) as mock_post:
            resp = self.ctrl._send_request("<dummy/>")

            self.assertTrue(mock_resp.close_called, "Response must be closed on success path")
            self.assertEqual(mock_resp.close_call_count, 1)
            self.assertEqual(resp.content, sample_xml)
            self.assertEqual(resp.text, sample_xml.decode("utf-8"))
            self.assertFalse(DummyDirectSession.instances[0].trust_env)
            self.assertTrue(DummyDirectSession.instances[0].close_called)

            mock_post.assert_called_once_with(
                self.ctrl.url,
                headers=self.ctrl.headers,
                data="<dummy/>",
                timeout=self.ctrl.REQUEST_TIMEOUT,
                stream=True,
                allow_redirects=False,
            )

    def test_send_request_rejects_and_closes_redirects(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                mock_resp = DummyStreamResponse(
                    [b"redirecting"],
                    status_code=status,
                    headers={"Location": "http://malicious.origin/redirect"},
                )
                with mock.patch("requests.post", return_value=mock_resp):
                    with self.assertRaises(ValueError) as ctx:
                        self.ctrl._send_request("<dummy/>")

                    self.assertIn("unexpected redirect", str(ctx.exception))
                    self.assertIn("http://malicious.origin/redirect", str(ctx.exception))
                    self.assertTrue(mock_resp.close_called, f"Response must be closed on redirect {status}")

    def test_send_request_closes_response_on_http_error(self):
        mock_resp = DummyStreamResponse([b"Server error"], status_code=500)
        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(Exception) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertIn("M-NET communication failed", str(ctx.exception))
            self.assertTrue(mock_resp.close_called, "Response must be closed on HTTP error")

    def test_send_request_closes_response_on_iter_content_error(self):
        mock_resp = DummyStreamResponse(
            [b"part1", requests.exceptions.ChunkedEncodingError("Connection dropped mid-stream")],
            status_code=200,
        )
        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(Exception) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertIn("M-NET communication failed", str(ctx.exception))
            self.assertTrue(mock_resp.close_called, "Response must be closed on body iteration error")

    def test_send_request_closes_response_on_size_limit_exceeded(self):
        self.ctrl.MAX_RESPONSE_BYTES = 100
        mock_resp = DummyStreamResponse([b"x" * 60, b"x" * 60], status_code=200)
        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertIn("byte limit", str(ctx.exception))
            self.assertTrue(mock_resp.close_called, "Response must be closed on size cap exceeded")

    def test_send_request_closes_response_on_wall_clock_timeout_during_streaming(self):
        def _timed_out_chunks(*args, **kwargs):
            yield b"chunk1"
            raise _RequestTimedOut()

        mock_resp = DummyStreamResponse([], status_code=200)
        mock_resp.iter_content = mock.Mock(side_effect=_timed_out_chunks)

        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(Exception) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertIn("wall-clock limit", str(ctx.exception))
            self.assertTrue(mock_resp.close_called, "Response must be closed on wall-clock timeout during streaming")

    def test_send_request_wall_clock_timeout_during_connect(self):
        with mock.patch("requests.post", side_effect=_RequestTimedOut()):
            with self.assertRaises(Exception) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertIn("wall-clock limit", str(ctx.exception))

    def test_send_request_close_only_failure_raises_communication_error(self):
        mock_resp = DummyStreamResponse(
            [b"<test/>"],
            status_code=200,
            close_side_effect=IOError("socket close error"),
        )
        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(Exception) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertTrue(mock_resp.close_called)
            self.assertIn("M-NET communication failed while closing response: socket close error", str(ctx.exception))

    def test_send_request_cleanup_remains_inside_wall_clock_deadline(self):
        mock_resp = DummyStreamResponse(
            [b"<test/>"],
            close_side_effect=_RequestTimedOut(),
        )
        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(Exception) as ctx:
                self.ctrl._send_request("<dummy/>")

        self.assertTrue(mock_resp.close_called)
        self.assertIn("wall-clock limit during cleanup", str(ctx.exception))

    def test_send_request_blocks_alarm_while_restoring_signal_state(self):
        mock_resp = DummyStreamResponse([b"<test/>"])
        real_pthread_sigmask = signal.pthread_sigmask

        with mock.patch("requests.post", return_value=mock_resp), \
             mock.patch(
                 "signal.pthread_sigmask", wraps=real_pthread_sigmask
             ) as mock_sigmask:
            self.ctrl._send_request("<dummy/>")

        self.assertEqual(
            mock_sigmask.call_args_list,
            [
                mock.call(signal.SIG_BLOCK, set()),
                mock.call(signal.SIG_BLOCK, {signal.SIGALRM}),
                mock.call(signal.SIG_SETMASK, set()),
            ],
        )

    def test_send_request_close_error_preserves_primary_http_error(self):
        mock_resp = DummyStreamResponse(
            [b"Internal Server Error"],
            status_code=500,
            close_side_effect=RuntimeError("close boom"),
        )
        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(Exception) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertTrue(mock_resp.close_called)
            self.assertIn("M-NET communication failed", str(ctx.exception))
            self.assertIn("500 Error", str(ctx.exception))
            self.assertNotIn("close boom", str(ctx.exception))


    def test_send_request_close_error_preserves_primary_redirect_error(self):
        mock_resp = DummyStreamResponse(
            [b"redirecting"],
            status_code=302,
            headers={"Location": "http://evil.com"},
            close_side_effect=RuntimeError("close boom"),
        )
        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertTrue(mock_resp.close_called)
            self.assertIn("unexpected redirect", str(ctx.exception))
            self.assertNotIn("close boom", str(ctx.exception))

    def test_send_request_close_error_preserves_primary_size_limit_error(self):
        self.ctrl.MAX_RESPONSE_BYTES = 100
        mock_resp = DummyStreamResponse(
            [b"x" * 60, b"x" * 60],
            status_code=200,
            close_side_effect=RuntimeError("close boom"),
        )
        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertTrue(mock_resp.close_called)
            self.assertIn("byte limit", str(ctx.exception))
            self.assertNotIn("close boom", str(ctx.exception))

    def test_send_request_close_error_preserves_primary_stream_error(self):
        mock_resp = DummyStreamResponse(
            [b"part1", requests.exceptions.ChunkedEncodingError("mid-stream failure")],
            status_code=200,
            close_side_effect=RuntimeError("close boom"),
        )
        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(Exception) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertTrue(mock_resp.close_called)
            self.assertIn("M-NET communication failed", str(ctx.exception))
            self.assertIn("mid-stream failure", str(ctx.exception))
            self.assertNotIn("close boom", str(ctx.exception))

    def test_send_request_close_error_preserves_primary_wall_clock_timeout(self):
        def _timed_out_chunks(*args, **kwargs):
            yield b"chunk1"
            raise _RequestTimedOut()

        mock_resp = DummyStreamResponse([], status_code=200, close_side_effect=RuntimeError("close boom"))
        mock_resp.iter_content = mock.Mock(side_effect=_timed_out_chunks)

        with mock.patch("requests.post", return_value=mock_resp):
            with self.assertRaises(Exception) as ctx:
                self.ctrl._send_request("<dummy/>")

            self.assertTrue(mock_resp.close_called)
            self.assertIn("wall-clock limit", str(ctx.exception))
            self.assertNotIn("close boom", str(ctx.exception))


class TestHVACUnitParsing(unittest.TestCase):
    def test_auto_direction_statuses_are_supported(self):
        self.assertEqual(
            HVACUnit.from_mnet_attrs({"Group": "1", "Mode": "AUTOCOOL"}).mode,
            Mode.AUTOCOOL,
        )
        self.assertEqual(
            HVACUnit.from_mnet_attrs({"Group": "1", "Mode": "AUTOHEAT"}).mode,
            Mode.AUTOHEAT,
        )

    def test_unknown_or_malformed_optional_fields_are_ignored(self):
        unit = HVACUnit.from_mnet_attrs({
            "Group": "1", "Drive": "BROKEN", "Mode": "FUTURE_MODE",
            "SetTemp": "--", "FanSpeed": "TURBO", "InletTemp": "--",
        })
        self.assertIsNone(unit.drive)
        self.assertIsNone(unit.mode)
        self.assertIsNone(unit.set_temp)
        self.assertIsNone(unit.fan_speed)
        self.assertIsNone(unit.inlet_temp)

    def test_air_direction_filter_and_fault_status_are_parsed(self):
        unit = HVACUnit.from_mnet_attrs({
            "Group": "6", "AirDirection": "HORIZONTAL",
            "FilterSign": "ON", "ErrorSign": "OFF",
        })
        self.assertEqual(unit.air_direction, AirDirection.HORIZONTAL)
        self.assertEqual(unit.filter_sign, "ON")
        self.assertEqual(unit.error_sign, "OFF")

    def test_one_malformed_group_does_not_discard_valid_group(self):
        xml = """<Packet><Command>getResponse</Command><DatabaseManager>
            <Mnet Group="bad" Drive="ON"/>
            <Mnet Group="2" Drive="OFF" Mode="AUTOHEAT" SetTemp="22.0"/>
        </DatabaseManager></Packet>"""
        units = MNetController()._parse_response(xml)
        self.assertEqual([unit.group for unit in units], [2])
        self.assertEqual(units[0].mode, Mode.AUTOHEAT)
        self.assertEqual(units[0].set_temp, 22)

    def test_all_wildcard_group_is_treated_as_unconfigured(self):
        xml = '<Packet><Command>getResponse</Command><DatabaseManager><Mnet Group="7" Drive="*" Mode="*"/></DatabaseManager></Packet>'
        self.assertEqual(MNetController()._parse_response(xml), [])

    def test_status_parser_rejects_wrong_or_missing_command(self):
        ctrl = MNetController()
        for command in ("", "getRequest", "setResponse"):
            command_xml = "<Command>{}</Command>".format(command) if command else ""
            xml = '<Packet>{}<DatabaseManager><Mnet Group="1" Drive="ON"/></DatabaseManager></Packet>'.format(command_xml)
            with self.subTest(command=command), self.assertRaisesRegex(Exception, "unexpected response command"):
                ctrl._parse_response(xml)

    def test_non_finite_inlet_temperatures_are_treated_as_missing(self):
        for value in ("nan", "inf", "-inf", "NaN", "Infinity"):
            with self.subTest(value=value):
                unit = HVACUnit.from_mnet_attrs({"Group": "1", "InletTemp": value})
                self.assertIsNone(unit.inlet_temp)

    def test_finite_inlet_temperature_is_preserved(self):
        unit = HVACUnit.from_mnet_attrs({"Group": "1", "InletTemp": "24.5"})
        self.assertTrue(math.isfinite(unit.inlet_temp))
        self.assertEqual(unit.inlet_temp, 24.5)

    def test_parse_response_rejects_internal_entity_dtd(self):
        xml = """<!DOCTYPE Packet [
            <!ENTITY a "1234567890">
            <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
        ]><Packet><DatabaseManager><Mnet Group="&b;"/></DatabaseManager></Packet>"""

        with self.assertRaisesRegex(Exception, "forbidden DTD"):
            MNetController()._parse_response(xml)

    def test_parse_response_rejects_external_entity_dtd(self):
        xml = """<!doctype Packet [
            <!ENTITY secret SYSTEM "file:///etc/passwd">
        ]><Packet><DatabaseManager><Mnet Group="1"/></DatabaseManager></Packet>"""

        with self.assertRaisesRegex(Exception, "forbidden DTD"):
            MNetController()._parse_response(xml)


class TestControlConfirmation(unittest.TestCase):
    def setUp(self):
        self.ctrl = MNetController()

    def test_read_only_auto_direction_is_rejected_before_network(self):
        for mode in (Mode.AUTOCOOL, Mode.AUTOHEAT):
            with self.subTest(mode=mode), mock.patch.object(self.ctrl, "_send_request") as send:
                with self.assertRaisesRegex(ValueError, "read-only"):
                    self.ctrl.set_mode(1, mode)
                send.assert_not_called()

    def test_confirmation_requires_same_group_and_expected_value(self):
        valid = """<Packet><Command>setResponse</Command><DatabaseManager>
            <Mnet Group="2" SetTemp="22"/>
        </DatabaseManager></Packet>"""
        wrong_group = valid.replace('Group="2"', 'Group="3"')
        wrong_value = valid.replace('SetTemp="22"', 'SetTemp="23"')
        self.assertTrue(self.ctrl._set_confirmed(valid, 2, SetTemp="22"))
        self.assertFalse(self.ctrl._set_confirmed(wrong_group, 2, SetTemp="22"))
        self.assertFalse(self.ctrl._set_confirmed(wrong_value, 2, SetTemp="22"))

    def test_numeric_temperature_confirmation(self):
        valid_integer = """<Packet><Command>setResponse</Command><DatabaseManager>
            <Mnet Group="2" SetTemp="22"/>
        </DatabaseManager></Packet>"""
        valid_float = """<Packet><Command>setResponse</Command><DatabaseManager>
            <Mnet Group="2" SetTemp="22.0"/>
        </DatabaseManager></Packet>"""
        valid_extra_decimal = """<Packet><Command>setResponse</Command><DatabaseManager>
            <Mnet Group="2" SetTemp="22.00"/>
        </DatabaseManager></Packet>"""

        # Equivalent numeric representations confirm
        self.assertTrue(self.ctrl._set_confirmed(valid_integer, 2, SetTemp="22"))
        self.assertTrue(self.ctrl._set_confirmed(valid_float, 2, SetTemp="22"))
        self.assertTrue(self.ctrl._set_confirmed(valid_integer, 2, SetTemp="22.0"))
        self.assertTrue(self.ctrl._set_confirmed(valid_float, 2, SetTemp="22.0"))
        self.assertTrue(self.ctrl._set_confirmed(valid_extra_decimal, 2, SetTemp="22"))

        # Different values fail
        different = """<Packet><Command>setResponse</Command><DatabaseManager>
            <Mnet Group="2" SetTemp="23"/>
        </DatabaseManager></Packet>"""
        self.assertFalse(self.ctrl._set_confirmed(different, 2, SetTemp="22"))

        # Non-finite and malformed values fail
        for malformed in ("nan", "inf", "-inf", "NaN", "Infinity", "invalid", "", "--"):
            with self.subTest(malformed=malformed):
                xml = f"""<Packet><Command>setResponse</Command><DatabaseManager>
                    <Mnet Group="2" SetTemp="{malformed}"/>
                </DatabaseManager></Packet>"""
                self.assertFalse(self.ctrl._set_confirmed(xml, 2, SetTemp="22"))
                self.assertFalse(self.ctrl._set_confirmed(valid_integer, 2, SetTemp=malformed))

        # Missing attribute fails
        missing_attr = """<Packet><Command>setResponse</Command><DatabaseManager>
            <Mnet Group="2" Drive="ON"/>
        </DatabaseManager></Packet>"""
        self.assertFalse(self.ctrl._set_confirmed(missing_attr, 2, SetTemp="22"))

    def test_non_temperature_attributes_require_exact_match(self):
        xml = """<Packet><Command>setResponse</Command><DatabaseManager>
            <Mnet Group="2" Drive="ON" Mode="COOL" FanSpeed="HIGH" AirDirection="HORIZONTAL"/>
        </DatabaseManager></Packet>"""
        self.assertTrue(self.ctrl._set_confirmed(xml, 2, Drive="ON", Mode="COOL"))
        self.assertFalse(self.ctrl._set_confirmed(xml, 2, Drive="OFF"))
        self.assertFalse(self.ctrl._set_confirmed(xml, 2, Mode="HEAT"))
        self.assertFalse(self.ctrl._set_confirmed(xml, 2, FanSpeed="LOW"))
        self.assertFalse(self.ctrl._set_confirmed(xml, 2, AirDirection="VERTICAL"))

    def test_empty_status_does_not_make_network_request(self):
        with mock.patch.object(self.ctrl, "_send_request") as send:
            self.assertEqual(self.ctrl.get_status([]), [])
        send.assert_not_called()

    def test_get_unit_status_requires_the_requested_group(self):
        wrong = HVACUnit(group=2, drive=DriveState.ON)
        with mock.patch.object(self.ctrl, "get_status", return_value=[wrong]):
            with self.assertRaisesRegex(Exception, "No response for group 1"):
                self.ctrl.get_unit_status(1)

        expected = HVACUnit(group=1, drive=DriveState.OFF)
        with mock.patch.object(self.ctrl, "get_status", return_value=[wrong, expected]):
            self.assertIs(self.ctrl.get_unit_status(1), expected)

    def test_set_air_direction_requires_matching_confirmation(self):
        response = mock.Mock(text="""<Packet><Command>setResponse</Command>
            <DatabaseManager><Mnet Group="6" AirDirection="MID1"/></DatabaseManager>
            </Packet>""")
        with mock.patch.object(self.ctrl, "_send_request", return_value=response):
            self.assertTrue(self.ctrl.set_air_direction(6, AirDirection.MID1))


class TestDiscoveryApis(unittest.TestCase):
    def setUp(self):
        self.ctrl = MNetController()

    def test_group_inventory_combines_registration_and_names(self):
        response = mock.Mock(text="""<Packet><Command>getResponse</Command>
            <DatabaseManager><ControlGroup>
              <MnetGroupList>
                <MnetGroupRecord Group="2" Model="IC" SubModel="" Address="12"/>
                <MnetGroupRecord Group="bad" Model="IC"/>
              </MnetGroupList>
              <MnetList><MnetRecord Group="2" GroupNameWeb="Kitchen"/></MnetList>
            </ControlGroup></DatabaseManager></Packet>""")
        with mock.patch.object(self.ctrl, "_send_request", return_value=response) as send:
            inventory = self.ctrl.get_group_inventory()

        self.assertEqual(inventory, {2: {
            "name": "Kitchen", "model": "IC", "submodel": "", "address": "12"
        }})
        request = send.call_args.args[0]
        self.assertIn("<MnetGroupList/>", request)
        self.assertIn("<MnetList/>", request)

    def test_query_api_rejects_wrong_response_command(self):
        response = mock.Mock(text="""<Packet><Command>setResponse</Command>
            <DatabaseManager><ControlGroup><MnetGroupList>
            <MnetGroupRecord Group="1" Model="IC"/>
            </MnetGroupList></ControlGroup></DatabaseManager></Packet>""")
        with mock.patch.object(self.ctrl, "_send_request", return_value=response):
            with self.assertRaisesRegex(Exception, "unexpected response command"):
                self.ctrl.get_group_inventory()

    def test_get_group_models_queries_and_parses_valid_records(self):
        response = mock.Mock(text="""<Packet><Command>getResponse</Command>
            <DatabaseManager>
              <Mnet Group="1" Model="IC"/>
              <Mnet Group="6" Model="IC"/>
              <Mnet Group="7" Model="*"/>
              <Mnet Group="bad" Model="IC"/>
              <Mnet Group="10" Model="OC"/>
            </DatabaseManager></Packet>""")
        with mock.patch.object(self.ctrl, "_send_request", return_value=response) as send:
            models = self.ctrl.get_group_models([1, 6, 7])

        self.assertEqual(models, {1: "IC", 6: "IC"})
        request = send.call_args.args[0]
        self.assertIn("getRequest", request)
        self.assertNotIn("setRequest", request)
        self.assertIn('Group="1"', request)
        self.assertIn('Group="6"', request)
        self.assertIn('Group="7"', request)
        self.assertIn('Model="*"', request)

    def test_get_group_models_empty_and_validation(self):
        with mock.patch.object(self.ctrl, "_send_request") as send:
            self.assertEqual(self.ctrl.get_group_models([]), {})
        send.assert_not_called()

        for invalid in (0, 51, -1, "1", True, None):
            with self.subTest(invalid=invalid), mock.patch.object(self.ctrl, "_send_request") as send:
                with self.assertRaises(ValueError):
                    self.ctrl.get_group_models([invalid])
                send.assert_not_called()

    def test_get_group_models_rejects_dtd(self):
        response = mock.Mock(text="""<!DOCTYPE Packet [<!ENTITY x "y">]>
            <Packet><DatabaseManager><Mnet Group="1" Model="IC"/></DatabaseManager></Packet>""")
        with mock.patch.object(self.ctrl, "_send_request", return_value=response):
            with self.assertRaisesRegex(Exception, "forbidden DTD"):
                self.ctrl.get_group_models([1])

    def test_capability_query_is_read_only_and_parses_each_group(self):
        response = mock.Mock(text="""<Packet><Command>getResponse</Command>
            <DatabaseManager>
              <Mnet Group="1" Model="IC" IcKind="COOL" FanSpeedSW="3STAGES"/>
              <Mnet Group="6" Model="IC" IcKind="NORMAL" FanSpeedSW="4STAGES"/>
            </DatabaseManager></Packet>""")
        with mock.patch.object(self.ctrl, "_send_request", return_value=response) as send:
            capabilities = self.ctrl.get_capabilities([1, 6])

        self.assertEqual(capabilities[1]["Model"], "IC")
        self.assertEqual(capabilities[1]["IcKind"], "COOL")
        self.assertEqual(capabilities[6]["Model"], "IC")
        self.assertEqual(capabilities[6]["FanSpeedSW"], "4STAGES")
        request = send.call_args.args[0]
        self.assertIn("getRequest", request)
        self.assertNotIn("setRequest", request)
        self.assertIn('Model="*"', request)
        self.assertIn('IcKind="*"', request)

    def test_active_alarm_records_are_parsed(self):
        response = mock.Mock(text="""<Packet><Command>getResponse</Command>
          <DatabaseManager><Mnet><AlarmStatusList>
            <AlarmStatusRecord Address="51" Group="6" Model="OC"
              AlarmCode="1300" Detect="2026-08-22 12:00"/>
          </AlarmStatusList></Mnet></DatabaseManager></Packet>""")
        with mock.patch.object(self.ctrl, "_send_request", return_value=response):
            alarms = self.ctrl.get_alarm_status()
        self.assertEqual(alarms[0]["address"], 51)
        self.assertEqual(alarms[0]["group"], 6)
        self.assertEqual(alarms[0]["alarm_code"], "1300")


class TestSignalAndAlarmPreservation(unittest.TestCase):
    def setUp(self):
        self.ctrl = MNetController("127.0.0.1", 80)
        self.session_patch = mock.patch("requests.Session", DummyDirectSession)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        signal.setitimer(signal.ITIMER_REAL, 0, 0)

    def tearDown(self):
        signal.setitimer(signal.ITIMER_REAL, 0, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)

    def test_signal_restoration_no_prior_alarm(self):
        custom_called = []
        def caller_handler(signum, frame):
            custom_called.append(signum)

        signal.signal(signal.SIGALRM, caller_handler)
        signal.setitimer(signal.ITIMER_REAL, 0, 0)

        mock_resp = DummyStreamResponse([b"<test/>"], status_code=200)
        with mock.patch("requests.post", return_value=mock_resp):
            self.ctrl._send_request("<dummy/>")

        # Caller handler restored
        current_handler = signal.signal(signal.SIGALRM, caller_handler)
        self.assertEqual(current_handler, caller_handler)

        # Alarm disabled
        delay, interval = signal.getitimer(signal.ITIMER_REAL)
        self.assertEqual(delay, 0.0)
        self.assertEqual(interval, 0.0)
        self.assertEqual(custom_called, [])

    def test_inactive_timer_installs_helper_deadline_and_restores_state_on_success(self):
        caller_handler = mock.Mock()
        mock_resp = DummyStreamResponse([b"<Packet><Command>setResponse</Command></Packet>"])

        with mock.patch("signal.getitimer", return_value=(0.0, 0.0)), \
             mock.patch("signal.getsignal", return_value=caller_handler), \
             mock.patch("signal.signal") as mock_signal, \
             mock.patch("signal.setitimer") as mock_setitimer, \
             mock.patch("requests.post", return_value=mock_resp):

            resp = self.ctrl._send_request("<dummy/>")

            self.assertEqual(resp.content, b"<Packet><Command>setResponse</Command></Packet>")
            # Initial setup: signal handler set to helper's _on_alarm, itimer set to 15s
            mock_signal.assert_any_call(signal.SIGALRM, mock.ANY)
            mock_setitimer.assert_any_call(signal.ITIMER_REAL, self.ctrl.MAX_REQUEST_SECONDS, 0.0)

            # Cleanup in finally: itimer reset to 0.0, signal handler restored to caller_handler
            mock_setitimer.assert_called_with(signal.ITIMER_REAL, 0.0, 0.0)
            mock_signal.assert_called_with(signal.SIGALRM, caller_handler)

    def test_inactive_timer_restores_state_on_failure(self):
        caller_handler = mock.Mock()
        failure_scenarios = [
            ("http_error", DummyStreamResponse([b"error 500"], status_code=500), Exception),
            ("redirect_302", DummyStreamResponse([b"redirect"], status_code=302, headers={"Location": "/foo"}), ValueError),
            ("connect_fail", requests.exceptions.ConnectionError("connection refused"), Exception),
            ("byte_limit_exceeded", DummyStreamResponse([b"x" * 60, b"x" * 60]), ValueError),
        ]

        for name, failure, expected_exc in failure_scenarios:
            with self.subTest(scenario=name):
                if name == "byte_limit_exceeded":
                    self.ctrl.MAX_RESPONSE_BYTES = 100
                else:
                    self.ctrl.MAX_RESPONSE_BYTES = 2 * 1024 * 1024

                with mock.patch("signal.getitimer", return_value=(0.0, 0.0)), \
                     mock.patch("signal.getsignal", return_value=caller_handler), \
                     mock.patch("signal.signal") as mock_signal, \
                     mock.patch("signal.setitimer") as mock_setitimer:

                    if isinstance(failure, Exception):
                        with mock.patch("requests.post", side_effect=failure):
                            with self.assertRaises(expected_exc):
                                self.ctrl._send_request("<dummy/>")
                    else:
                        with mock.patch("requests.post", return_value=failure):
                            with self.assertRaises(expected_exc):
                                self.ctrl._send_request("<dummy/>")

                    # Verify restore occurred
                    mock_setitimer.assert_called_with(signal.ITIMER_REAL, 0.0, 0.0)
                    mock_signal.assert_called_with(signal.SIGALRM, caller_handler)

    def test_active_caller_timer_fails_before_network_and_is_not_modified(self):
        caller_handler = mock.Mock()

        with mock.patch("signal.getitimer", return_value=(10.0, 0.0)), \
             mock.patch("signal.signal") as mock_signal, \
             mock.patch("signal.setitimer") as mock_setitimer, \
             mock.patch("requests.post") as mock_post:
            with self.assertRaises(RuntimeError) as ctx:
                self.ctrl._send_request("<dummy/>")

        self.assertIn("ITIMER_REAL is already in use", str(ctx.exception))
        mock_post.assert_not_called()
        mock_signal.assert_not_called()
        mock_setitimer.assert_not_called()

    def test_blocked_sigalrm_fails_before_network(self):
        with mock.patch("signal.getitimer", return_value=(0.0, 0.0)), \
             mock.patch("signal.pthread_sigmask", return_value={signal.SIGALRM}), \
             mock.patch("requests.post") as mock_post:
            with self.assertRaisesRegex(RuntimeError, "SIGALRM is blocked"):
                self.ctrl._send_request("<dummy/>")

        mock_post.assert_not_called()

    def test_pending_sigalrm_fails_before_network_or_handler_change(self):
        with mock.patch("signal.getitimer", return_value=(0.0, 0.0)), \
             mock.patch("signal.pthread_sigmask", return_value=set()), \
             mock.patch("signal.sigpending", return_value={signal.SIGALRM}), \
             mock.patch("signal.signal") as mock_signal, \
             mock.patch("requests.post") as mock_post:
            with self.assertRaisesRegex(RuntimeError, "SIGALRM is pending"):
                self.ctrl._send_request("<dummy/>")

        mock_post.assert_not_called()
        mock_signal.assert_not_called()


class LocalHttpServerIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Silence server logs during tests

            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", 0))
                req_body = self.rfile.read(content_length)

                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/destination")
                    self.end_headers()
                    self.wfile.write(b"Redirecting")
                    return

                if self.path == "/error500":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"Internal Error")
                    return

                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.end_headers()
                resp = (
                    b'<?xml version="1.0" encoding="UTF-8"?>\n'
                    b'<Packet>\n'
                    b'  <Command>getResponse</Command>\n'
                    b'  <DatabaseManager>\n'
                    b'    <Mnet Group="1" Drive="ON" Mode="COOL" SetTemp="22" FanSpeed="HIGH" InletTemp="24.5" />\n'
                    b'  </DatabaseManager>\n'
                    b'</Packet>'
                )
                self.wfile.write(resp)

        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)

    def test_real_server_successful_query_and_parse(self):
        ctrl = MNetController("127.0.0.1", self.port)
        units = ctrl.get_status([1])
        self.assertEqual(len(units), 1)
        u = units[0]
        self.assertEqual(u.group, 1)
        self.assertEqual(u.drive, DriveState.ON)
        self.assertEqual(u.mode, Mode.COOL)
        self.assertEqual(u.set_temp, 22)
        self.assertEqual(u.fan_speed, FanSpeed.HIGH)
        self.assertEqual(u.inlet_temp, 24.5)

    def test_real_server_redirect_rejection(self):
        ctrl = MNetController("127.0.0.1", self.port)
        ctrl.url = f"http://127.0.0.1:{self.port}/redirect"
        with self.assertRaises(ValueError) as ctx:
            ctrl.get_status([1])
        self.assertIn("unexpected redirect", str(ctx.exception))

    def test_real_server_http_error(self):
        ctrl = MNetController("127.0.0.1", self.port)
        ctrl.url = f"http://127.0.0.1:{self.port}/error500"
        with self.assertRaises(Exception) as ctx:
            ctrl.get_status([1])
        self.assertIn("M-NET communication failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
