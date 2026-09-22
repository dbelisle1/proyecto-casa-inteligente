# Control del LED integrado de Arduino UNO

Primera etapa del proyecto de casa inteligente: interfaz de escritorio Python
(Tkinter), con un único botón para encender/apagar, etiqueta de estado y log.
La comunicación USB serial utiliza pySerial a 9600 baudios. El firmware de la
placa se escribe en C++ de Arduino; la interfaz y la comunicación en la PC son Python.

El PDF `proyecto2.pdf` describe etapas posteriores (puerta, ventana, DHT11,
alarma, bomba, RTC DS3231 y eventos en TXT). Esta primera entrega implementa
únicamente el control del LED solicitado. El log actual aparece en pantalla;
todavía no implementa el registro de eventos del proyecto completo.

## Preparar la placa

1. Conecte el Arduino UNO con un cable USB de datos.
2. Abra `arduino/ControlLedUno/ControlLedUno.ino` en Arduino IDE.
3. Seleccione **Arduino AVR Boards > Arduino Uno** y el puerto de su placa.
4. Suba el sketch. Esto reemplaza el programa anterior de la placa.
5. Cierre el Monitor Serial y el Serial Plotter para liberar el puerto.

No necesita LED externo: se utiliza `LED_BUILTIN`, el LED L del UNO (pin 13).
El LED comienza apagado. Abrir el puerto puede reiniciar el UNO; la aplicación
espera dos segundos antes de identificarlo.

## Ejecutar

Necesita Python 3.10 o superior con Tcl/Tk y pip. Desde esta carpeta:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

También puede ejecutar `powershell -ExecutionPolicy Bypass -File .\iniciar.ps1`.
El script utiliza `.tools/python/python.exe` si está disponible, o el comando
`python` del sistema; crea el entorno local e instala pySerial.

## Funcionamiento

- Busca automáticamente al arrancar y tras desconexiones. Recorre todos los
  puertos seriales que Windows expone, incluidos adaptadores USB de clones,
  priorizando el identificador USB de Arduino. No prueba dispositivos USB sin
  interfaz serial, como ratones o memorias.
- Cada candidato recibe `CASA_HELLO_V1`; solo acepta `CASA_UNO_LED_V1`.
  Esta identificación reconoce nuestro firmware, no certifica el modelo físico.
  El firmware debe estar cargado previamente.
- El botón se habilita tras confirmar la conexión y el estado. Envía `LED 1`
  o `LED 0`; la etiqueta cambia cuando la placa confirma el comando.
- Consulta `STATUS` aproximadamente cada segundo para detectar desconexiones
  y cambios de estado. Estas consultas periódicas no saturan el log.
- Registra puertos probados, esperas, comandos, respuestas, errores y resultados.
  Conserva aproximadamente las últimas 1500 líneas en pantalla.
- Si falla la comunicación, muestra estado desconocido, deshabilita el botón
  y vuelve a buscar. Si no encuentra la placa, reintenta tras cinco segundos.
- Al cerrar libera el puerto; no envía una orden de apagado. La placa conserva
  su estado hasta otro comando, reinicio o pérdida de alimentación.

Un puerto ocupado, cable solo de carga, controlador ausente o firmware distinto
impide conectar. Cierre otros programas que utilicen el puerto. La búsqueda abre
los puertos seriales y envía una consulta, por lo que puede reiniciar otras placas
conectadas; desconecte equipos seriales ajenos durante las pruebas.

## Probar con hardware

1. Sin Arduino: el log debe mostrar los candidatos o ausencia de puertos;
   el botón permanece deshabilitado y la ventana responde.
2. Con el sketch cargado: debe identificar el puerto y mostrar LED apagado.
3. Pulse Encender LED: compruebe LED L, respuesta `LED 1` y etiqueta ENCENDIDO.
4. Pulse Apagar LED: compruebe LED L, respuesta `LED 0` y etiqueta APAGADO.
5. Desconecte USB: debe mostrar error y estado desconocido. Reconecte y espere
   la detección automática (cada candidato puede requerir unos cuatro segundos).
6. Cierre y reabra: el puerto debe quedar disponible.

Referencias: [UNO Rev3](https://store.arduino.cc/products/arduino-uno-rev3),
[enumeración de puertos](https://pyserial.readthedocs.io/en/stable/tools.html),
[API serial](https://pyserial.readthedocs.io/en/stable/pyserial_api.html).

## Validación realizada

- Firmware compilado para `arduino:avr:uno`: 2148 bytes de programa y 263 de RAM.
- Seis pruebas aprobadas: puerto ocupado, identificación rechazada, comandos
  de LED, timeout, ausencia de puertos y estados de la interfaz Tkinter.
- Ejecutar pruebas: `.\.venv\Scripts\python.exe -m unittest -v`.
- No se cargó firmware ni se verificó el LED físico: el UNO todavía no estaba
  conectado durante la preparación.
