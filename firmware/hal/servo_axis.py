"""The real servo axis: an AxisDriver talking to a Feetech/Waveshare bus servo.

All the logic lives in hal/axis_base.py and is shared with the simulator; this
file only turns "go to this servo angle" and "what is your state" into bus
transactions, using the verbatim driver in hal/scservo.py.

One read per refresh: registers 56..66 are contiguous -- position, speed, load,
voltage, temperature, then the moving flag at 66 -- so a single 11-byte READ
returns everything the tracker and the supervisor need, instead of the six
separate transactions scservo.status() would make.
"""

from control.kinematics import FAMILY_DEGREES, FAMILY_STEPS

from hal import board
from hal.axis_base import AxisDriver
from hal.scservo import REG_PRESENT_POSITION, SCSBus, ServoError, STSBus

_BLOCK_START = REG_PRESENT_POSITION  # 56
_BLOCK_LEN = 11  # 56..66 inclusive

# Short bus timeout. A healthy servo answers in well under a millisecond at
# 1 Mbps; scservo busy-waits for the full timeout on a silent one, and that wait
# stalls the whole asyncio loop, so keep it small.
_BUS_TIMEOUT_MS = 10

_ACCELERATION = 50  # STS only; the SCS family has no acceleration register


class ServoAxis(AxisDriver):
    def __init__(self, axis, bus, max_missed=3):
        super().__init__(axis)
        self.bus = bus
        self.sid = axis.servo_id
        self._steps = FAMILY_STEPS[axis.family]
        self._step_deg = FAMILY_DEGREES[axis.family] / self._steps
        self._max_missed = max_missed
        self._is_sts = axis.family == "STS"

    async def _command(self, servo_deg, speed_dps):
        position = int(round(servo_deg / self._step_deg))
        position = max(0, min(self._steps - 1, position))
        dps = speed_dps if speed_dps else self.axis.max_speed_dps
        speed = max(1, int(dps * self.axis.gear_ratio / self._step_deg))
        try:
            if self._is_sts:
                self.bus.move(self.sid, position, speed=speed, acc=_ACCELERATION)
            else:
                self.bus.move(self.sid, position, speed=speed)
        except ServoError:
            self._missed()

    async def _refresh(self):
        try:
            block = self.bus.read(self.sid, _BLOCK_START, _BLOCK_LEN)
        except ServoError:
            self._missed()
            return

        unpack = self.bus._unpack
        position = unpack(block, 0)
        load = unpack(block, 4)
        self.state["missed"] = 0
        self.state["ok"] = True
        self.state["position_deg"] = self.axis.from_servo_deg(position * self._step_deg)
        self.state["load"] = -(load & 0x3FF) if load & 0x400 else load  # sign in bit 10
        self.state["volts"] = block[6] / 10.0
        self.state["temp_c"] = block[7]
        self.state["moving"] = bool(block[10])

    def _set_torque(self, on):
        try:
            self.bus.torque(self.sid, on)
        except ServoError:
            self._missed()

    def _missed(self):
        self.state["missed"] += 1
        self.state["ok"] = self.state["missed"] < self._max_missed


def make_real_axes(cfg, models, max_missed):
    """Build one bus per servo family, all sharing ONE UART object.

    Each STSBus/SCSBus constructs its own UART(2). On the ESP32 two UART objects
    on the same peripheral are NOT interchangeable: once the first has been used,
    the second never receives another byte. Found on the bench -- the SC09 went
    silent the moment the ST3020's bus had been used first -- so every bus after
    the first is handed the first one's UART.
    """
    buses = {}
    axes = []
    shared_uart = None
    for model in models:
        if model.family not in buses:
            cls = STSBus if model.family == "STS" else SCSBus
            bus = cls(
                uart_id=board.SERVO_UART,
                tx=board.SERVO_TX,
                rx=board.SERVO_RX,
                baudrate=board.SERVO_BAUD,
                timeout=_BUS_TIMEOUT_MS,
            )
            if shared_uart is None:
                shared_uart = bus.uart
            else:
                bus.uart = shared_uart
            buses[model.family] = bus
        bus = buses[model.family]
        # Transact once now, so the bus's half-duplex echo detection happens
        # here and not in the middle of the control loop.
        bus.ping(model.servo_id)
        axes.append(ServoAxis(model, bus, max_missed))
    return tuple(axes)
