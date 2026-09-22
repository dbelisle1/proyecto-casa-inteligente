"""Interfaz Tkinter y comunicación serial con el firmware ControlLedUno."""
import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import serial
from serial.tools import list_ports


VARIABLES_FILE = Path(__file__).resolve().with_name("variables.json")
REPORT_FILE = Path(__file__).resolve().with_name("reporte.txt")
RTC_OFFLINE = "RTC OFFLINE"
RTC_UNSET = "RTC SIN CONFIGURAR"


def rtc_timestamp(answer):
    """Convierte una respuesta del protocolo RTC en una marca para el reporte."""
    if answer == "RTC OFFLINE":
        return RTC_OFFLINE
    if answer == "RTC UNSET":
        return RTC_UNSET
    if answer.startswith("RTC "):
        return answer[4:]
    return RTC_OFFLINE


def append_report(path, timestamp, message):
    """Agrega una línea al historial persistente de acciones y eventos."""
    with Path(path).open("a", encoding="utf-8") as report:
        report.write(f"[{timestamp}] {message}\n")


class ArduinoWorker(threading.Thread):
    """Único propietario del puerto; nunca modifica widgets desde este hilo."""

    def __init__(self, events, commands, stop, variables_path=VARIABLES_FILE, rescan=None):
        super().__init__(daemon=True)
        self.events, self.commands, self.stop = events, commands, stop
        self.variables_path = Path(variables_path)
        self.rescan = rescan or threading.Event()
        self.connection = None
        self.remembered_port = self.load_remembered_port()

    def log(self, message):
        self.events.put(("log", message))

    def load_remembered_port(self):
        try:
            variables = json.loads(self.variables_path.read_text(encoding="utf-8"))
            port = variables.get("ultimo_puerto_arduino", "")
            return port.strip() if isinstance(port, str) and port.strip() else None
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError) as error:
            self.log(f"ERROR al leer {self.variables_path.name}: {error}. Se buscarán todos los puertos.")
            return None

    def remember_port(self, port):
        temporary_path = self.variables_path.with_suffix(self.variables_path.suffix + ".tmp")
        try:
            contents = json.dumps({"ultimo_puerto_arduino": port}, ensure_ascii=False, indent=2) + "\n"
            temporary_path.write_text(contents, encoding="utf-8")
            temporary_path.replace(self.variables_path)
            self.remembered_port = port
            self.log(f"Puerto confirmado y guardado en {self.variables_path.name}: {port}")
        except OSError as error:
            self.log(f"ERROR al guardar el puerto en {self.variables_path.name}: {error}")
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def exchange(self, command, accepted=None, accepted_prefixes=(), verbose=True):
        accepted = accepted or set()
        if verbose:
            self.log(f"TX → {command}")
        self.connection.reset_input_buffer()
        self.connection.write((command + "\n").encode("ascii"))
        deadline = time.monotonic() + 2
        while not self.stop.is_set() and time.monotonic() < deadline:
            answer = self.connection.read_until(b"\n", 128).decode("ascii", errors="replace").strip()
            if not answer:
                continue
            if verbose:
                self.log(f"RX ← {answer}")
            if answer in accepted or any(answer.startswith(prefix) for prefix in accepted_prefixes):
                return answer
            if answer.startswith("ERR"):
                raise serial.SerialException(f"Arduino respondió: {answer}")
        raise serial.SerialException(f"Sin confirmación válida para {command!r} (2 s).")

    def query_rtc(self, verbose=True):
        return self.exchange("RTC GET", accepted_prefixes=("RTC ",), verbose=verbose)

    def query_servo(self, verbose=True):
        return self.exchange(
            "SERVO STATUS", {"SERVO OPEN 89", "SERVO CLOSED 5"}, verbose=verbose
        )

    def query_door(self, verbose=True):
        return self.exchange(
            "DOOR STATUS",
            {"DOOR OPEN", "DOOR CLOSED", "DOOR MOVING OPEN", "DOOR MOVING CLOSED"},
            verbose=verbose,
        )

    def servo_for_connection(self):
        try:
            return self.query_servo(verbose=False)
        except (OSError, serial.SerialException) as error:
            self.log(f"No se pudo consultar la posición del servomotor: {error}")
            return None

    def door_for_connection(self):
        try:
            return self.query_door(verbose=False)
        except (OSError, serial.SerialException) as error:
            self.log(f"No se pudo consultar el estado del motor de la puerta: {error}")
            return None

    def wait_for_door(self, expected, moving):
        deadline = time.monotonic() + 3
        while not self.stop.is_set() and time.monotonic() < deadline:
            state = self.query_door(verbose=False)
            if state == expected:
                return
            if state != moving:
                raise serial.SerialException(f"Estado inesperado de la puerta: {state}")
            self.stop.wait(0.1)
        raise serial.SerialException("La puerta no confirmó el fin del movimiento.")

    def rtc_for_report(self):
        if self.connection is None:
            return "RTC OFFLINE"
        try:
            return self.query_rtc(verbose=False)
        except (OSError, serial.SerialException) as error:
            self.log(f"No se pudo obtener la hora para el reporte: {error}")
            return "RTC OFFLINE"

    def report(self, message, rtc_answer=None):
        answer = rtc_answer if rtc_answer is not None else self.rtc_for_report()
        self.events.put(("report", (rtc_timestamp(answer), message)))

    def disconnect(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except (OSError, serial.SerialException) as error:
                self.log(f"ERROR al cerrar el puerto: {error}")
            self.connection = None
        self.events.put(("disconnected", None))

    def handle_rescan_request(self):
        if not self.rescan.is_set():
            return False
        self.rescan.clear()
        self.report("CONTROL MANUAL | Nueva búsqueda del Arduino solicitada")
        self.log("Búsqueda manual: reiniciando la conexión y el recorrido de puertos.")
        self.disconnect()
        return True

    def wait_before_retry(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self.rescan.wait(min(0.1, remaining)):
                self.log("Búsqueda manual: se adelantó el siguiente intento.")
                return

    def discover(self):
        remembered = (self.remembered_port or "").casefold()
        ports = sorted(
            list_ports.comports(),
            key=lambda p: (
                0 if p.device.casefold() == remembered else 1,
                p.vid != 0x2341,
                p.device.casefold(),
            ),
        )
        self.log(f"Búsqueda: {len(ports)} puerto(s) serial(es) disponibles.")
        if not ports:
            self.log("No hay puertos seriales. Compruebe cable USB de datos y controlador.")
        elif remembered and any(port.device.casefold() == remembered for port in ports):
            self.log(f"Se probará primero el puerto recordado: {self.remembered_port}")
        elif remembered:
            self.log(f"El puerto recordado {self.remembered_port} no está disponible; se hará una búsqueda completa.")
        for port in ports:
            if self.stop.is_set():
                return False
            self.log(f"Probando {port.device}: {port.description}")
            try:
                self.connection = serial.Serial(port.device, 9600, timeout=0.2, write_timeout=1)
                self.log("Puerto abierto. Esperando 2 s por el reinicio del UNO...")
                if self.stop.wait(2):
                    self.disconnect()
                    return False
                self.exchange("CASA_HELLO_V1", {"CASA_UNO_LED_V1"})
                state = self.exchange("STATUS", {"LED 0", "LED 1"})
                rtc_state = self.rtc_for_report()
                servo_state = self.servo_for_connection()
                door_state = self.door_for_connection()
                self.log(f"Arduino identificado en {port.device}; estado inicial confirmado.")
                self.remember_port(port.device)
                self.events.put(
                    (
                        "connected",
                        (
                            port.device,
                            state == "LED 1",
                            rtc_state,
                            servo_state,
                            door_state,
                        ),
                    )
                )
                return True
            except (OSError, serial.SerialException) as error:
                self.log(f"ERROR en {port.device}: {error}")
                self.disconnect()
        self.log("No se encontró el firmware del UNO. Reintento en 5 s; revise cable, sketch y Monitor Serial.")
        return False

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    self.handle_rescan_request()
                    if self.connection is None:
                        # Desechar acciones anteriores a una desconexión.
                        while True:
                            try:
                                self.commands.get_nowait()
                            except queue.Empty:
                                break
                        if not self.discover():
                            self.wait_before_retry(5)
                            continue
                    try:
                        action, value = self.commands.get(timeout=1)
                    except queue.Empty:
                        state = self.exchange("STATUS", {"LED 0", "LED 1"}, verbose=False)
                        self.events.put(("state", state == "LED 1"))
                        continue
                    rtc_answer = None
                    report_message = None
                    try:
                        if action == "led":
                            desired = bool(value)
                            report_message = (
                                "CONTROL MANUAL | Botón Encender LED presionado"
                                if desired
                                else "CONTROL MANUAL | Botón Apagar LED presionado"
                            )
                            rtc_answer = self.rtc_for_report()
                            self.log(f"Acción: {'encender' if desired else 'apagar'} el LED integrado.")
                            expected = f"LED {int(desired)}"
                            self.exchange(expected, {expected})
                            self.log("Acción completada: el Arduino confirmó el estado solicitado.")
                            self.events.put(("done", desired))
                        elif action == "rtc_get":
                            report_message = "CONTROL MANUAL | Consulta de fecha y hora del RTC"
                            rtc_answer = self.query_rtc()
                            value_for_console = rtc_timestamp(rtc_answer)
                            self.log(f"Datos actuales del módulo: {value_for_console}")
                            print(f"RTC: {value_for_console}", flush=True)
                            self.events.put(("rtc_done", rtc_answer))
                        elif action == "rtc_set":
                            report_message = f"CONTROL MANUAL | Asignación de hora al RTC: {value}"
                            wire_value = value.replace(" ", "T", 1)
                            rtc_answer = self.exchange(
                                f"RTC SET {wire_value}", accepted_prefixes=("RTC ",)
                            )
                            if rtc_answer == "RTC OFFLINE":
                                self.log("No se pudo asignar la hora: RTC OFFLINE")
                            elif rtc_answer == "RTC UNSET":
                                self.log("El RTC respondió, pero la hora continúa sin configurar.")
                            else:
                                self.log(f"Hora del RTC asignada correctamente: {rtc_timestamp(rtc_answer)}")
                            self.events.put(("rtc_done", rtc_answer))
                        elif action == "servo":
                            open_window = bool(value)
                            report_message = (
                                "CONTROL MANUAL | Botón Abrir ventana presionado (89 grados)"
                                if open_window
                                else "CONTROL MANUAL | Botón Cerrar ventana presionado (5 grados)"
                            )
                            rtc_answer = self.rtc_for_report()
                            command = "SERVO OPEN" if open_window else "SERVO CLOSE"
                            expected = "SERVO OPEN 89" if open_window else "SERVO CLOSED 5"
                            self.log(
                                f"Acción: {'abrir' if open_window else 'cerrar'} la ventana con el servomotor."
                            )
                            self.exchange(command, {expected})
                            self.log(f"Posición confirmada por el Arduino: {expected}")
                            self.events.put(("servo_done", open_window))
                        elif action == "door":
                            open_door = bool(value)
                            report_message = (
                                "CONTROL MANUAL | Botón Abrir puerta presionado"
                                if open_door
                                else "CONTROL MANUAL | Botón Cerrar puerta presionado"
                            )
                            rtc_answer = self.rtc_for_report()
                            command = "DOOR OPEN" if open_door else "DOOR CLOSE"
                            expected = "DOOR OPEN" if open_door else "DOOR CLOSED"
                            moving = "DOOR MOVING OPEN" if open_door else "DOOR MOVING CLOSED"
                            self.log(
                                f"Acción: {'abrir' if open_door else 'cerrar'} la puerta con el motor DC."
                            )
                            response = self.exchange(command, {expected, moving})
                            if response == moving:
                                self.wait_for_door(expected, moving)
                            self.log(f"Movimiento terminado; Arduino confirmó: {expected}")
                            self.events.put(("door_done", open_door))
                        else:
                            self.log(f"ERROR: acción interna desconocida: {action}")
                    finally:
                        if report_message is not None:
                            self.report(report_message, rtc_answer or "RTC OFFLINE")
                except (OSError, serial.SerialException) as error:
                    self.log(f"ERROR de comunicación: {error}. Estado del LED desconocido.")
                    self.report(f"SISTEMA | Error de comunicación: {error}", "RTC OFFLINE")
                    self.disconnect()
                    self.stop.wait(1)
        finally:
            self.disconnect()


class App(tk.Tk):
    def __init__(self, report_path=REPORT_FILE):
        super().__init__()
        self.title("Casa inteligente · Arduino UNO")
        self.geometry("800x800")
        self.minsize(680, 620)
        self.report_path = Path(report_path)
        self.events, self.commands = queue.Queue(), queue.Queue()
        self.stop = threading.Event()
        self.rescan = threading.Event()
        self.connected = False
        self.busy = False
        self.busy_action = None
        self.led_on = False
        self.window_open = None
        self.door_open = None
        self.closing = False
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Control del LED integrado", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        self.connection_label = ttk.Label(frame, text="Buscando Arduino UNO por USB...")
        self.connection_label.pack(anchor="w", pady=(8, 16))
        self.led_label = ttk.Label(frame, text="LED: estado desconocido", font=("Segoe UI", 12))
        self.led_label.pack(anchor="w")
        button_row = ttk.Frame(frame)
        button_row.pack(anchor="w", pady=12)
        self.button = ttk.Button(button_row, text="Esperando Arduino...", command=self.toggle, state="disabled")
        self.button.pack(side="left")
        self.search_button = ttk.Button(button_row, text="Buscar Arduino de nuevo", command=self.rediscover)
        self.search_button.pack(side="left", padx=(8, 0))
        ttk.Separator(frame).pack(fill="x", pady=(4, 12))
        ttk.Label(frame, text="Reloj RTC DS3231", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.rtc_label = ttk.Label(frame, text="RTC: estado desconocido")
        self.rtc_label.pack(anchor="w", pady=(4, 0))
        rtc_button_row = ttk.Frame(frame)
        rtc_button_row.pack(anchor="w", pady=10)
        self.rtc_set_button = ttk.Button(
            rtc_button_row, text="Asignar hora al RTC", command=self.set_rtc_time, state="disabled"
        )
        self.rtc_set_button.pack(side="left")
        self.rtc_read_button = ttk.Button(
            rtc_button_row, text="Mostrar datos del RTC", command=self.read_rtc, state="disabled"
        )
        self.rtc_read_button.pack(side="left", padx=(8, 0))
        ttk.Separator(frame).pack(fill="x", pady=(4, 12))
        ttk.Label(frame, text="Ventana · Servomotor", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.servo_label = ttk.Label(frame, text="Ventana: posición desconocida")
        self.servo_label.pack(anchor="w", pady=(4, 0))
        servo_button_row = ttk.Frame(frame)
        servo_button_row.pack(anchor="w", pady=10)
        self.servo_open_button = ttk.Button(
            servo_button_row, text="Abrir ventana (89°)", command=self.open_window, state="disabled"
        )
        self.servo_open_button.pack(side="left")
        self.servo_close_button = ttk.Button(
            servo_button_row, text="Cerrar ventana (5°)", command=self.close_window, state="disabled"
        )
        self.servo_close_button.pack(side="left", padx=(8, 0))
        ttk.Separator(frame).pack(fill="x", pady=(4, 12))
        ttk.Label(frame, text="Puerta · Motor DC L298N", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.door_label = ttk.Label(frame, text="Puerta: posición desconocida")
        self.door_label.pack(anchor="w", pady=(4, 0))
        door_button_row = ttk.Frame(frame)
        door_button_row.pack(anchor="w", pady=10)
        self.door_open_button = ttk.Button(
            door_button_row, text="Abrir puerta", command=self.open_door, state="disabled"
        )
        self.door_open_button.pack(side="left")
        self.door_close_button = ttk.Button(
            door_button_row, text="Cerrar puerta", command=self.close_door, state="disabled"
        )
        self.door_close_button.pack(side="left", padx=(8, 0))
        ttk.Label(frame, text="Registro de ejecución y errores").pack(anchor="w")
        self.log_box = ScrolledText(frame, height=8, state="disabled", wrap="word", font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True, pady=(6, 0))
        self.worker = ArduinoWorker(self.events, self.commands, self.stop, rescan=self.rescan)
        self.worker.start()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.process_events)

    def append_log(self, text, timestamp=None):
        self.log_box.configure(state="normal")
        visible_timestamp = timestamp or f"{datetime.now():%H:%M:%S}"
        self.log_box.insert("end", f"[{visible_timestamp}] {text}\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 1500:
            self.log_box.delete("1.0", "101.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def render(self):
        self.led_label.configure(text="LED: " + (("ENCENDIDO" if self.led_on else "APAGADO") if self.connected else "estado desconocido"))
        self.button.configure(
            state="normal" if self.connected and not self.busy else "disabled",
            text=("Ejecutando..." if self.busy_action == "led" else ("Apagar LED" if self.led_on else "Encender LED")) if self.connected else "Esperando Arduino...",
        )
        rtc_button_state = "normal" if self.connected and not self.busy else "disabled"
        self.rtc_set_button.configure(state=rtc_button_state)
        self.rtc_read_button.configure(state=rtc_button_state)
        if self.connected and self.window_open is not None:
            servo_text = "Ventana: ABIERTA (89°)" if self.window_open else "Ventana: CERRADA (5°)"
        else:
            servo_text = "Ventana: posición desconocida"
        self.servo_label.configure(text=servo_text)
        servo_button_state = "normal" if self.connected and not self.busy else "disabled"
        self.servo_open_button.configure(state=servo_button_state)
        self.servo_close_button.configure(state=servo_button_state)
        if self.connected and self.door_open is not None:
            door_text = "Puerta: ABIERTA" if self.door_open else "Puerta: CERRADA"
        else:
            door_text = "Puerta: posición desconocida"
        self.door_label.configure(text=door_text)
        door_button_state = "normal" if self.connected and not self.busy else "disabled"
        self.door_open_button.configure(state=door_button_state)
        self.door_close_button.configure(state=door_button_state)

    def toggle(self):
        if self.connected and not self.busy:
            self.busy, self.busy_action = True, "led"
            self.append_log("Paso 1: acción seleccionada; enviando al hilo de comunicación.")
            self.commands.put(("led", not self.led_on))
            self.render()

    def set_rtc_time(self):
        if self.connected and not self.busy:
            value = datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            self.busy, self.busy_action = True, "rtc_set"
            self.append_log(f"Acción: asignar al RTC la hora de esta computadora ({value}).")
            self.commands.put(("rtc_set", value))
            self.render()

    def read_rtc(self):
        if self.connected and not self.busy:
            self.busy, self.busy_action = True, "rtc_get"
            self.append_log("Acción: solicitar los datos actuales del módulo RTC.")
            self.commands.put(("rtc_get", None))
            self.render()

    def set_window(self, open_window):
        if self.connected and not self.busy:
            self.busy, self.busy_action = True, "servo"
            position = 89 if open_window else 5
            action = "abrir" if open_window else "cerrar"
            self.append_log(f"Acción: {action} la ventana; posición solicitada: {position}°.")
            self.commands.put(("servo", open_window))
            self.render()

    def open_window(self):
        self.set_window(True)

    def close_window(self):
        self.set_window(False)

    def set_door(self, open_door):
        if self.connected and not self.busy:
            self.busy, self.busy_action = True, "door"
            action = "abrir" if open_door else "cerrar"
            self.append_log(f"Acción: {action} la puerta durante un cuarto de recorrido.")
            self.commands.put(("door", open_door))
            self.render()

    def open_door(self):
        self.set_door(True)

    def close_door(self):
        self.set_door(False)

    def rediscover(self):
        if self.closing:
            return
        self.connected, self.busy, self.busy_action = False, False, None
        self.connection_label.configure(text="Búsqueda manual solicitada...")
        self.append_log("Acción: volver a buscar el Arduino en los puertos seriales.")
        self.rescan.set()
        self.render()

    def process_events(self):
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(value)
            elif kind == "report":
                timestamp, message = value
                try:
                    append_report(self.report_path, timestamp, message)
                    self.append_log(f"REPORTE | {message}", timestamp)
                except OSError as error:
                    self.append_log(f"ERROR al escribir {self.report_path.name}: {error}")
            elif kind == "connected":
                port, self.led_on, rtc_answer, servo_answer, door_answer = value
                self.connected, self.busy, self.busy_action = True, False, None
                self.window_open = (
                    True
                    if servo_answer == "SERVO OPEN 89"
                    else False if servo_answer == "SERVO CLOSED 5" else None
                )
                self.door_open = (
                    True
                    if door_answer == "DOOR OPEN"
                    else False if door_answer == "DOOR CLOSED" else None
                )
                self.connection_label.configure(text=f"Conectado: Arduino UNO · {port} · 9600 baudios")
                self.rtc_label.configure(text=f"RTC: {rtc_timestamp(rtc_answer)}")
            elif kind == "disconnected":
                self.connected, self.busy, self.busy_action = False, False, None
                self.window_open = None
                self.door_open = None
                self.connection_label.configure(text="Sin conexión · búsqueda automática activa")
                self.rtc_label.configure(text="RTC: estado desconocido")
            elif kind in ("done", "state"):
                self.led_on = value
                if kind == "done":
                    self.busy, self.busy_action = False, None
            elif kind == "rtc_done":
                self.busy, self.busy_action = False, None
                self.rtc_label.configure(text=f"RTC: {rtc_timestamp(value)}")
            elif kind == "servo_done":
                self.busy, self.busy_action = False, None
                self.window_open = value
            elif kind == "door_done":
                self.busy, self.busy_action = False, None
                self.door_open = value
            self.render()
        if not self.closing:
            self.after(100, self.process_events)

    def close(self):
        self.closing = True
        self.button.configure(state="disabled")
        self.search_button.configure(state="disabled")
        self.rtc_set_button.configure(state="disabled")
        self.rtc_read_button.configure(state="disabled")
        self.servo_open_button.configure(state="disabled")
        self.servo_close_button.configure(state="disabled")
        self.door_open_button.configure(state="disabled")
        self.door_close_button.configure(state="disabled")
        self.stop.set()
        self.append_log("Cerrando comunicación serial...")
        self.wait_for_worker()

    def wait_for_worker(self):
        if self.worker.is_alive():
            self.after(100, self.wait_for_worker)
        else:
            self.destroy()


if __name__ == "__main__":
    App().mainloop()
