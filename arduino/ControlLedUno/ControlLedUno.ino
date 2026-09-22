// Placa objetivo: Arduino UNO (ATmega328P). Protocolo ASCII a 9600 baudios.
// DS3231 por I2C: SDA -> A4, SCL -> A5, además de VCC y GND.
#include <Arduino.h>
#include <RTClib.h>
#include <Servo.h>
#include <Wire.h>
#include <avr/pgmspace.h>
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

namespace WindowServo {
constexpr byte kSignalPin = 2;
constexpr byte kOpenAngle = 89;
constexpr byte kClosedAngle = 5;
constexpr unsigned long kStepIntervalMs = 20;
Servo motor;
byte currentAngle = kClosedAngle;
byte targetAngle = kClosedAngle;
unsigned long lastStepAt = 0;

void begin() {
  motor.attach(kSignalPin);
  motor.write(kClosedAngle);
  currentAngle = kClosedAngle;
  targetAngle = kClosedAngle;
  lastStepAt = millis();
}

void setOpen(bool openWindow) {
  targetAngle = openWindow ? kOpenAngle : kClosedAngle;
}

void update() {
  if (currentAngle == targetAngle) return;

  const unsigned long now = millis();
  if (now - lastStepAt < kStepIntervalMs) return;
  lastStepAt = now;

  currentAngle += currentAngle < targetAngle ? 1 : -1;
  motor.write(currentAngle);
}

void printState() {
  Serial.println(targetAngle == kOpenAngle ? F("SERVO OPEN 89")
                                           : F("SERVO CLOSED 5"));
}
}  // namespace WindowServo

namespace DoorMotor {
constexpr byte kIn1Pin = 3;
constexpr byte kIn2Pin = 4;
constexpr unsigned long kQuarterTravelMs = 1000;

enum State : byte { CLOSED, OPEN, OPENING, CLOSING };
State state = CLOSED;
unsigned long movementStartedAt = 0;

void stop() {
  digitalWrite(kIn1Pin, LOW);
  digitalWrite(kIn2Pin, LOW);
}

void begin() {
  pinMode(kIn1Pin, OUTPUT);
  pinMode(kIn2Pin, OUTPUT);
  stop();
  state = CLOSED;
}

void printState() {
  switch (state) {
    case OPEN:
      Serial.println(F("DOOR OPEN"));
      break;
    case OPENING:
      Serial.println(F("DOOR MOVING OPEN"));
      break;
    case CLOSING:
      Serial.println(F("DOOR MOVING CLOSED"));
      break;
    default:
      Serial.println(F("DOOR CLOSED"));
  }
}

void open() {
  if (state == OPEN || state == OPENING) {
    printState();
    return;
  }
  if (state == CLOSING) {
    Serial.println(F("ERR DOOR_BUSY"));
    return;
  }

  digitalWrite(kIn1Pin, LOW);
  digitalWrite(kIn2Pin, HIGH);
  movementStartedAt = millis();
  state = OPENING;
  printState();
}

void close() {
  if (state == CLOSED || state == CLOSING) {
    printState();
    return;
  }
  if (state == OPENING) {
    Serial.println(F("ERR DOOR_BUSY"));
    return;
  }

  digitalWrite(kIn1Pin, HIGH);
  digitalWrite(kIn2Pin, LOW);
  movementStartedAt = millis();
  state = CLOSING;
  printState();
}

void update() {
  if (state != OPENING && state != CLOSING) return;
  if (millis() - movementStartedAt < kQuarterTravelMs) return;

  stop();
  state = state == OPENING ? OPEN : CLOSED;
}
}  // namespace DoorMotor

namespace ClockModule {
constexpr byte kDs3231Address = 0x68;
RTC_DS3231 rtc;
bool initialized = false;

bool respondsOnI2c() {
  Wire.beginTransmission(kDs3231Address);
  return Wire.endTransmission() == 0;
}

bool ensureAvailable() {
  if (!initialized) {
    initialized = rtc.begin();
    return initialized;
  }
  if (!respondsOnI2c()) initialized = false;
  return initialized;
}

void begin() {
  initialized = rtc.begin();
}

void printOffline() {
  Serial.println(F("RTC OFFLINE"));
}

void printUnset() {
  Serial.println(F("RTC UNSET"));
}

void printTwoDigits(byte value) {
  if (value < 10) Serial.write('0');
  Serial.print(value);
}

void printDateTime(const DateTime &value) {
  Serial.print(F("RTC "));
  Serial.print(value.year());
  Serial.write('-');
  printTwoDigits(value.month());
  Serial.write('-');
  printTwoDigits(value.day());
  Serial.write(' ');
  printTwoDigits(value.hour());
  Serial.write(':');
  printTwoDigits(value.minute());
  Serial.write(':');
  printTwoDigits(value.second());
  Serial.println();
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
  printDateTime(now);
}

void setFromText(const char *text) {
  if (!ensureAvailable()) {
    printOffline();
    return;
  }

  const DateTime requested(text);
  if (!requested.isValid()) {
    Serial.println(F("ERR RTC_BAD_DATETIME"));
    return;
  }

  rtc.adjust(requested);
  printDateTime(requested);
}
}  // namespace ClockModule

namespace SerialProtocol {
constexpr byte kCommandCapacity = 32;
char command[kCommandCapacity];
byte used = 0;
bool overflow = false;

void execute(const char *input) {
  if (strcmp_P(input, PSTR("CASA_HELLO_V1")) == 0) {
    Serial.println(F("CASA_UNO_LED_V1"));
  } else if (strcmp_P(input, PSTR("STATUS")) == 0) {
    BuiltInLed::printState();
  } else if (strcmp_P(input, PSTR("LED 1")) == 0 ||
             strcmp_P(input, PSTR("LED 0")) == 0) {
    BuiltInLed::set(input[4] == '1');
    BuiltInLed::printState();
  } else if (strcmp_P(input, PSTR("RTC GET")) == 0) {
    ClockModule::printCurrent();
  } else if (strncmp_P(input, PSTR("RTC SET "), 8) == 0) {
    ClockModule::setFromText(input + 8);
  } else if (strcmp_P(input, PSTR("SERVO OPEN")) == 0) {
    WindowServo::setOpen(true);
    WindowServo::printState();
  } else if (strcmp_P(input, PSTR("SERVO CLOSE")) == 0) {
    WindowServo::setOpen(false);
    WindowServo::printState();
  } else if (strcmp_P(input, PSTR("SERVO STATUS")) == 0) {
    WindowServo::printState();
  } else if (strcmp_P(input, PSTR("DOOR OPEN")) == 0) {
    DoorMotor::open();
  } else if (strcmp_P(input, PSTR("DOOR CLOSE")) == 0) {
    DoorMotor::close();
  } else if (strcmp_P(input, PSTR("DOOR STATUS")) == 0) {
    DoorMotor::printState();
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
  WindowServo::begin();
  DoorMotor::begin();
  ClockModule::begin();
}

void loop() {
  SerialProtocol::poll();
  WindowServo::update();
  DoorMotor::update();
}
