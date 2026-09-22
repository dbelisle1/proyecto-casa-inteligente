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
from app import (
    ArduinoWorker,
    App,
    DHT_OFFLINE,
    RTC_OFFLINE,
    RTC_UNSET,
    alarm_values,
    dht_values,
    light_values,
    rtc_timestamp,
)


class FakePort:
    def __init__(self, *args, **kwargs):
        self.answer = b""
        self.closed = False
        self.led = False
        self.servo_open = False
        self.door_open = False
        self.rtc_online = True
        self.rtc_value = "2026-09-21 20:30:45"
        self.dht_online = True
        self.temperature = 24
        self.humidity = 50
        self.alarm_armed = False
        self.pir_motion = False
        self.alarm_event = False
        self.ldr_value = 50
        self.external_led_on = False
        self.light_event = False

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
            self.answer = b"SERVO CLOSED 0\n"
        elif command == b"SERVO STATUS\n":
            self.answer = b"SERVO OPEN 89\n" if self.servo_open else b"SERVO CLOSED 0\n"
        elif command == b"DOOR OPEN\n":
            self.door_open = True
            self.answer = b"DOOR OPEN\n"
        elif command == b"DOOR CLOSE\n":
            self.door_open = False
            self.answer = b"DOOR CLOSED\n"
        elif command == b"DOOR STATUS\n":
            self.answer = b"DOOR OPEN\n" if self.door_open else b"DOOR CLOSED\n"
        elif command == b"DHT GET\n":
            self.answer = (
                f"DHT {self.temperature} {self.humidity}\n".encode()
                if self.dht_online
                else b"DHT OFFLINE\n"
            )
        elif command == b"ALARM ON\n":
            if not self.alarm_armed and self.pir_motion:
                self.alarm_event = True
            self.alarm_armed = True
            self.refresh_light()
            self.answer = self.alarm_answer()
        elif command == b"ALARM OFF\n":
            self.alarm_armed = False
            self.alarm_event = False
            self.refresh_light()
            self.answer = self.alarm_answer()
        elif command == b"ALARM STATUS\n":
            self.answer = self.alarm_answer()
        elif command == b"LIGHT STATUS\n":
            self.answer = self.light_answer()
        else:
            self.answer = replies.get(command, b"LED 1\n" if self.led else b"LED 0\n")

    def read_until(self, *args):
        answer, self.answer = self.answer, b""
        return answer

    def close(self):
        self.closed = True

    def set_motion(self, detected):
        if self.alarm_armed and detected and not self.pir_motion:
            self.alarm_event = True
        self.pir_motion = detected

    def alarm_answer(self):
        answer = (
            f"ALARM {'ARMED' if self.alarm_armed else 'DISARMED'} "
            f"MOTION {int(self.pir_motion)} EVENT {int(self.alarm_event)}\n"
        ).encode()
        self.alarm_event = False
        return answer

    def set_ldr(self, value):
        self.ldr_value = value
        self.refresh_light()

    def refresh_light(self):
        desired = self.alarm_armed and self.ldr_value < 500
        if desired != self.external_led_on:
            self.external_led_on = desired
            self.light_event = True

    def light_answer(self):
        answer = (
            f"LIGHT {'ON' if self.external_led_on else 'OFF'} "
            f"VALUE {self.ldr_value} EVENT {int(self.light_event)}\n"
        ).encode()
        self.light_event = False
        return answer


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
                (
                    "COM4",
                    False,
                    "RTC 2026-09-21 20:30:45",
                    "SERVO CLOSED 0",
                    "DOOR CLOSED",
                    "DHT 24 50",
                    "ALARM DISARMED MOTION 0 EVENT 0",
                    "LIGHT OFF VALUE 50 EVENT 0",
                ),
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
        self.assertEqual(self.worker.query_servo(), "SERVO CLOSED 0")
        self.assertEqual(
            self.worker.exchange("SERVO OPEN", {"SERVO OPEN 89"}),
            "SERVO OPEN 89",
        )
        self.assertEqual(self.worker.query_servo(), "SERVO OPEN 89")
        self.assertEqual(
            self.worker.exchange("SERVO CLOSE", {"SERVO CLOSED 0"}),
            "SERVO CLOSED 0",
        )

    def test_door_motor_accepts_open_and_close_commands(self):
        self.worker.connection = FakePort()
        self.assertEqual(self.worker.query_door(), "DOOR CLOSED")
        self.assertEqual(
            self.worker.exchange("DOOR OPEN", {"DOOR OPEN", "DOOR MOVING OPEN"}),
            "DOOR OPEN",
        )
        self.assertEqual(self.worker.query_door(), "DOOR OPEN")
        self.assertEqual(
            self.worker.exchange("DOOR CLOSE", {"DOOR CLOSED", "DOOR MOVING CLOSED"}),
            "DOOR CLOSED",
        )

    def test_dht11_reading_and_offline_state(self):
        port = FakePort()
        self.worker.connection = port
        self.assertEqual(self.worker.query_dht(), "DHT 24 50")
        self.assertEqual(dht_values("DHT 24 50"), (24, 50))
        port.dht_online = False
        self.assertEqual(self.worker.query_dht(), DHT_OFFLINE)
        self.assertIsNone(dht_values(DHT_OFFLINE))

    def test_dht11_automatic_sample_generates_report_with_rtc_time(self):
        self.worker.connection = FakePort()
        self.worker.capture_dht_report()
        events = list(self.worker.events.queue)
        self.assertIn(("dht", "DHT 24 50"), events)
        self.assertIn(
            (
                "report",
                (
                    "2026-09-21 20:30:45",
                    "AUTOMÁTICO | DHT11 | Temperatura: 24 °C | Humedad: 50 %",
                ),
            ),
            events,
        )

    def test_dht11_sample_waits_for_five_second_deadline(self):
        self.worker.next_dht_report_at = 10
        with patch("app.time.monotonic", side_effect=[9.9, 10]), patch.object(
            self.worker, "capture_dht_report"
        ) as capture:
            self.worker.maybe_capture_dht_report()
            capture.assert_not_called()
            self.worker.maybe_capture_dht_report()
            capture.assert_called_once_with()
        self.assertEqual(self.worker.next_dht_report_at, 15)

    def test_alarm_can_be_armed_and_reports_one_event_per_detection(self):
        port = FakePort()
        self.worker.connection = port
        self.assertEqual(
            alarm_values(self.worker.query_alarm()),
            (False, False, False),
        )
        answer = self.worker.exchange("ALARM ON", accepted_prefixes=("ALARM ",))
        self.assertEqual(alarm_values(answer), (True, False, False))
        port.set_motion(True)
        self.assertEqual(alarm_values(self.worker.query_alarm()), (True, True, True))
        self.assertEqual(alarm_values(self.worker.query_alarm()), (True, True, False))
        answer = self.worker.exchange("ALARM OFF", accepted_prefixes=("ALARM ",))
        self.assertEqual(alarm_values(answer), (False, True, False))

    def test_pir_detection_generates_automatic_report(self):
        port = FakePort()
        port.alarm_armed = True
        port.set_motion(True)
        self.worker.connection = port
        self.worker.publish_alarm_state(self.worker.query_alarm(verbose=False))
        events = list(self.worker.events.queue)
        self.assertIn(("alarm", (True, True)), events)
        self.assertIn(
            (
                "report",
                (
                    "2026-09-21 20:30:45",
                    "AUTOMÁTICO | Alarma PIR | Movimiento detectado | Buzzer activado",
                ),
            ),
            events,
        )

    def test_ldr_led_only_operates_while_alarm_is_armed(self):
        port = FakePort()
        self.worker.connection = port
        port.set_ldr(300)
        self.assertEqual(light_values(self.worker.query_light()), (False, 300, False))
        self.worker.exchange("ALARM ON", accepted_prefixes=("ALARM ",))
        self.assertEqual(light_values(self.worker.query_light()), (True, 300, True))
        self.worker.exchange("ALARM OFF", accepted_prefixes=("ALARM ",))
        self.assertEqual(light_values(self.worker.query_light()), (False, 300, True))

    def test_ldr_change_generates_automatic_report(self):
        port = FakePort()
        port.alarm_armed = True
        port.set_ldr(300)
        self.worker.connection = port
        self.worker.publish_light_state(self.worker.query_light(verbose=False))
        events = list(self.worker.events.queue)
        self.assertIn(("light", (True, 300)), events)
        self.assertIn(
            (
                "report",
                (
                    "2026-09-21 20:30:45",
                    "AUTOMÁTICO | LDR | Oscuridad detectada (300) | LED exterior encendido",
                ),
            ),
            events,
        )

    def test_worker_waits_until_door_finishes_moving(self):
        with patch.object(
            self.worker,
            "query_door",
            side_effect=["DOOR MOVING OPEN", "DOOR OPEN"],
        ), patch.object(self.worker.stop, "wait", return_value=False):
            self.worker.wait_for_door("DOOR OPEN", "DOOR MOVING OPEN")

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
                (
                    "connected",
                    (
                        "COM4",
                        False,
                        "RTC OFFLINE",
                        "SERVO CLOSED 0",
                        "DOOR CLOSED",
                        "DHT OFFLINE",
                        "ALARM DISARMED MOTION 0 EVENT 0",
                        "LIGHT OFF VALUE 50 EVENT 0",
                    ),
                )
            )
            app.process_events()
            self.assertNotIn("disabled", app.button.state())
            self.assertNotIn("disabled", app.rtc_set_button.state())
            self.assertNotIn("disabled", app.rtc_read_button.state())
            self.assertNotIn("disabled", app.servo_open_button.state())
            self.assertNotIn("disabled", app.servo_close_button.state())
            self.assertNotIn("disabled", app.door_open_button.state())
            self.assertNotIn("disabled", app.door_close_button.state())
            self.assertNotIn("disabled", app.alarm_on_button.state())
            self.assertIn("disabled", app.alarm_off_button.state())
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
            self.assertEqual(app.door_label.cget("text"), "Puerta: posición desconocida")
            self.assertEqual(app.dht_label.cget("text"), "DHT11: estado desconocido")
            self.assertEqual(app.alarm_label.cget("text"), "Alarma: estado desconocido")
            self.assertEqual(app.ldr_label.cget("text"), "LDR: estado desconocido")
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
                        "SERVO CLOSED 0",
                        "DOOR CLOSED",
                        "DHT 24 50",
                        "ALARM DISARMED MOTION 0 EVENT 0",
                        "LIGHT OFF VALUE 50 EVENT 0",
                    ),
                )
            )
            app.process_events()
            app.read_rtc()
            self.assertEqual(app.commands.get_nowait(), ("rtc_get", None))
            app.events.put(("rtc_done", "RTC 2026-09-21 20:31:00"))
            app.process_events()
            self.assertEqual(app.rtc_label.cget("text"), "RTC: 2026-09-21 20:31:00")
            self.assertEqual(
                app.dht_label.cget("text"),
                "Temperatura: 24 °C · Humedad: 50 %",
            )
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
                        "SERVO CLOSED 0",
                        "DOOR CLOSED",
                        "DHT 24 50",
                        "ALARM DISARMED MOTION 0 EVENT 0",
                        "LIGHT OFF VALUE 50 EVENT 0",
                    ),
                )
            )
            app.process_events()
            self.assertEqual(app.servo_label.cget("text"), "Ventana: CERRADA (0°)")
            app.open_window()
            self.assertEqual(app.commands.get_nowait(), ("servo", True))
            app.events.put(("servo_done", True))
            app.process_events()
            self.assertEqual(app.servo_label.cget("text"), "Ventana: ABIERTA (89°)")
            app.close_window()
            self.assertEqual(app.commands.get_nowait(), ("servo", False))
            app.events.put(("servo_done", False))
            app.process_events()
            self.assertEqual(app.servo_label.cget("text"), "Ventana: CERRADA (0°)")
        finally:
            app.destroy()

    def test_gui_can_open_and_close_door(self):
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
                        "SERVO CLOSED 0",
                        "DOOR CLOSED",
                        "DHT 24 50",
                        "ALARM DISARMED MOTION 0 EVENT 0",
                        "LIGHT OFF VALUE 50 EVENT 0",
                    ),
                )
            )
            app.process_events()
            self.assertEqual(app.door_label.cget("text"), "Puerta: CERRADA")
            app.open_door()
            self.assertEqual(app.commands.get_nowait(), ("door", True))
            app.events.put(("door_done", True))
            app.process_events()
            self.assertEqual(app.door_label.cget("text"), "Puerta: ABIERTA")
            app.close_door()
            self.assertEqual(app.commands.get_nowait(), ("door", False))
            app.events.put(("door_done", False))
            app.process_events()
            self.assertEqual(app.door_label.cget("text"), "Puerta: CERRADA")
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

    def test_gui_can_activate_and_deactivate_alarm(self):
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
                        "SERVO CLOSED 0",
                        "DOOR CLOSED",
                        "DHT 24 50",
                        "ALARM DISARMED MOTION 0 EVENT 0",
                        "LIGHT OFF VALUE 50 EVENT 0",
                    ),
                )
            )
            app.process_events()
            self.assertEqual(app.alarm_label.cget("text"), "Alarma: DESACTIVADA")
            self.assertEqual(app.ldr_label.cget("text"), "LDR: 50 / 1023")
            self.assertEqual(
                app.external_led_label.cget("text"),
                "LED exterior: APAGADO (alarma desactivada)",
            )
            app.activate_alarm()
            self.assertEqual(app.commands.get_nowait(), ("alarm", True))
            app.events.put(("alarm", (True, True)))
            app.events.put(("alarm_done", None))
            app.process_events()
            self.assertEqual(app.alarm_label.cget("text"), "Alarma: ACTIVADA")
            self.assertEqual(app.pir_label.cget("text"), "PIR: MOVIMIENTO DETECTADO")
            app.deactivate_alarm()
            self.assertEqual(app.commands.get_nowait(), ("alarm", False))
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
