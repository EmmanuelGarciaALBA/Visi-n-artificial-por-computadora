"""Simula un PLC/SCADA: prueba el servidor TCP y el servidor OPC UA.

Uso:  python tools/test_clients.py [IP] [puerto_tcp] [puerto_opcua]   (por defecto 127.0.0.1 5000 4841)
"""
import asyncio
import socket
import sys

from asyncua import Client

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
TCP_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
OPC_PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 4841
BLOCKS = ("Safety", "PPE", "Andon", "Stock")


def test_tcp():
    print(f"--- TCP {HOST}:{TCP_PORT}")
    with socket.create_connection((HOST, TCP_PORT), timeout=5) as s:
        f = s.makefile("rwb")
        for cmd in ["PING", "STATUS", "SAFETY", "EPP", "ANDON", "STOCK", "RECIPE STOCK", "FOO"]:
            f.write((cmd + "\r\n").encode())
            f.flush()
            print(f"{cmd:13} -> {f.readline().decode().strip()}")


async def test_opcua():
    url = f"opc.tcp://{HOST}:{OPC_PORT}/visionserver/"
    print(f"--- OPC UA {url}")
    async with Client(url) as c:
        idx = await c.get_namespace_index("urn:visionserver")
        node = lambda p: c.get_node(f"ns={idx};s=VisionServer.{p}")
        print("Heartbeat :", await node("Status.Heartbeat").read_value())
        print("CamerasOk :", await node("Status.CamerasOk").read_value())
        for b in BLOCKS:
            vals = [await node(f"{b}.{n}").read_value() for n in ("Enabled", "Alarm", "Warning", "AlarmText")]
            print(f"{b:7} Enabled={vals[0]} Alarm={vals[1]} Warning={vals[2]} Texto={vals[3]!r}")


if __name__ == "__main__":
    test_tcp()
    asyncio.run(test_opcua())
