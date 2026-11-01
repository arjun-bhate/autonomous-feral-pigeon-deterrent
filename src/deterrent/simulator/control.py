"""The test and operator side of the simulated device.

The real device has a physical key switch that is not a protocol message. In the
simulator, ``SimulatorControl`` is how the outside world turns that key. It is a
separate object on purpose: it holds the inputs the hardware would have, and the
device itself stays free of any way for outside code to push it into a state.
"""

from deterrent.simulator.device import Device


class SimulatorControl:
    def __init__(self, device: Device) -> None:
        self._device = device

    def set_key(self, on: bool) -> None:
        """Turn the arm key on or off."""
        self._device.set_key_level(on)
