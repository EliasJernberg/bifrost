#!/usr/bin/env python3
"""Flip "Run on Startup" for the Bifrost add-in without touching Fusion's GUI.

Fusion keeps its per-user list of known scripts and add-ins in

    <prefix>/drive_c/users/<user>/AppData/Roaming/Autodesk/Autodesk Fusion 360/
        <ACCOUNT_ID>/JSLoadedScriptsinfo

as plain JSON: a "loadedScripts" array of {name, path, location, isRemoved,
isFavorite, runOnStartup}. Fusion scans the AddIns folder at startup and appends
anything new it finds, so the reliable sequence is:

    1. install the add-in folder
    2. start Fusion once (it discovers Bifrost and writes an entry)
    3. quit Fusion, run this tool (it sets runOnStartup to true)
    4. start Fusion again

Running this before Fusion has ever seen the add-in still works: the entry is
created from scratch, using the same shape Fusion uses for user add-ins.

Fusion rewrites the file when it exits, so never run this while Fusion is up.
"""

import argparse
import glob
import json
import os
import sys

ADDIN_NAME = "Bifrost"
# location 3 is the user's API/AddIns folder. 4 is Autodesk's bundled add-ins,
# 5 the samples that ship with Fusion.
DEFAULT_LOCATION = 3


def find_registry(prefix, user):
    base = os.path.join(
        prefix,
        "drive_c",
        "users",
        user,
        "AppData",
        "Roaming",
        "Autodesk",
        "Autodesk Fusion 360",
    )
    hits = sorted(glob.glob(os.path.join(base, "*", "JSLoadedScriptsinfo")))
    return hits, base


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--prefix",
        default=os.path.expanduser("~/.autodesk_fusion/wineprefixes/default"),
    )
    parser.add_argument("--user", default=os.environ.get("USER", "user"))
    parser.add_argument("--name", default=ADDIN_NAME)
    parser.add_argument("--location", type=int, default=DEFAULT_LOCATION)
    parser.add_argument(
        "--off", action="store_true", help="clear runOnStartup instead of setting it"
    )
    parser.add_argument(
        "--show", action="store_true", help="only print the current entries"
    )
    args = parser.parse_args(argv)

    registries, base = find_registry(args.prefix, args.user)
    if not registries:
        print("no JSLoadedScriptsinfo under %s" % base, file=sys.stderr)
        return 1

    want = not args.off
    changed_any = False
    for path in registries:
        try:
            with open(path, "r") as handle:
                data = json.load(handle)
        except Exception as exc:
            print("cannot parse %s: %s" % (path, exc), file=sys.stderr)
            continue
        scripts = data.setdefault("loadedScripts", [])

        if args.show:
            print(path)
            for entry in scripts:
                if entry.get("location") not in (4, 5):
                    print("   user entry: %s" % json.dumps(entry))
            continue

        entry = None
        for candidate in scripts:
            if candidate.get("name") == args.name:
                entry = candidate
                break
        if entry is None:
            entry = {
                "name": args.name,
                "path": "C:/users/%s/AppData/Roaming/Autodesk/Autodesk Fusion 360"
                "/API/AddIns/%s/%s.py" % (args.user, args.name, args.name),
                "location": args.location,
                "isRemoved": False,
                "isFavorite": False,
                "runOnStartup": want,
            }
            scripts.append(entry)
            print("added %s entry to %s" % (args.name, path))
            changed_any = True
        else:
            if entry.get("runOnStartup") != want or entry.get("isRemoved"):
                entry["runOnStartup"] = want
                entry["isRemoved"] = False
                print("set runOnStartup=%s for %s in %s" % (want, args.name, path))
                changed_any = True
            else:
                print("%s already has runOnStartup=%s in %s" % (args.name, want, path))

        with open(path, "w") as handle:
            json.dump(data, handle, indent=1)

    if args.show:
        return 0
    if not changed_any:
        print("nothing to change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
