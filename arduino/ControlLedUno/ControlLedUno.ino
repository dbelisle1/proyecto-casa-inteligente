// Placa objetivo: Arduino UNO (ATmega328P). Protocolo ASCII a 9600 baudios.
// DS3231 por I2C: SDA -> A4, SCL -> A5, además de VCC y GND.
#include <Arduino.h>
#include <RTClib.h>
#include <Wire.h>
#include <stdio.h>
#include <string.h>

namespace BuiltInLed {
bool isOn = false;

void begin() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);
}

void set(bool turnOn) {
  isOn = turnOn;
  digitalWrite(LED_BUILTIN, isOn ? HIGH : LOW);
}

void printState() {
  Serial.println(isOn ? F("LED 1") : F("LED 0"));
}
}  // namespace BuiltInLed

namespace ClockModule {
constexpr byte kDs3231Address = 0x68;
RTC_DS3231 rtc;
bool initialized = false;

bool respondsOnI2c() {
  Wire.beginTransmission(kDs3231Address);
  return Wire.endTransmission() == 0;
}

bool ensureAvailable() {
  if (!respondsOnI2c()) {
    initialized = false;
    return false;
  }
  if (!initialized) initialized = rtc.begin();
  return initialized;
}

void begin() {
  Wire.begin();
  initialized = rtc.begin();
}

void printOffline() {
  Serial.println(F("RTC OFFLINE"));
}

void printUnset() {
  Serial.println(F("RTC UNSET"));
}

void printDateTime(const DateTime &value) {
  char output[20];
  snprintf(output, sizeof(output), "%04u-%02u-%02u %02u:%02u:%02u",
           value.year(), value.month(), value.day(), value.hour(),
           value.minute(), value.second());
  Serial.print(F("RTC "));
  Serial.println(output);
}

void printCurrent() {
  if (!ensureAvailable()) {
    printOffline();
    return;
  }
  if (rtc.lostPower()) {
    printUnset();
    return;
  }

  const DateTime now = rtc.now();
  if (!now.isValid()) {
    printUnset();
    return;
  }
  printDateTime(now);
}

bool parseDateTime(const char *text, DateTime &result) {
  unsigned int year, month, day, hour, minute, second;
  char trailing;
  const int fields = sscanf(text, "%u-%u-%u %u:%u:%u%c", &year, &month,
                            &day, &hour, &minute, &second, &trailing);
  if (fields != 6 || year < 2000 || year > 2099 || month < 1 ||
      month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 ||
      second > 59) {
    return false;
  }

  const DateTime candidate(year, month, day, hour, minute, second);
  if (!candidate.isValid()) return false;
  result = candidate;
  return true;
}

void setFromText(const char *text) {
  if (!ensureAvailable()) {
    printOffline();
    return;
  }

  DateTime requested;
  if (!parseDateTime(text, requested)) {
    Serial.println(F("ERR RTC_BAD_DATETIME"));
    return;
  }

  rtc.adjust(requested);
  printCurrent();
}
}  // namespace ClockModule

namespace SerialProtocol {
constexpr byte kCommandCapacity = 48;
char command[kCommandCapacity];
byte used = 0;
bool overflow = false;

void execute(const char *input) {
  if (strcmp(input, "CASA_HELLO_V1") == 0) {
    Serial.println(F("CASA_UNO_LED_V1"));
  } else if (strcmp(input, "STATUS") == 0) {
    BuiltInLed::printState();
  } else if (strcmp(input, "LED 1") == 0 || strcmp(input, "LED 0") == 0) {
    BuiltInLed::set(input[4] == '1');
    BuiltInLed::printState();
  } else if (strcmp(input, "RTC GET") == 0) {
    ClockModule::printCurrent();
  } else if (strncmp(input, "RTC SET ", 8) == 0) {
    ClockModule::setFromText(input + 8);
  } else {
    Serial.println(F("ERR UNKNOWN_COMMAND"));
  }
}

void poll() {
  while (Serial.available() > 0) {
    const char received = Serial.read();
    if (received == '\n') {
      command[used] = '\0';
      if (overflow) Serial.println(F("ERR TOO_LONG"));
      else execute(command);
      used = 0;
      overflow = false;
    } else if (received != '\r') {
      if (used < sizeof(command) - 1) command[used++] = received;
      else overflow = true;
    }
  }
}
}  // namespace SerialProtocol

void setup() {
  Serial.begin(9600);
  BuiltInLed::begin();
  ClockModule::begin();
}

void loop() {
  SerialProtocol::poll();
}
