"""Behaviour shared by the real and the simulated axis drivers.

Everything that is not "talk to the servo" lives here, so the simulator and the
hardware cannot drift apart: soft-limit enforcement, backlash compensation, and
the cached state the tracker and the safety supervisor read.

Subclasses implement three hooks:

    async _command(servo_deg, speed_dps)   send a position to the servo
    async _refresh()                        read state back into self.state
    _set_torque(on)                         enable the motor HOLDING ITS PRESENT
                                            position, or release it

The public surface is identical for both, which is the whole point:
control/tracker.py receives one of these by constructor injection and never
learns which kind it has.
"""

from control.calibration import apply_backlash


class AxisDriver:
    def __init__(self, axis):
        self.axis = axis
        self.torque_on = False
        self._final_deg = None  # where the operator or tracker wants to end up
        self._leg_deg = None  # what is being commanded right now
        self._previous_final_deg = None
        self.state = {
            "name": axis.name,
            "family": axis.family,
            "servo_id": axis.servo_id,
            "position_deg": None,  # mechanical, from the encoder
            "target_deg": None,
            "moving": False,
            "volts": None,
            "temp_c": None,
            "load": None,
            "missed": 0,
            "ok": True,
        }

    # -- commands -----------------------------------------------------------

    async def move_deg(self, mech_deg, speed_dps=None):
        """Command a mechanical angle, clamped to the soft limits.

        If the direction reverses and the axis has backlash configured, this
        first drives PAST the target and lets update() bring it back, so the gear
        teeth always settle loaded the same way. Returns True if the request was
        clamped.
        """
        target, clamped = self.axis.clamp(mech_deg)
        approach, overshoot = apply_backlash(
            target, self._previous_final_deg, self.axis.backlash_deg
        )
        approach, _ = self.axis.clamp(approach)

        self._previous_final_deg = target
        self._final_deg = target
        self._leg_deg = approach if overshoot else target
        self.state["target_deg"] = target
        await self._send(self._leg_deg, speed_dps)
        return clamped

    async def update(self):
        """Refresh state, and finish a backlash overshoot once it has arrived."""
        await self._refresh()
        if (
            self._leg_deg is not None
            and self._leg_deg != self._final_deg
            and not self.state["moving"]
            and self.state["ok"]
        ):
            self._leg_deg = self._final_deg
            await self._send(self._final_deg, None)

    async def _send(self, mech_deg, speed_dps):
        await self._command(self.axis.to_servo_deg(mech_deg), speed_dps)
        # Until a refresh proves otherwise, a commanded axis is a MOVING axis.
        # Without this, `settled` reads the stale "stopped" from the refresh that
        # came before the command -- and reports a joint mid-backlash-return,
        # a full degree off target, as settled on it.
        self.state["moving"] = True

    def torque(self, on):
        """Enable or release the motor. Either way, any unfinished move is dropped.

        A servo keeps its last goal in a register through a torque release, and
        drives straight to it the moment torque returns. Found on the mounted
        structure: a stow into a pose the frame cannot reach stalled the servo
        and latched a fault; clearing the fault re-enabled torque and the servo
        went back into the frame. So enabling torque HOLDS WHERE THE JOINT IS
        (the _set_torque hook does that), and nothing pending is replayed.
        """
        self._leg_deg = None
        self._final_deg = None
        self._previous_final_deg = None
        self.state["target_deg"] = None
        self.torque_on = bool(on)
        self._set_torque(self.torque_on)

    # -- readings ---------------------------------------------------------------

    def position_deg(self):
        """Mechanical angle from the last refresh, or None before the first."""
        return self.state["position_deg"]

    @property
    def moving(self):
        return self.state["moving"]

    @property
    def settled(self):
        """Stopped, and at the final target rather than mid-overshoot."""
        return not self.state["moving"] and self._leg_deg == self._final_deg

    def status(self):
        return dict(self.state, torque=self.torque_on)

    # -- hooks ------------------------------------------------------------------

    async def _command(self, servo_deg, speed_dps):
        raise NotImplementedError

    async def _refresh(self):
        raise NotImplementedError

    def _set_torque(self, on):
        raise NotImplementedError
