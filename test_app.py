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
from app import ArduinoWorker, App


class FakePort:
    def __init__(self, *args, **kwargs):
        self.answer = b""
        self.closed = False
        self.led = False

    def reset_input_buffer(self):
        self.answer = b""

    def write(self, command):
        replies = {b"CASA_HELLO_V1\n": b"CASA_UNO_LED_V1\n"}
        if command in (b"LED 1\n", b"LED 0\n"):
            self.led = command == b"LED 1\n"
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
        self.assertIn(("connected", ("COM4", False)), events)
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
            app.events.put(("connected", ("COM4", False)))
            app.process_events()
            self.assertNotIn("disabled", app.button.state())
            app.toggle()
            self.assertTrue(app.commands.get_nowait())
            self.assertIn("disabled", app.button.state())
            self.assertEqual(app.led_label.cget("text"), "LED: APAGADO")
            app.events.put(("done", True))
            app.process_events()
            self.assertEqual(app.led_label.cget("text"), "LED: ENCENDIDO")
            app.events.put(("disconnected", None))
            app.process_events()
            self.assertIn("disabled", app.button.state())
            self.assertEqual(app.led_label.cget("text"), "LED: estado desconocido")
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
