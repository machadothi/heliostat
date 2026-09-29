"""
scservo.py -- MicroPython driver for Feetech / Waveshare serial bus servos.

Covers both families on one shared UART bus:

  STSBus  -> ST3020, ST3215, SM/STS series.  Little-endian words, 0..4095 over 360 deg.
  SCSBus  -> SC09 (SCS0009), SCS series.     BIG-endian words,    0..1023 over 300 deg.

The two families share the same packet format and almost the same register map,
but they disagree on byte order -- that is the single most common reason a
"working" ST3020 example does nothing on an SC09.

Packet format (Feetech, a close relative of Dynamixel 1.0):

    0xFF 0xFF  ID  LENGTH  INSTRUCTION  [ADDR  PARAMS...]  CHECKSUM
    LENGTH   = len(PARAMS) + 3     (or 2 for a bare PING)
    CHECKSUM = ~(ID + LENGTH + INSTRUCTION + ADDR + sum(PARAMS)) & 0xFF

Status packet back from the servo:

    0xFF 0xFF  ID  LENGTH  ERROR  [PARAMS...]  CHECKSUM
"""

from machine import UART
import time

# ---------------------------------------------------------------- instructions

INST_PING = 0x01
INST_READ = 0x02
INST_WRITE = 0x03
INST_REG_WRITE = 0x04
INST_ACTION = 0x05
INST_SYNC_WRITE = 0x83

BROADCAST_ID = 0xFE

# --------------------------------------------------------------- register map
# EEPROM (survives power cycle -- must be unlocked before writing)
REG_MODEL = 3
REG_ID = 5
REG_BAUD_RATE = 6
REG_RETURN_DELAY = 7
REG_STATUS_LEVEL = 8
REG_MIN_ANGLE = 9
REG_MAX_ANGLE = 11
REG_MAX_TEMP = 13
REG_MAX_VOLT = 14
REG_MIN_VOLT = 15
REG_MAX_TORQUE = 16
REG_CW_DEAD = 26
REG_CCW_DEAD = 27
REG_OFFSET = 31          # STS only
REG_MODE = 33            # STS only -- SC09 switches mode via the angle limits

# RAM (volatile)
REG_TORQUE_ENABLE = 40
REG_ACC = 41
REG_GOAL_POSITION = 42
REG_GOAL_TIME = 44
REG_GOAL_SPEED = 46
REG_TORQUE_LIMIT = 48    # STS only -- on SC09 address 48 is the EEPROM lock
REG_LOCK_STS = 55
REG_LOCK_SCS = 48
REG_PRESENT_POSITION = 56
REG_PRESENT_SPEED = 58
REG_PRESENT_LOAD = 60
REG_PRESENT_VOLTAGE = 62
REG_PRESENT_TEMPERATURE = 63
REG_MOVING = 66
REG_PRESENT_CURRENT = 69

# STS operating modes (register 33)
MODE_POSITION = 0        # normal servo, absolute position
MODE_WHEEL = 1           # continuous rotation, closed-loop speed
MODE_PWM = 2             # continuous rotation, open-loop PWM
MODE_STEP = 3


class ServoError(Exception):
    pass


def _encode_signed(value, sign_bit):
    """Feetech encodes signed values as magnitude + a sign bit, not two's complement."""
    if value < 0:
        return (-value) | (1 << sign_bit)
    return value


def _decode_signed(value, sign_bit):
    if value & (1 << sign_bit):
        return -(value & ~(1 << sign_bit))
    return value


class _Bus:
    """Shared packet layer. Use STSBus or SCSBus, not this."""

    BIG_ENDIAN = False
    STEPS = 4096
    DEGREES = 360

    def __init__(self, uart_id=2, tx=17, rx=16, baudrate=1000000, timeout=25):
        self.uart = UART(uart_id, baudrate=baudrate, tx=tx, rx=rx,
                         bits=8, parity=None, stop=1,
                         timeout=timeout, timeout_char=4, rxbuf=256)
        self.timeout = timeout
        self.error = 0
        # Some half-duplex adapters loop our own bytes back into RX. Detected
        # on the first transaction, then cached.
        self._echo = None

    # -- word packing -------------------------------------------------------

    def _pack(self, value):
        lo, hi = value & 0xFF, (value >> 8) & 0xFF
        return bytes((hi, lo)) if self.BIG_ENDIAN else bytes((lo, hi))

    def _unpack(self, data, offset=0):
        a, b = data[offset], data[offset + 1]
        return (a << 8) | b if self.BIG_ENDIAN else (b << 8) | a

    # -- framing ------------------------------------------------------------

    def _frame(self, sid, inst, addr=None, params=b""):
        if addr is None:
            chk = sid + 2 + inst
            return b"\xff\xff" + bytes((sid, 2, inst, (~chk) & 0xFF))
        length = len(params) + 3
        chk = sid + length + inst + addr + sum(params)
        return (b"\xff\xff" + bytes((sid, length, inst, addr))
                + bytes(params) + bytes(((~chk) & 0xFF,)))

    def _drain(self):
        # Read in bounded chunks: a floating or noisy RX line can otherwise
        # make uart.read() try to allocate tens of KB in one go.
        for _ in range(64):
            if not self.uart.any():
                return
            self.uart.read(64)

    def _read_exact(self, n):
        buf = bytearray()
        deadline = time.ticks_add(time.ticks_ms(), self.timeout + 10)
        while len(buf) < n and time.ticks_diff(deadline, time.ticks_ms()) > 0:
            chunk = self.uart.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def _transact(self, packet, n_expect):
        """Send a packet, return the raw status packet (or b'' on timeout)."""
        self._drain()
        self.uart.write(packet)
        if self._echo is None:
            # First ever transaction: grab enough for echo + reply and see
            # which one we actually got.
            buf = self._read_exact(len(packet) + n_expect)
            if buf[:len(packet)] == packet:
                self._echo = True
                return buf[len(packet):]
            self._echo = False
            return buf[:n_expect]
        if self._echo:
            self._read_exact(len(packet))
        return self._read_exact(n_expect)

    def _check(self, buf, sid, n_params):
        if len(buf) < n_params + 6:
            raise ServoError("no reply from servo %d (got %d bytes)" % (sid, len(buf)))
        if buf[0] != 0xFF or buf[1] != 0xFF:
            raise ServoError("bad header from servo %d: %s" % (sid, buf))
        if buf[2] != sid:
            raise ServoError("reply from id %d, expected %d" % (buf[2], sid))
        chk = (~sum(buf[2:5 + n_params])) & 0xFF
        if chk != buf[5 + n_params]:
            raise ServoError("checksum error from servo %d" % sid)
        self.error = buf[4]
        return buf[5:5 + n_params]

    # -- primitives ---------------------------------------------------------

    def ping(self, sid):
        """True if a servo with this id answers."""
        try:
            self._check(self._transact(self._frame(sid, INST_PING), 6), sid, 0)
            return True
        except ServoError:
            return False

    def scan(self, first=0, last=20):
        return [i for i in range(first, last + 1) if self.ping(i)]

    def read(self, sid, addr, length):
        buf = self._transact(self._frame(sid, INST_READ, addr, bytes((length,))),
                             length + 6)
        return self._check(buf, sid, length)

    def write(self, sid, addr, data):
        packet = self._frame(sid, INST_WRITE, addr, data)
        # A broadcast write is never acknowledged.
        if sid == BROADCAST_ID:
            self._drain()
            self.uart.write(packet)
            if self._echo:
                self._read_exact(len(packet))
            return
        self._check(self._transact(packet, 6), sid, 0)

    def read_byte(self, sid, addr):
        return self.read(sid, addr, 1)[0]

    def write_byte(self, sid, addr, value):
        self.write(sid, addr, bytes((value & 0xFF,)))

    def read_word(self, sid, addr):
        return self._unpack(self.read(sid, addr, 2))

    def write_word(self, sid, addr, value):
        self.write(sid, addr, self._pack(value))

    # -- units --------------------------------------------------------------

    def steps_to_deg(self, steps):
        return steps * self.DEGREES / self.STEPS

    def deg_to_steps(self, deg):
        return int(deg * self.STEPS / self.DEGREES)

    # -- common commands ----------------------------------------------------

    def torque(self, sid, on=True):
        self.write_byte(sid, REG_TORQUE_ENABLE, 1 if on else 0)

    def position(self, sid):
        return self.read_word(sid, REG_PRESENT_POSITION)

    def speed(self, sid):
        return _decode_signed(self.read_word(sid, REG_PRESENT_SPEED), 15)

    def load(self, sid):
        return _decode_signed(self.read_word(sid, REG_PRESENT_LOAD), 10)

    def voltage(self, sid):
        return self.read_byte(sid, REG_PRESENT_VOLTAGE) / 10.0

    def temperature(self, sid):
        return self.read_byte(sid, REG_PRESENT_TEMPERATURE)

    def is_moving(self, sid):
        return bool(self.read_byte(sid, REG_MOVING))

    def status(self, sid):
        return {
            "pos": self.position(sid),
            "deg": round(self.steps_to_deg(self.position(sid)), 1),
            "speed": self.speed(sid),
            "load": self.load(sid),
            "volts": self.voltage(sid),
            "temp": self.temperature(sid),
            "moving": self.is_moving(sid),
        }

    def wait(self, sid, timeout_ms=4000):
        """Block until the servo stops moving."""
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        time.sleep_ms(30)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            if not self.is_moving(sid):
                return True
            time.sleep_ms(20)
        return False

    # -- EEPROM -------------------------------------------------------------

    LOCK_ADDR = REG_LOCK_STS

    def unlock_eeprom(self, sid):
        self.write_byte(sid, self.LOCK_ADDR, 0)

    def lock_eeprom(self, sid):
        self.write_byte(sid, self.LOCK_ADDR, 1)

    def set_id(self, sid, new_id):
        """Permanent. Put ONE servo on the bus before calling this."""
        self.unlock_eeprom(sid)
        self.write_byte(sid, REG_ID, new_id)
        self.lock_eeprom(new_id)


class STSBus(_Bus):
    """ST3020 / ST3215 / SMS-STS series. Little-endian, 0..4095 over 360 deg."""

    BIG_ENDIAN = False
    STEPS = 4096
    DEGREES = 360
    LOCK_ADDR = REG_LOCK_STS

    def move(self, sid, position, speed=2400, acc=50):
        """Go to an absolute position (0..4095). speed in steps/s, acc in steps/s^2 * 100."""
        params = (bytes((acc & 0xFF,))
                  + self._pack(_encode_signed(int(position), 15))
                  + self._pack(0)                       # goal time, unused here
                  + self._pack(int(speed)))
        self.write(sid, REG_ACC, params)

    def move_deg(self, sid, degrees, speed=2400, acc=50):
        self.move(sid, self.deg_to_steps(degrees), speed, acc)

    def set_mode(self, sid, mode):
        self.unlock_eeprom(sid)
        self.write_byte(sid, REG_MODE, mode)
        self.lock_eeprom(sid)

    def spin(self, sid, speed, acc=50):
        """Continuous rotation. Call set_mode(sid, MODE_WHEEL) first. Negative = reverse."""
        self.write_byte(sid, REG_ACC, acc)
        self.write_word(sid, REG_GOAL_SPEED, _encode_signed(int(speed), 15))

    def current(self, sid):
        """Present current in mA (ST3215/ST3020 report in 6.5 mA units)."""
        return _decode_signed(self.read_word(sid, REG_PRESENT_CURRENT), 15) * 6.5

    def sync_move(self, targets, speed=2400, acc=50):
        """targets: dict {id: position}. Moves every listed servo in one packet."""
        body = bytearray((REG_ACC, 7))
        for sid, pos in targets.items():
            body.append(sid)
            body.append(acc & 0xFF)
            body.extend(self._pack(_encode_signed(int(pos), 15)))
            body.extend(self._pack(0))
            body.extend(self._pack(int(speed)))
        length = len(body) + 2
        chk = BROADCAST_ID + length + INST_SYNC_WRITE + sum(body)
        self._drain()
        packet = (b"\xff\xff" + bytes((BROADCAST_ID, length, INST_SYNC_WRITE))
                  + bytes(body) + bytes(((~chk) & 0xFF,)))
        self.uart.write(packet)
        if self._echo:
            self._read_exact(len(packet))


class SCSBus(_Bus):
    """SC09 / SCS0009 / SCS series. BIG-endian, 0..1023 over 300 deg."""

    BIG_ENDIAN = True
    STEPS = 1024
    DEGREES = 300
    LOCK_ADDR = REG_LOCK_SCS

    def move(self, sid, position, speed=0, run_time=0):
        """Go to an absolute position (0..1023).

        speed is in steps/s; 0 means 'as fast as possible'.
        run_time (ms) is an alternative to speed -- set one or the other.
        """
        params = (self._pack(int(position))
                  + self._pack(int(run_time))
                  + self._pack(int(speed)))
        self.write(sid, REG_GOAL_POSITION, params)

    def move_deg(self, sid, degrees, speed=0, run_time=0):
        self.move(sid, self.deg_to_steps(degrees), speed, run_time)

    def set_wheel_mode(self, sid, on=True):
        """SC09 has no mode register -- zeroing both angle limits enables PWM/wheel mode."""
        self.unlock_eeprom(sid)
        if on:
            self.write(sid, REG_MIN_ANGLE, self._pack(0) + self._pack(0))
        else:
            self.write(sid, REG_MIN_ANGLE, self._pack(0) + self._pack(1023))
        self.lock_eeprom(sid)

    def spin(self, sid, pwm):
        """Continuous rotation, -1000..1000. Call set_wheel_mode(sid) first."""
        self.write_word(sid, REG_GOAL_TIME, _encode_signed(int(pwm), 15))
