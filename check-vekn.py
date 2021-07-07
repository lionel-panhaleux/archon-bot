#!/usr/bin/env python3
import aiohttp
import asyncio
import csv
import os
import sys


VEKN_LOGIN = os.getenv("VEKN_LOGIN")
VEKN_PASSWORD = os.getenv("VEKN_PASSWORD")


async def main(data):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://www.vekn.net/api/vekn/login",
            data={"username": VEKN_LOGIN, "password": VEKN_PASSWORD},
        ) as response:
            result = await response.json()
            try:
                token = result["data"]["auth"]
            except:  # noqa: E722
                token = None
        if not token:
            print("Unable to authentify to VEKN", file=sys.stderr)
            return
        results = await asyncio.gather(
            *(
                fetch_official_vekn(session, token, vekn.strip(" #\r\n"))
                for vekn in data
            )
        )
        writer = csv.writer(sys.stdout)
        writer.writerows(results)


async def fetch_official_vekn(session, token, vekn):
    async with session.get(
        f"https://www.vekn.net/api/vekn/registry?filter={vekn}",
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        result = await response.json()
        # print("Received: %s", result, file=sys.stderr)
        result = result["data"]
        if isinstance(result, str):
            return False, f"VEKN returned an error: {result}"
        result = result["players"]
        if len(result) > 1:
            return False, "Incomplete VEKN ID#"
        if len(result) < 1:
            return False, "VEKN ID# not found"
        result = result[0]
        if result["veknid"] != str(vekn):
            return False, "VEKN ID# not found"
        return (
            vekn,
            True,
            result["firstname"] + " " + result["lastname"],
            result["countryname"],
        )


if __name__ == "__main__":
    asyncio.run(main(sys.stdin.readlines()))
