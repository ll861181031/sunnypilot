openpilot in simulator
=====================

openpilot implements a [bridge](run_bridge.py) that allows it to run in the [MetaDrive simulator](https://github.com/metadriverse/metadrive).

## Launching openpilot
First, start openpilot.
``` bash
# Run locally
./openpilot/tools/sim/launch_openpilot.sh
```

## Bridge usage
```
$ ./run_bridge.py -h
usage: run_bridge.py [-h] [--joystick] [--high_quality] [--dual_camera]
Bridge between the simulator and openpilot.

options:
  -h, --help            show this help message and exit
  --joystick
  --high_quality
  --dual_camera
```

此分支仅支持手动驾驶与传感器模拟，使用无输出的 `MOCK` 接口。没有经过验证的宋 PLUS DM-i ALT 闭环模拟车，不生成该车型的 CAN 数据。

#### All inputs:

```
| key  |   functionality       |
|------|-----------------------|
|  r   | Reset Simulation      |
|  i   | Toggle Ignition       |
|  q   | Exit all              |
| wasd | Control manually      |
```

## MetaDrive

### Launching Metadrive
Start bridge processes located in openpilot/tools/sim:
``` bash
./run_bridge.py
```
