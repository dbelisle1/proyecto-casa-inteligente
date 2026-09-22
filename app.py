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

    def exchange(self, command, accepted, verbose=True):
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
            if answer in accepted:
                return answer
            if answer.startswith("ERR"):
                raise serial.SerialException(f"Arduino respondió: {answer}")
        raise serial.SerialException(f"Sin confirmación válida para {command!r} (2 s).")

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
                self.rescan.clear()
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
                self.log(f"Arduino identificado en {port.device}; estado inicial confirmado.")
                self.remember_port(port.device)
                self.events.put(("connected", (port.device, state == "LED 1")))
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
                        desired = self.commands.get(timeout=1)
                    except queue.Empty:
                        state = self.exchange("STATUS", {"LED 0", "LED 1"}, verbose=False)
                        self.events.put(("state", state == "LED 1"))
                        continue
                    self.log(f"Acción: {'encender' if desired else 'apagar'} el LED integrado.")
                    expected = f"LED {int(desired)}"
                    self.exchange(expected, {expected})
                    self.log("Acción completada: el Arduino confirmó el estado solicitado.")
                    self.events.put(("done", desired))
                except (OSError, serial.SerialException) as error:
                    self.log(f"ERROR de comunicación: {error}. Estado del LED desconocido.")
                    self.disconnect()
                    self.stop.wait(1)
        finally:
            self.disconnect()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Casa inteligente · Arduino UNO")
        self.geometry("760x510")
        self.minsize(580, 380)
        self.events, self.commands = queue.Queue(), queue.Queue()
        self.stop = threading.Event()
        self.rescan = threading.Event()
        self.connected = False
        self.busy = False
        self.led_on = False
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
        ttk.Label(frame, text="Registro de ejecución y errores").pack(anchor="w")
        self.log_box = ScrolledText(frame, height=14, state="disabled", wrap="word", font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True, pady=(6, 0))
        self.worker = ArduinoWorker(self.events, self.commands, self.stop, rescan=self.rescan)
        self.worker.start()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.process_events)

    def append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{datetime.now():%H:%M:%S}] {text}\n")
        if int(self.log_box.index("end-1c").split(".")[0]) > 1500:
            self.log_box.delete("1.0", "101.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def render(self):
        self.led_label.configure(text="LED: " + (("ENCENDIDO" if self.led_on else "APAGADO") if self.connected else "estado desconocido"))
        self.button.configure(
            state="normal" if self.connected and not self.busy else "disabled",
            text=("Ejecutando..." if self.busy else ("Apagar LED" if self.led_on else "Encender LED")) if self.connected else "Esperando Arduino...",
        )

    def toggle(self):
        if self.connected and not self.busy:
            self.busy = True
            self.append_log("Paso 1: acción seleccionada; enviando al hilo de comunicación.")
            self.commands.put(not self.led_on)
            self.render()

    def rediscover(self):
        if self.closing:
            return
        self.connected, self.busy = False, False
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
            elif kind == "connected":
                port, self.led_on = value
                self.connected, self.busy = True, False
                self.connection_label.configure(text=f"Conectado: Arduino UNO · {port} · 9600 baudios")
            elif kind == "disconnected":
                self.connected, self.busy = False, False
                self.connection_label.configure(text="Sin conexión · búsqueda automática activa")
            elif kind in ("done", "state"):
                self.led_on = value
                if kind == "done":
                    self.busy = False
            self.render()
        if not self.closing:
            self.after(100, self.process_events)

    def close(self):
        self.closing = True
        self.button.configure(state="disabled")
        self.search_button.configure(state="disabled")
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
