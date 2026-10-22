"""
cgate.sideload — register the add-in with the copy of Word on this machine.

Word does not discover add-ins. On Windows a developer manifest is registered
under `HKCU\\Software\\Microsoft\\Office\\16.0\\WEF\\Developer`, where each value
is a path to a manifest. That is the whole mechanism.

Two properties of that key matter here. It is per-user, so nothing done by this
module needs elevation or touches another account. And it is a pointer, not a
copy — the manifest stays in the repository, so editing it and restarting Word
is the entire edit cycle, and removing the value removes the add-in completely.

This writes to the registry, which is a change to the machine. It is therefore
its own command that a person runs on purpose, never a side effect of starting
the server, and `--remove` puts it back.
"""

from __future__ import annotations

import os
import sys

VALUE_NAME = "CGate.ComplianceReview"
# Office 16.0 covers 2016, 2019, 2021 and Microsoft 365 — every build that
# supports the add-in model on Windows.
KEY = r"Software\Microsoft\Office\16.0\WEF\Developer"


def manifest_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "addin", "manifest.xml")


def validate(path: str) -> list[str]:
    """Problems that would stop Word loading this manifest.

    Word does not report a bad manifest. It does not warn, log or grey the button
    out — the add-in simply never appears, which is indistinguishable from having
    forgotten to install it. This build shipped with a comment containing `--`,
    which is illegal inside an XML comment and made the whole file unparseable;
    the symptom was an empty ribbon. So the check lives here, and runs on every
    install.
    """
    import xml.etree.ElementTree as ET

    problems: list[str] = []
    ns = {"o": "http://schemas.microsoft.com/office/appforoffice/1.1"}

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return [f"not well-formed XML ({exc}). Word will ignore it without saying so."]
    except OSError as exc:
        return [f"could not be read: {exc}"]

    if not root.tag.endswith("OfficeApp"):
        problems.append(f"root element is {root.tag}, expected OfficeApp")

    for tag in ("Id", "Version", "ProviderName", "DefaultLocale", "DisplayName",
                "Description", "DefaultSettings", "Permissions", "Hosts"):
        if root.find("o:" + tag, ns) is None:
            problems.append(f"missing required element <{tag}>")

    # An empty <AppDomains/> fails schema validation. Omitting it is correct.
    domains = root.find("o:AppDomains", ns)
    if domains is not None and len(list(domains)) == 0:
        problems.append("<AppDomains/> is present but empty; omit the element entirely")

    if not any("VersionOverrides" in child.tag for child in root):
        problems.append("no <VersionOverrides>, so no ribbon button will be created")

    # Every resid a control points at has to resolve, or the ribbon silently
    # drops the control that references it.
    declared = {e.get("id") for e in root.iter() if e.get("id")}
    for el in root.iter():
        resid = el.get("resid")
        if resid and resid not in declared:
            problems.append(f"resid {resid!r} is referenced but never declared in <Resources>")

    return problems


def _require_windows() -> bool:
    if sys.platform != "win32":
        print(
            "Sideloading through the registry is a Windows mechanism. On macOS, copy "
            "the manifest to ~/Library/Containers/com.microsoft.Word/Data/Documents/wef:",
            file=sys.stderr,
        )
        print(f"  {manifest_path()}", file=sys.stderr)
        return False
    return True


def install() -> int:
    if not _require_windows():
        return 1
    import winreg

    path = manifest_path()
    if not os.path.exists(path):
        print(f"manifest not found: {path}", file=sys.stderr)
        return 2

    problems = validate(path)
    if problems:
        print("refusing to register: Word would ignore this manifest without "
              "reporting an error.", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 3

    # The Developer key does not exist until an add-in has been sideloaded once.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, path)

    print(f"Registered for this user: {path}")
    print()
    print("Next:")
    print("  1. cgate trust           trust the localhost certificate Word requires")
    print("  2. cgate serve           start the review host")
    print("  3. restart Word          the ribbon is read at start-up")
    print()
    print("The button is on the Home tab, in a group called Compliance.")
    return 0


def remove() -> int:
    if not _require_windows():
        return 1
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        print("Not registered; nothing to remove.")
        return 0
    print("Removed. Restart Word and the button is gone.")
    return 0


def status() -> int:
    if sys.platform != "win32":
        print(f"manifest: {manifest_path()}")
        return 0
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        print(f"Registered: {value}")
        if not os.path.exists(value):
            print("MISSING on disk — Word has nothing to load.")
            return 1
        problems = validate(value)
        if problems:
            print("Manifest is registered but INVALID. Word will show no button "
                  "and no error:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("Manifest present and valid.")
    except FileNotFoundError:
        print("Not registered. Run: cgate addin --install")
    return 0
