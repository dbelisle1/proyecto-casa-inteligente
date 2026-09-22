# Casa inteligente con Arduino UNO

Interfaz de escritorio Python (Tkinter) para el proyecto de casa inteligente.
La etapa actual controla el LED integrado, una ventana con servomotor y una
puerta con motor DC mediante L298N. También administra la conexión USB serial y
permite consultar o ajustar un reloj RTC DS3231. La comunicación utiliza
pySerial a 9600 baudios y cada acción manual se conserva en `reporte.txt`.

El PDF `proyecto2.pdf` también describe etapas posteriores: puerta, ventana,
DHT11, alarma y bomba de agua. El registro ya está preparado para incorporar
los eventos automáticos de esas funciones.

## Preparar la placa

1. Conecte el Arduino UNO con un cable USB de datos.
2. Abra `arduino/ControlLedUno/ControlLedUno.ino` en Arduino IDE.
3. Seleccione **Arduino AVR Boards > Arduino Uno** y el puerto de su placa.
4. En el gestor de bibliotecas instale **RTClib by Adafruit**.
5. Suba el sketch. Esto reemplaza el programa anterior de la placa.
6. Cierre el Monitor Serial y el Serial Plotter para liberar el puerto.

No necesita LED externo: se utiliza `LED_BUILTIN`, el LED L del UNO (pin 13).
El LED comienza apagado. Abrir el puerto puede reiniciar el UNO; la aplicación
espera dos segundos antes de identificarlo.

Para el reloj DS3231 conecte `SDA` a `A4`, `SCL` a `A5`, además de `VCC` y
`GND`. Instale la biblioteca **RTClib de Adafruit** y sus dependencias desde el
gestor de bibliotecas de Arduino IDE antes de compilar el sketch. La ausencia
del RTC no detiene el firmware: las funciones restantes continúan operativas.

Para el servomotor conecte la señal al pin digital `D2`. Al arrancar se ordena
la posición cerrada de 5°; la posición abierta es 89°. Se recomienda alimentar
el servo con una fuente regulada de 5 V adecuada para su consumo y unir el GND
de esa fuente con el GND del Arduino. Evite alimentar servos de alto consumo
directamente desde el pin de 5 V del UNO.

Para el L298N conecte `IN1` a `D3`, `IN2` a `D4`, el motor a `OUT1/OUT2` y
mantenga habilitado `ENA` mediante el jumper del módulo. Alimente el motor con
una fuente externa adecuada y conecte su GND con el GND del Arduino; no alimente
el motor desde un pin del UNO. El movimiento dura 1000 ms por sentido, un cuarto
de los 4000 ms del ejemplo. Sin encoder ni finales de carrera esta posición es
una estimación temporal, por lo que `kQuarterTravelMs` debe calibrarse con la
mecánica real. Al reiniciar, el programa supone que la puerta está cerrada.

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
- Después de identificar y consultar correctamente la placa, guarda el puerto en
  `variables.json`. En ejecuciones posteriores prueba primero ese puerto; si ya
  no está disponible, continúa automáticamente con la búsqueda completa.
- Cada candidato recibe `CASA_HELLO_V1`; solo acepta `CASA_UNO_LED_V1`.
  Esta identificación reconoce nuestro firmware, no certifica el modelo físico.
  El firmware debe estar cargado previamente.
- El botón se habilita tras confirmar la conexión y el estado. Envía `LED 1`
  o `LED 0`; la etiqueta cambia cuando la placa confirma el comando.
- El botón **Buscar Arduino de nuevo** permite cerrar la conexión actual y
  reiniciar inmediatamente el recorrido de puertos. También adelanta el intento
  si la aplicación estaba esperando el siguiente ciclo automático.
- **Asignar hora al RTC** envía al DS3231 la fecha y hora local de la computadora.
  **Mostrar datos del RTC** imprime el valor en la terminal y en el log visible.
  Un módulo ausente se muestra y registra como `RTC OFFLINE`; uno que perdió la
  hora por falta de energía se muestra como `RTC SIN CONFIGURAR`.
- Cada acción manual se agrega a `reporte.txt` con la hora obtenida del DS3231.
  El mismo evento `report` queda disponible para los eventos automáticos que se
  incorporen en las siguientes etapas.
- La sección **Ventana · Servomotor** ofrece controles independientes para abrir
  a 89° y cerrar a 5°. El Arduino responde con la posición ordenada y la acción
  queda registrada con la hora del RTC. El movimiento se realiza gradualmente,
  un grado cada 20 ms (aproximadamente 1.7 s entre ambos extremos), sin bloquear
  la comunicación serial.
- La sección **Puerta · Motor DC L298N** controla apertura y cierre invirtiendo
  `IN1/IN2`. El motor se detiene automáticamente después de 1000 ms, sin usar
  `delay()`, y la aplicación espera la confirmación final antes de habilitar otra
  acción. Ambas órdenes se guardan en `reporte.txt`.
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
5. Pulse **Mostrar datos del RTC**: debe aparecer una fecha válida o `RTC
   OFFLINE` si el módulo no está conectado.
6. Pulse **Asignar hora al RTC** y vuelva a consultar: la hora debe coincidir
   aproximadamente con la computadora y la acción debe aparecer en `reporte.txt`.
7. Pulse **Abrir ventana (89°)** y **Cerrar ventana (5°)**, comprobando ambas
   posiciones y sus entradas correspondientes en `reporte.txt`.
8. Con la puerta inicialmente cerrada, pulse **Abrir puerta** y compruebe que el
   motor gira durante aproximadamente un segundo y se detiene. Pulse **Cerrar
   puerta** y confirme el giro inverso durante el mismo tiempo.
9. Desconecte USB: debe mostrar error y estado desconocido. Reconecte y espere
   la detección automática (cada candidato puede requerir unos cuatro segundos).
10. Cierre y reabra: el puerto debe quedar disponible.

Referencias: [UNO Rev3](https://store.arduino.cc/products/arduino-uno-rev3),
[enumeración de puertos](https://pyserial.readthedocs.io/en/stable/tools.html),
[API serial](https://pyserial.readthedocs.io/en/stable/pyserial_api.html).

## Validación realizada

- Firmware compilado para `arduino:avr:uno` con RTClib 2.1.4, Adafruit BusIO
  1.17.4 y Servo 1.3.0: 9104 bytes de programa y 524 bytes de RAM.
- El firmware evita enlazar `scanf`, `printf` y `snprintf`: RTClib construye y
  valida la fecha ISO, mientras que la salida usa impresiones numéricas directas.
  El búfer serial se redujo de 48 a 32 bytes.
- Diecisiete pruebas aprobadas, incluidas lectura y ajuste del RTC, `RTC OFFLINE`,
  servo, control no bloqueante de la puerta, botones y reporte persistente.
- Ejecutar pruebas: `.\.venv\Scripts\python.exe -m unittest -v`.
- No se cargó firmware ni se verificó el LED físico: el UNO todavía no estaba
  conectado durante la preparación.
