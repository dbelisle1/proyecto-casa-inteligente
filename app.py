"""Interfaz Tkinter y comunicación serial con el firmware ControlLedUno."""
import queue
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import serial
from serial.tools import list_ports


class ArduinoWorker(threading.Thread):
    """Único propietario del puerto; nunca modifica widgets desde este hilo."""

    def __init__(self, events, commands, stop):
        super().__init__(daemon=True)
        self.events, self.commands, self.stop = events, commands, stop
        self.connection = None

    def log(self, message):
        self.events.put(("log", message))

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

    def discover(self):
        ports = sorted(list_ports.comports(), key=lambda p: (p.vid != 0x2341, p.device))
        self.log(f"Búsqueda: {len(ports)} puerto(s) serial(es) disponibles.")
        if not ports:
            self.log("No hay puertos seriales. Compruebe cable USB de datos y controlador.")
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
                    if self.connection is None:
                        # Desechar acciones anteriores a una desconexión.
                        while True:
                            try:
                                self.commands.get_nowait()
                            except queue.Empty:
                                break
                        if not self.discover():
                            self.stop.wait(5)
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
        self.button = ttk.Button(frame, text="Esperando Arduino...", command=self.toggle, state="disabled")
        self.button.pack(anchor="w", pady=12)
        ttk.Label(frame, text="Registro de ejecución y errores").pack(anchor="w")
        self.log_box = ScrolledText(frame, height=14, state="disabled", wrap="word", font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True, pady=(6, 0))
        self.worker = ArduinoWorker(self.events, self.commands, self.stop)
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
