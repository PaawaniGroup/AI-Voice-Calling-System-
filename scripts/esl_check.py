"""Verify ESL connectivity/auth and run read-only or reload commands using the service's own ESL client.

Usage: python scripts/esl_check.py [api-command ...]     (default: status)
Password: MME_TEL_ESL_PASSWORD, else read from the local FreeSWITCH event_socket.conf.xml (never printed).
"""
import asyncio
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.telephony.esl import EslCommandClient  # noqa: E402

CONF = Path(os.environ.get("FS_CONF", "/usr/local/freeswitch/etc/freeswitch")) / "autoload_configs" / "event_socket.conf.xml"


def esl_password() -> str:
    pw = os.environ.get("MME_TEL_ESL_PASSWORD", "")
    if pw:
        return pw
    try:
        for p in ET.parse(CONF).getroot().iter("param"):
            if p.attrib.get("name") == "password":
                return p.attrib["value"]
    except (OSError, ET.ParseError):
        pass
    return ""


async def main(cmds: list[str]) -> int:
    c = EslCommandClient(os.environ.get("MME_TEL_ESL_HOST", "127.0.0.1"), int(os.environ.get("MME_TEL_ESL_PORT", "8021")),
                         esl_password(), connect_timeout=5, reconnect_initial=0.5, reconnect_max=5, jitter=0.1)
    try:
        for cmd in cmds or ["status"]:
            print(f"> {cmd}")
            print(await c.api(cmd))
        return 0
    except Exception as e:
        print(f"ESL check failed: {type(e).__name__}")
        return 1
    finally:
        await c.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
