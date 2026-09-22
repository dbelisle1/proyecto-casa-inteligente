"""Pruebas de protocolo con puertos simulados; no sustituyen la prueba física."""
import json
import queue
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import serial
from app import ArduinoWorker, App, RTC_OFFLINE, RTC_UNSET, rtc_timestamp


class FakePort:
    def __init__(self, *args, **kwargs):
        self.answer = b""
        self.closed = False
        self.led = False
        self.servo_open = False
        self.rtc_online = True
        self.rtc_value = "2026-09-21 20:30:45"

    def reset_input_buffer(self):
        self.answer = b""

    def write(self, command):
        replies = {b"CASA_HELLO_V1\n": b"CASA_UNO_LED_V1\n"}
        if command in (b"LED 1\n", b"LED 0\n"):
            self.led = command == b"LED 1\n"
        if command == b"RTC GET\n":
            self.answer = f"RTC {self.rtc_value}\n".encode() if self.rtc_online else b"RTC OFFLINE\n"
        elif command.startswith(b"RTC SET "):
            if self.rtc_online:
                self.rtc_value = (
                    command.decode("ascii")
                    .removeprefix("RTC SET ")
                    .strip()
                    .replace("T", " ", 1)
                )
                self.answer = f"RTC {self.rtc_value}\n".encode()
            else:
                self.answer = b"RTC OFFLINE\n"
        elif command == b"SERVO OPEN\n":
            self.servo_open = True
            self.answer = b"SERVO OPEN 89\n"
        elif command == b"SERVO CLOSE\n":
            self.servo_open = False
            self.answer = b"SERVO CLOSED 5\n"
        elif command == b"SERVO STATUS\n":
            self.answer = b"SERVO OPEN 89\n" if self.servo_open else b"SERVO CLOSED 5\n"
        else:
            self.answer = replies.get(command, b"LED 1\n" if self.led else b"LED 0\n")

    def read_until(self, *args):
        answer, self.answer = self.answer, b""
        return answer

    def close(self):
        self.closed = True


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.variables_path = Path(self.temporary_directory.name) / "variables.json"
        self.worker = ArduinoWorker(
            queue.Queue(), queue.Queue(), threading.Event(), self.variables_path
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_occupied_port_is_skipped_and_next_is_identified(self):
        ports = [SimpleNamespace(device=p, vid=None, description="Test") for p in ["COM3", "COM4"]]
        good = FakePort()
        with patch("app.list_ports.comports", return_value=ports), patch("app.serial.Serial", side_effect=[serial.SerialException("ocupado"), good]), patch.object(self.worker.stop, "wait", return_value=False):
            self.assertTrue(self.worker.discover())
        events = list(self.worker.events.queue)
        self.assertIn(
            (
                "connected",
                ("COM4", False, "RTC 2026-09-21 20:30:45", "SERVO CLOSED 5"),
            ),
            events,
        )
        self.assertTrue(any("ocupado" in str(event) for event in events))
        variables = json.loads(self.variables_path.read_text(encoding="utf-8"))
        self.assertEqual(variables["ultimo_puerto_arduino"], "COM4")

    def test_remembered_port_is_tried_first(self):
        self.variables_path.write_text(
            json.dumps({"ultimo_puerto_arduino": "COM4"}), encoding="utf-8"
        )
        worker = ArduinoWorker(
            queue.Queue(), queue.Queue(), threading.Event(), self.variables_path
        )
        ports = [SimpleNamespace(device=p, vid=None, description="Test") for p in ["COM3", "COM4"]]
        with patch("app.list_ports.comports", return_value=ports), patch(
            "app.serial.Serial", return_value=FakePort()
        ) as serial_constructor, patch.object(worker.stop, "wait", return_value=False):
            self.assertTrue(worker.discover())
        self.assertEqual(serial_constructor.call_args.args[0], "COM4")

    def test_led_commands_require_exact_confirmation(self):
        self.worker.connection = FakePort()
        self.assertEqual(self.worker.exchange("LED 1", {"LED 1"}), "LED 1")
        self.assertEqual(self.worker.exchange("LED 0", {"LED 0"}), "LED 0")

    def test_rtc_can_be_read_set_and_reported_offline(self):
        port = FakePort()
        self.worker.connection = port
        self.assertEqual(self.worker.query_rtc(), "RTC 2026-09-21 20:30:45")
        answer = self.worker.exchange(
            "RTC SET 2026-10-01T08:09:10", accepted_prefixes=("RTC ",)
        )
        self.assertEqual(answer, "RTC 2026-10-01 08:09:10")
        port.rtc_online = False
        self.assertEqual(self.worker.query_rtc(), "RTC OFFLINE")
        self.assertEqual(rtc_timestamp("RTC OFFLINE"), RTC_OFFLINE)
        self.assertEqual(rtc_timestamp("RTC UNSET"), RTC_UNSET)

    def test_servo_accepts_only_the_two_configured_positions(self):
        self.worker.connection = FakePort()
        self.assertEqual(self.worker.query_servo(), "SERVO CLOSED 5")
        self.assertEqual(
            self.worker.exchange("SERVO OPEN", {"SERVO OPEN 89"}),
            "SERVO OPEN 89",
        )
        self.assertEqual(self.worker.query_servo(), "SERVO OPEN 89")
        self.assertEqual(
            self.worker.exchange("SERVO CLOSE", {"SERVO CLOSED 5"}),
            "SERVO CLOSED 5",
        )

    def test_wrong_firmware_rejected_and_port_closed(self):
        port = FakePort()
        candidate = SimpleNamespace(device="COM9", vid=None, description="Other")
        with patch("app.list_ports.comports", return_value=[candidate]), patch("app.serial.Serial", return_value=port), patch.object(self.worker.stop, "wait", return_value=False), patch.object(self.worker, "exchange", side_effect=serial.SerialException("Sin identificación")):
            self.assertFalse(self.worker.discover())
        self.assertTrue(port.closed)
        self.assertIsNone(self.worker.connection)

    def test_timeout_does_not_report_success(self):
        self.worker.connection = FakePort()
        with patch.object(self.worker.connection, "read_until", return_value=b"OTHER\n"), patch("app.time.monotonic", side_effect=[0, 0, 3]):
            with self.assertRaises(serial.SerialException):
                self.worker.exchange("LED 1", {"LED 1"})

    def test_no_ports(self):
        with patch("app.list_ports.comports", return_value=[]):
            self.assertFalse(self.worker.discover())

    def test_manual_rescan_closes_current_connection(self):
        port = FakePort()
        self.worker.connection = port
        self.worker.rescan.set()
        self.assertTrue(self.worker.handle_rescan_request())
        self.assertTrue(port.closed)
        self.assertIsNone(self.worker.connection)

    def test_gui_only_enables_control_after_connection(self):
        with patch.object(ArduinoWorker, "start"):
            app = App()
        try:
            app.withdraw()
            self.assertIn("disabled", app.button.state())
            app.events.put(
                ("connected", ("COM4", False, "RTC OFFLINE", "SERVO CLOSED 5"))
            )
            app.process_events()
            self.assertNotIn("disabled", app.button.state())
            self.assertNotIn("disabled", app.rtc_set_button.state())
            self.assertNotIn("disabled", app.rtc_read_button.state())
            self.assertNotIn("disabled", app.servo_open_button.state())
            self.assertNotIn("disabled", app.servo_close_button.state())
            app.toggle()
            self.assertEqual(app.commands.get_nowait(), ("led", True))
            self.assertIn("disabled", app.button.state())
            self.assertEqual(app.led_label.cget("text"), "LED: APAGADO")
            app.events.put(("done", True))
            app.process_events()
            self.assertEqual(app.led_label.cget("text"), "LED: ENCENDIDO")
            app.events.put(("disconnected", None))
            app.process_events()
            self.assertIn("disabled", app.button.state())
            self.assertEqual(app.led_label.cget("text"), "LED: estado desconocido")
            self.assertEqual(app.servo_label.cget("text"), "Ventana: posición desconocida")
        finally:
            app.destroy()

    def test_gui_can_request_rtc_actions(self):
        with patch.object(ArduinoWorker, "start"):
            app = App()
        try:
            app.withdraw()
            app.events.put(
                (
                    "connected",
                    (
                        "COM4",
                        False,
                        "RTC 2026-09-21 20:30:45",
                        "SERVO CLOSED 5",
                    ),
                )
            )
            app.process_events()
            app.read_rtc()
            self.assertEqual(app.commands.get_nowait(), ("rtc_get", None))
            app.events.put(("rtc_done", "RTC 2026-09-21 20:31:00"))
            app.process_events()
            self.assertEqual(app.rtc_label.cget("text"), "RTC: 2026-09-21 20:31:00")
            app.set_rtc_time()
            action, value = app.commands.get_nowait()
            self.assertEqual(action, "rtc_set")
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        finally:
            app.destroy()

    def test_gui_can_open_and_close_window(self):
        with patch.object(ArduinoWorker, "start"):
            app = App()
        try:
            app.withdraw()
            app.events.put(
                (
                    "connected",
                    (
                        "COM4",
                        False,
                        "RTC 2026-09-21 20:30:45",
                        "SERVO CLOSED 5",
                    ),
                )
            )
            app.process_events()
            self.assertEqual(app.servo_label.cget("text"), "Ventana: CERRADA (5°)")
            app.open_window()
            self.assertEqual(app.commands.get_nowait(), ("servo", True))
            app.events.put(("servo_done", True))
            app.process_events()
            self.assertEqual(app.servo_label.cget("text"), "Ventana: ABIERTA (89°)")
            app.close_window()
            self.assertEqual(app.commands.get_nowait(), ("servo", False))
            app.events.put(("servo_done", False))
            app.process_events()
            self.assertEqual(app.servo_label.cget("text"), "Ventana: CERRADA (5°)")
        finally:
            app.destroy()

    def test_report_event_is_written_with_rtc_timestamp(self):
        report_path = Path(self.temporary_directory.name) / "reporte.txt"
        with patch.object(ArduinoWorker, "start"):
            app = App(report_path=report_path)
        try:
            app.withdraw()
            app.events.put(("report", ("RTC OFFLINE", "CONTROL MANUAL | Prueba")))
            app.process_events()
            self.assertEqual(
                report_path.read_text(encoding="utf-8"),
                "[RTC OFFLINE] CONTROL MANUAL | Prueba\n",
            )
        finally:
            app.destroy()

    def test_gui_can_request_a_new_search(self):
        with patch.object(ArduinoWorker, "start"):
            app = App()
        try:
            app.withdraw()
            app.connected = True
            app.rediscover()
            self.assertTrue(app.rescan.is_set())
            self.assertFalse(app.connected)
            self.assertEqual(app.connection_label.cget("text"), "Búsqueda manual solicitada...")
            self.assertIn("disabled", app.button.state())
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
