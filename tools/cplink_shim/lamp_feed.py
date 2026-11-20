#!/usr/bin/env python3
"""Fake 7711 lamp feed: 4-byte big-endian length + UTF-8 JSON at 20 Hz.

carrot_man xiaoge_data source is present.
"""
import json
import socket
import struct
import time

HOST = "0.0.0.0"
PORT = 7711


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(4)
    print(f"lamp_feed listening on {PORT}", flush=True)
    while True:
        conn, addr = srv.accept()
        print(f"client {addr}", flush=True)
        seq = 0
        try:
            while True:
                seq += 1
                color = (1, 2, 3, 1, 2)[(seq // 40) % 5]
                dist = round(max(0.0, 60.0 - (seq % 600) * 0.1), 2)
                frame = {
                    "version": 1,
                    "sequence": seq,
                    "timestamp": int(time.time() * 1000),
                    "ip": "192.168.123.70",
                    "data": {"carState": {"stopLineDist": dist, "trafficLightColor": color}},
                }
                raw = json.dumps(frame, ensure_ascii=False).encode("utf-8")
                conn.sendall(struct.pack(">I", len(raw)) + raw)
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            print("client gone", flush=True)
        finally:
            conn.close()


if __name__ == "__main__":
    main()