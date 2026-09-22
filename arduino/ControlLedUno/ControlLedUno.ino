// Placa objetivo: Arduino UNO (ATmega328P). Protocolo ASCII a 9600 baudios.
#include <Arduino.h>
#include <string.h>

char command[40];
byte used = 0;
bool overflow = false;
bool ledOn = false;

void reportState() {
  Serial.println(ledOn ? F("LED 1") : F("LED 0"));
}

void executeCommand() {
  if (strcmp(command, "CASA_HELLO_V1") == 0) {
    Serial.println(F("CASA_UNO_LED_V1"));
  } else if (strcmp(command, "STATUS") == 0) {
    reportState();
  } else if (strcmp(command, "LED 1") == 0 || strcmp(command, "LED 0") == 0) {
    ledOn = command[4] == '1';
    digitalWrite(LED_BUILTIN, ledOn ? HIGH : LOW);
    reportState();
  } else {
    Serial.println(F("ERR UNKNOWN_COMMAND"));
  }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);
  Serial.begin(9600);
}

void loop() {
  while (Serial.available() > 0) {
    const char ch = Serial.read();
    if (ch == '\n') {
      command[used] = '\0';
      if (overflow) Serial.println(F("ERR TOO_LONG"));
      else executeCommand();
      used = 0;
      overflow = false;
    } else if (ch != '\r') {
      if (used < sizeof(command) - 1) command[used++] = ch;
      else overflow = true;
    }
  }
}
