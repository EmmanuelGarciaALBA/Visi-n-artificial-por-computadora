"""Simula un PLC/SCADA: prueba el servidor TCP y el servidor OPC UA.

Uso:  python tools/test_clients.py [IP]   (por defecto 127.0.0.1)
"""
import asyncio
import socket
import sys

from asyncua import Client

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"


def test_tcp(port=5000):
    print(f"--- TCP {HOST}:{port}")
    with socket.create_connection((HOST, port), timeout=5) as s:
        f = s.makefile("rwb")
        for cmd in ["PING", "STATUS", "TRIGGER", "GET", "FOO"]:
            f.write((cmd + "\r\n").encode())
            f.flush()
            print(f"{cmd:8} -> {f.readline().decode().strip()}")


async def test_opcua(port=4841):
    url = f"opc.tcp://{HOST}:{port}/visionserver/"
    print(f"--- OPC UA {url}")
    async with Client(url) as c:
        idx = await c.get_namespace_index("urn:visionserver")
        node = lambda p: c.get_node(f"ns={idx};s=VisionServer.{p}")
        before = await node("Result.Counter").read_value()
        await node("Control.Trigger").write_value(True)  # disparo, como lo haría el PLC
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not await node("Control.Trigger").read_value():  # acuse del servidor
                break
        await asyncio.sleep(0.1)
        print("Counter      :", before, "->", await node("Result.Counter").read_value())
        print("Pass         :", await node("Result.Pass").read_value())
        print("CycleTimeMs  :", await node("Result.CycleTimeMs").read_value())
        print("Heartbeat    :", await node("Status.Heartbeat").read_value())
        print("CameraOk     :", await node("Status.CameraOk").read_value())
        tools = await node("Tools").get_children()
        for t in tools:
            name = (await t.read_browse_name()).Name
            val = await c.get_node(f"ns={idx};s=VisionServer.Tools.{name}.Value").read_value()
            ok = await c.get_node(f"ns={idx};s=VisionServer.Tools.{name}.Pass").read_value()
            print(f"Tools.{name:8}: {val}  pass={ok}")


if __name__ == "__main__":
    test_tcp()
    asyncio.run(test_opcua())
