#!/usr/bin/env python3
"""Set or clear the navigation destination used by navd.

navd turns the destination into a route (Mapbox when `MAPBOX_TOKEN` is set, otherwise the
public OSRM server) and publishes `navRoute`/`navInstruction`. Keeping the destination in
Params rather than a launch-time environment variable means it survives restarts and can
also be written by athenad's `setNavDestination`.

  python -m openpilot.selfdrive.navigation.set_destination 37.7749 -122.4194 --name "Ferry Building"
  python -m openpilot.selfdrive.navigation.set_destination --clear
"""
import argparse
import sys

from openpilot.common.params import Params

KEY = "NavDestination"


def parse_args(argv=None):
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("latitude", nargs="?", type=float, help="destination latitude in degrees")
  parser.add_argument("longitude", nargs="?", type=float, help="destination longitude in degrees")
  parser.add_argument("--name", default=None, help="human readable place name")
  parser.add_argument("--clear", action="store_true", help="remove the destination")
  args = parser.parse_args(argv)
  if not args.clear and (args.latitude is None or args.longitude is None):
    parser.error("latitude and longitude are required unless --clear is given")
  return args


def apply_destination(params: Params, latitude: float, longitude: float, name: str | None = None) -> dict:
  if not -90.0 <= latitude <= 90.0:
    raise ValueError("latitude out of range")
  if not -180.0 <= longitude <= 180.0:
    raise ValueError("longitude out of range")
  destination = {"latitude": float(latitude), "longitude": float(longitude), "place_name": name}
  params.put(KEY, destination)
  return destination


def main(argv=None, params: Params | None = None) -> int:
  args = parse_args(argv)
  params = params if params is not None else Params()
  if args.clear:
    params.remove(KEY)
    print("destination cleared")
    return 0
  destination = apply_destination(params, args.latitude, args.longitude, args.name)
  print(f"destination set: {destination['latitude']:.6f}, {destination['longitude']:.6f}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
