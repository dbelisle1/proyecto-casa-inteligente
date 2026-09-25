// Placa objetivo: Arduino UNO (ATmega328P). Protocolo ASCII a 9600 baudios.
// DS3231 por I2C: SDA -> A4, SCL -> A5, además de VCC y GND.
// DHT11: DATA -> D5, además de VCC y GND.
// Buzzer: señal -> D6. PIR: OUT -> D7, además de VCC y GND.
// Fotoresistencia: divisor de voltaje -> A0. LED exterior: ánodo -> D9.
// Relé de bomba: IN -> D11. Sensor de depósito lleno/vacío: OUT -> D12.
#include <Arduino.h>
#include <DHT.h>
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
constexpr byte kOpenAngle = 90;
constexpr byte kClosedAngle = 0;
Servo motor;
byte currentAngle = kClosedAngle;

void begin() {
  motor.attach(kSignalPin);
  motor.write(kClosedAngle);
  currentAngle = kClosedAngle;
}

void setOpen(bool openWindow) {
  currentAngle = openWindow ? kOpenAngle : kClosedAngle;
  motor.write(currentAngle);
}

void printState() {
  Serial.println(currentAngle == kOpenAngle ? F("SERVO OPEN 90")
                                           : F("SERVO CLOSED 0"));
}

void pausePulses() {
  motor.detach();
}

void resumePulses() {
  motor.attach(kSignalPin);
  motor.write(currentAngle);
}
}  // namespace WindowServo

namespace DoorMotor {
constexpr byte kIn1Pin = 3;
constexpr byte kIn2Pin = 4;
constexpr unsigned long kQuarterTravelMs = 50;
constexpr unsigned int kPwmPeriodUs = 2000;

constexpr unsigned int kPoweredTimeUs = 300;

enum State : byte { CLOSED, OPEN, OPENING, CLOSING };
State state = CLOSED;
unsigned long movementStartedAt = 0;
unsigned long pwmCycleStartedAt = 0;

void stop() {
  digitalWrite(kIn1Pin, LOW);
  digitalWrite(kIn2Pin, LOW);
}

void drive(bool powered) {
  if (!powered) {
    stop();
  } else if (state == OPENING) {
    digitalWrite(kIn1Pin, LOW);
    digitalWrite(kIn2Pin, HIGH);
  } else {
    digitalWrite(kIn1Pin, HIGH);
    digitalWrite(kIn2Pin, LOW);
  }
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

  state = OPENING;
  movementStartedAt = millis();
  pwmCycleStartedAt = micros();
  drive(true);
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

  state = CLOSING;
  movementStartedAt = millis();
  pwmCycleStartedAt = micros();
  drive(true);
  printState();
}

void update() {
  if (state != OPENING && state != CLOSING) return;
  if (millis() - movementStartedAt >= kQuarterTravelMs) {
    stop();
    state = state == OPENING ? OPEN : CLOSED;
    return;
  }

  const unsigned long now = micros();
  unsigned long elapsed = now - pwmCycleStartedAt;
  if (elapsed >= kPwmPeriodUs) {
    pwmCycleStartedAt = now;
    elapsed = 0;
  }
  drive(elapsed < kPoweredTimeUs);
}

void pausePower() {
  if (state == OPENING || state == CLOSING) stop();
}

void resumePower() {
  if ((state == OPENING || state == CLOSING) &&
      millis() - movementStartedAt < kQuarterTravelMs) {
    pwmCycleStartedAt = micros();
    drive(true);
  }
}
}  // namespace DoorMotor

namespace ClimateSensor {
constexpr byte kDataPin = 5;
DHT sensor(kDataPin, DHT11);

void begin() {
  sensor.begin();
}

void printReading() {
  // La librería DHT desactiva interrupciones durante su lectura. Pausar el
  // servo y el motor evita pulsos anormalmente largos en ambos actuadores.
  WindowServo::pausePulses();
  DoorMotor::pausePower();
  const float humidity = sensor.readHumidity();
  const float temperature = sensor.readTemperature();
  DoorMotor::resumePower();
  WindowServo::resumePulses();
  if (isnan(humidity) || isnan(temperature)) {
    Serial.println(F("DHT OFFLINE"));
    return;
  }

  // El DHT11 entrega resolución entera. Evitar imprimir float ahorra memoria
  // de programa en el ATmega328P y simplifica el protocolo serial.
  Serial.print(F("DHT "));
  Serial.print(static_cast<int>(temperature));
  Serial.write(' ');
  Serial.println(static_cast<int>(humidity));
}
}  // namespace ClimateSensor

namespace SecurityAlarm {
constexpr byte kBuzzerPin = 6;
constexpr byte kPirPin = 7;
constexpr unsigned int kLowToneHz = 440;
constexpr unsigned int kHighToneHz = 523;
constexpr unsigned long kToneStepMs = 300;
constexpr unsigned long kAlarmDurationMs = 5000;

bool armed = false;
bool motion = false;
bool eventPending = false;
bool sounding = false;
bool highTone = false;
unsigned long lastToneAt = 0;
unsigned long alarmStartedAt = 0;

void silence() {
  if (sounding) noTone(kBuzzerPin);
  sounding = false;
  digitalWrite(kBuzzerPin, LOW);
}

void startSound() {
  const unsigned long now = millis();
  highTone = false;
  tone(kBuzzerPin, kLowToneHz);
  sounding = true;
  alarmStartedAt = now;
  lastToneAt = now;
}

void begin() {
  pinMode(kBuzzerPin, OUTPUT);
  pinMode(kPirPin, INPUT);
  silence();
  motion = digitalRead(kPirPin) == HIGH;
}

void setArmed(bool enable) {
  const bool wasArmed = armed;
  motion = digitalRead(kPirPin) == HIGH;
  armed = enable;
  if (armed && !wasArmed && motion) {
    eventPending = true;
    startSound();
  }
  if (!armed) {
    eventPending = false;
    silence();
  }
}

void update() {
  const bool currentMotion = digitalRead(kPirPin) == HIGH;
  if (armed && currentMotion && !motion) {
    eventPending = true;
    startSound();
  }
  motion = currentMotion;

  if (!armed) {
    silence();
    return;
  }
  if (!sounding) return;

  const unsigned long now = millis();
  if (now - alarmStartedAt >= kAlarmDurationMs) {
    silence();
  } else if (now - lastToneAt >= kToneStepMs) {
    highTone = !highTone;
    tone(kBuzzerPin, highTone ? kHighToneHz : kLowToneHz);
    lastToneAt = now;
  }
}

void printState() {
  Serial.print(armed ? F("ALARM ARMED MOTION ")
                     : F("ALARM DISARMED MOTION "));
  Serial.print(motion ? '1' : '0');
  Serial.print(F(" EVENT "));
  Serial.println(eventPending ? '1' : '0');
  eventPending = false;
}

bool isArmed() {
  return armed;
}
}  // namespace SecurityAlarm

namespace AutomaticLight {
constexpr byte kLedPin = 9;
constexpr byte kLdrPin = A0;
constexpr int kDarkThreshold = 500;
constexpr unsigned long kSampleIntervalMs = 100;

int ldrValue = 0;
bool ledOn = false;
bool eventPending = false;
unsigned long lastSampleAt = 0;

void setLed(bool turnOn) {
  if (ledOn == turnOn) return;
  ledOn = turnOn;
  digitalWrite(kLedPin, ledOn ? HIGH : LOW);
  eventPending = true;
}

void begin() {
  pinMode(kLedPin, OUTPUT);
  digitalWrite(kLedPin, LOW);
  ldrValue = analogRead(kLdrPin);
  lastSampleAt = millis();
}

void update() {
  if (!SecurityAlarm::isArmed()) setLed(false);

  const unsigned long now = millis();
  if (now - lastSampleAt < kSampleIntervalMs) return;
  lastSampleAt = now;
  ldrValue = analogRead(kLdrPin);
  setLed(SecurityAlarm::isArmed() && ldrValue < kDarkThreshold);
}

void printState() {
  Serial.print(ledOn ? F("LIGHT ON VALUE ") : F("LIGHT OFF VALUE "));
  Serial.print(ldrValue);
  Serial.print(F(" EVENT "));
  Serial.println(eventPending ? '1' : '0');
  eventPending = false;
}
}  // namespace AutomaticLight

namespace WaterTank {
constexpr byte kRelayPin = 11;
constexpr byte kFullSensorPin = 12;
constexpr bool kRelayActiveLow = false;
constexpr bool kFullSensorActiveLow = true;

bool tankFull = false;
bool relayOn = false;
bool eventPending = false;

bool isTankFull() {
  const bool sensorHigh = digitalRead(kFullSensorPin) == HIGH;
  return kFullSensorActiveLow ? !sensorHigh : sensorHigh;
}

void writeRelayPin(bool turnOn) {
  const byte activeLevel = kRelayActiveLow ? LOW : HIGH;
  digitalWrite(kRelayPin, turnOn ? activeLevel : !activeLevel);
}

void setRelay(bool turnOn) {
  if (relayOn == turnOn) return;
  relayOn = turnOn;
  writeRelayPin(relayOn);
  eventPending = true;
}

void begin() {
  writeRelayPin(false);
  pinMode(kRelayPin, OUTPUT);
  pinMode(kFullSensorPin, INPUT);
  tankFull = isTankFull();
  setRelay(SecurityAlarm::isArmed() && !tankFull);
}

void update() {
  tankFull = isTankFull();
  setRelay(SecurityAlarm::isArmed() && !tankFull);
}

void printState() {
  Serial.print(F("WATER FULL "));
  Serial.print(tankFull ? '1' : '0');
  Serial.print(relayOn ? F(" RELAY ON EVENT ") : F(" RELAY OFF EVENT "));
  Serial.println(eventPending ? '1' : '0');
  eventPending = false;
}
}  // namespace WaterTank

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
  } else if (strcmp_P(input, PSTR("DHT GET")) == 0) {
    ClimateSensor::printReading();
  } else if (strcmp_P(input, PSTR("ALARM ON")) == 0) {
    SecurityAlarm::setArmed(true);
    SecurityAlarm::printState();
  } else if (strcmp_P(input, PSTR("ALARM OFF")) == 0) {
    SecurityAlarm::setArmed(false);
    SecurityAlarm::printState();
  } else if (strcmp_P(input, PSTR("ALARM STATUS")) == 0) {
    SecurityAlarm::printState();
  } else if (strcmp_P(input, PSTR("LIGHT STATUS")) == 0) {
    AutomaticLight::printState();
  } else if (strcmp_P(input, PSTR("WATER STATUS")) == 0) {
    WaterTank::printState();
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
  ClimateSensor::begin();
  SecurityAlarm::begin();
  AutomaticLight::begin();
  WaterTank::begin();
  ClockModule::begin();
}

void loop() {
  SerialProtocol::poll();
  DoorMotor::update();
  SecurityAlarm::update();
  AutomaticLight::update();
  WaterTank::update();
}
