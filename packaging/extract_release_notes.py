#!/usr/bin/env python3
"""Pulls one release's own <description> out of this file's sibling
metainfo.xml and prints it as plain Markdown bullets -- the single
source of truth for a GitHub release's notes, so the two never drift
apart the way a separately hand-typed release body would.

Usage: extract_release_notes.py <version-without-leading-v>
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

def main():
    if len(sys.argv) != 2:
        sys.exit("usage: extract_release_notes.py <version>")
    version = sys.argv[1]
    metainfo_path = Path(__file__).parent / "io.github.ScarletPachydermDev.SelfSteam.metainfo.xml"
    tree = ET.parse(metainfo_path)
    for release in tree.getroot().find("releases").findall("release"):
        if release.get("version") != version:
            continue
        desc = release.find("description")
        items = [li.text.strip() for li in desc.findall(".//li")]
        if items:
            print("\n".join(f"- {item}" for item in items))
        else:
            print("\n".join(p.text.strip() for p in desc.findall(".//p")))
        return
    sys.exit(f"version {version} not found in {metainfo_path}")

if __name__ == "__main__":
    main()
