# python check updates
# pcu is a script tool to check, sync and update outdated packages
# usage: python scripts/pcu.py [check|upgrade]

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


PYPROJECT_PATH = Path("pyproject.toml")
REQUIREMENTS_PATH = Path("requirements.txt")

# ANSI colors
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def run_command(cmd: str, capture: bool = True) -> tuple[int, str, str]:
    result = subprocess.run(cmd, shell=True, capture_output=capture, text=True)
    return result.returncode, result.stdout, result.stderr


def load_pyproject() -> dict:
    if tomllib is None:
        print(f"  {RED}Cannot read pyproject.toml: tomllib/tomli not available.{RESET}")
        sys.exit(1)
    content = PYPROJECT_PATH.read_text()
    return tomllib.loads(content)


def parse_dependency(dep_string: str) -> tuple[str, str, str, str]:
    """Parse a dependency string into (name, extras, operator, version)."""
    extras = ""
    name = dep_string.strip()

    # extract extras like httpx[http2]
    if "[" in name:
        bracket_start = name.index("[")
        bracket_end = name.index("]")
        extras = name[bracket_start : bracket_end + 1]
        name = name[:bracket_start] + name[bracket_end + 1 :]

    for op in [">=", "==", "~=", "<=", "!=", "<", ">"]:
        if op in name:
            parts = name.split(op, 1)
            return parts[0].strip(), extras, op, parts[1].strip()

    return name.strip(), extras, "", ""


def get_all_dependencies(pyproject: dict) -> list[dict]:
    """Extract all dependencies from pyproject.toml with their source group."""
    deps = []

    for dep_str in pyproject.get("project", {}).get("dependencies", []):
        name, extras, op, version = parse_dependency(dep_str)
        if name:
            deps.append(
                {
                    "name": name,
                    "extras": extras,
                    "operator": op,
                    "version": version,
                    "group": "main",
                    "raw": dep_str.strip(),
                }
            )

    for dep_str in pyproject.get("project", {}).get("optional-dependencies", {}).get("dev", []):
        name, extras, op, version = parse_dependency(dep_str)
        if name:
            deps.append(
                {
                    "name": name,
                    "extras": extras,
                    "operator": op,
                    "version": version,
                    "group": "dev",
                    "raw": dep_str.strip(),
                }
            )

    for dep_str in pyproject.get("dependency-groups", {}).get("dev", []):
        if isinstance(dep_str, str):
            name, extras, op, version = parse_dependency(dep_str)
            if name:
                deps.append(
                    {
                        "name": name,
                        "extras": extras,
                        "operator": op,
                        "version": version,
                        "group": "dep-group",
                        "raw": dep_str.strip(),
                    }
                )

    return deps


def get_deps_from_requirements() -> list[dict]:
    """Parse dependencies from requirements.txt."""
    deps = []
    for line in REQUIREMENTS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name, extras, op, version = parse_dependency(line)
        if name:
            deps.append(
                {
                    "name": name,
                    "extras": extras,
                    "operator": op,
                    "version": version,
                    "group": "main",
                    "raw": line,
                }
            )
    return deps


def get_installed_versions() -> dict[str, str]:
    """Get currently installed package versions via uv pip list or pip list."""
    code, stdout, _ = run_command("uv pip list --format=json")
    if code != 0:
        code, stdout, _ = run_command("pip list --format=json")
    if code != 0:
        return {}
    try:
        packages = json.loads(stdout)
        return {p["name"].lower(): p["version"] for p in packages}
    except (json.JSONDecodeError, KeyError):
        return {}


def fetch_latest_version(package_name: str) -> str | None:
    """Fetch the latest version from PyPI."""
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data["info"]["version"]
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        return None


def version_tuple(v: str) -> tuple[int, ...]:
    """Convert version string to comparable tuple."""
    parts = []
    for p in v.split("."):
        # handle pre-release suffixes like 1.0a1
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_outdated(pinned: str, latest: str) -> bool:
    """Check if pinned version is behind latest."""
    if not pinned or not latest:
        return False
    return version_tuple(pinned) < version_tuple(latest)


def detect_sources() -> tuple[bool, bool]:
    """Detect available dependency sources. Returns (has_pyproject, has_requirements)."""
    return PYPROJECT_PATH.exists(), REQUIREMENTS_PATH.exists()


def cmd_check():
    print(f"\n{BOLD}Python Check Updates{RESET}")

    has_pyproject, has_requirements = detect_sources()

    if not has_pyproject and not has_requirements:
        print(f"\n  {RED}No pyproject.toml or requirements.txt found.{RESET}\n")
        sys.exit(1)

    if has_pyproject:
        print(f"{DIM}Checking dependencies in pyproject.toml...{RESET}\n")
        pyproject = load_pyproject()
        deps = get_all_dependencies(pyproject)
    else:
        print(f"{DIM}Checking dependencies in requirements.txt...{RESET}\n")
        deps = get_deps_from_requirements()

    installed = get_installed_versions()

    if not deps:
        print("No dependencies found.")
        return

    # fetch latest versions
    print(f"{DIM}Fetching latest versions from PyPI...{RESET}\n")
    for dep in deps:
        dep["latest"] = fetch_latest_version(dep["name"]) or "?"
        dep["installed"] = installed.get(dep["name"].lower(), "—")

    # column widths
    name_w = max(len(d["name"] + d["extras"]) for d in deps) + 2
    pin_w = max(len(d["operator"] + d["version"]) for d in deps) + 2
    inst_w = max(len(d["installed"]) for d in deps) + 2
    lat_w = max(len(d["latest"]) for d in deps) + 2

    name_w = max(name_w, 10)
    pin_w = max(pin_w, 10)
    inst_w = max(inst_w, 12)
    lat_w = max(lat_w, 10)

    # header
    header = f"  {'Package':<{name_w}}{'Pinned':<{pin_w}}{'Installed':<{inst_w}}{'Latest':<{lat_w}}{'Status'}"
    print(f"{BOLD}{header}{RESET}")
    print(f"  {'─' * (name_w + pin_w + inst_w + lat_w + 10)}")

    outdated_count = 0
    current_group = None

    for dep in deps:
        # group separator
        if dep["group"] != current_group:
            current_group = dep["group"]
            group_label = {"main": "dependencies", "dev": "optional-dependencies.dev", "dep-group": "dependency-groups.dev"}
            print(f"\n  {DIM}[{group_label.get(current_group, current_group)}]{RESET}")

        name_display = dep["name"] + dep["extras"]
        pinned_display = dep["operator"] + dep["version"] if dep["version"] else "any"
        outdated = is_outdated(dep["version"], dep["latest"])

        if outdated:
            outdated_count += 1
            status = f"{YELLOW}update available{RESET}"
            latest_display = f"{YELLOW}{dep['latest']}{RESET}"
        else:
            status = f"{GREEN}up to date{RESET}"
            latest_display = f"{GREEN}{dep['latest']}{RESET}"

        # check if installed version differs from latest
        inst_display = dep["installed"]
        if dep["installed"] != "—" and dep["latest"] != "?":
            if is_outdated(dep["installed"], dep["latest"]):
                inst_display = f"{RED}{dep['installed']}{RESET}"
            else:
                inst_display = f"{GREEN}{dep['installed']}{RESET}"

        # we need raw lengths for padding (without ANSI codes)
        raw_latest = dep["latest"]
        raw_inst = dep["installed"]

        line = (
            f"  {name_display:<{name_w}}"
            f"{pinned_display:<{pin_w}}"
            f"{inst_display}{' ' * (inst_w - len(raw_inst))}"
            f"{latest_display}{' ' * (lat_w - len(raw_latest))}"
            f"{status}"
        )
        print(line)

    print()
    if outdated_count:
        print(f"  {YELLOW}{outdated_count} package(s) can be updated.{RESET}")
        print(f"  Run {BOLD}poe pcu:upgrade{RESET} to update.\n")
    else:
        print(f"  {GREEN}All pinned versions are up to date!{RESET}\n")


def update_pyproject_content(content: str, dep: dict, new_version: str) -> str:
    """Update a dependency version in pyproject.toml content."""
    old_str = dep["raw"]
    if dep["operator"] and dep["version"]:
        new_str = old_str.replace(
            f"{dep['operator']}{dep['version']}",
            f"{dep['operator']}{new_version}",
        )
    else:
        # no version pinned, add one
        new_str = f"{dep['name']}{dep['extras']}>={new_version}"

    # handle quoting — the raw string in the toml is quoted
    # find and replace the quoted version
    for quote in ['"', "'"]:
        quoted_old = f"{quote}{old_str}{quote}"
        if quoted_old in content:
            quoted_new = f"{quote}{new_str}{quote}"
            content = content.replace(quoted_old, quoted_new)
            break

    return content


def update_requirements_content(content: str, dep: dict, new_version: str) -> str:
    """Update a dependency version in requirements.txt content."""
    old_str = dep["raw"]
    if dep["operator"] and dep["version"]:
        new_str = old_str.replace(
            f"{dep['operator']}{dep['version']}",
            f"{dep['operator']}{new_version}",
        )
    else:
        new_str = f"{dep['name']}{dep['extras']}>={new_version}"

    content = content.replace(old_str, new_str)
    return content


def generate_requirements(pyproject: dict) -> str:
    """Generate requirements.txt from main dependencies."""
    lines = []
    for dep_str in pyproject.get("project", {}).get("dependencies", []):
        lines.append(dep_str.strip())
    return "\n".join(sorted(lines)) + "\n"


def cmd_upgrade():
    print(f"\n{BOLD}Python Check Updates{RESET}")
    print(f"{DIM}Upgrading dependencies...{RESET}\n")

    has_pyproject, has_requirements = detect_sources()

    if not has_pyproject and not has_requirements:
        print(f"\n  {RED}No pyproject.toml or requirements.txt found.{RESET}\n")
        sys.exit(1)

    if has_pyproject:
        pyproject = load_pyproject()
        deps = get_all_dependencies(pyproject)
    else:
        deps = get_deps_from_requirements()

    installed = get_installed_versions()

    if not deps:
        print("No dependencies found.")
        return

    print(f"{DIM}Fetching latest versions from PyPI...{RESET}\n")
    for dep in deps:
        dep["latest"] = fetch_latest_version(dep["name"]) or ""
        dep["installed"] = installed.get(dep["name"].lower(), "")

    # find deps that need updating
    to_update = [d for d in deps if d["latest"] and is_outdated(d["version"], d["latest"])]

    if not to_update:
        print(f"  {GREEN}All pinned versions are already up to date!{RESET}\n")

        # sync requirements.txt from pyproject if both exist
        if has_pyproject and has_requirements:
            pyproject = load_pyproject()
            expected = generate_requirements(pyproject)
            if REQUIREMENTS_PATH.read_text() != expected:
                REQUIREMENTS_PATH.write_text(expected)
                print("  Synced requirements.txt\n")

        return

    # update files
    print(f"  {BOLD}Updating pinned versions:{RESET}\n")
    name_w = max(len(d["name"] + d["extras"]) for d in to_update) + 2

    if has_pyproject:
        content = PYPROJECT_PATH.read_text()
        for dep in to_update:
            name_display = dep["name"] + dep["extras"]
            old_v = dep["operator"] + dep["version"] if dep["version"] else "any"
            new_v = dep["operator"] + dep["latest"] if dep["operator"] else ">=" + dep["latest"]
            content = update_pyproject_content(content, dep, dep["latest"])
            print(f"  {name_display:<{name_w}} {RED}{old_v}{RESET} -> {GREEN}{new_v}{RESET}")
        PYPROJECT_PATH.write_text(content)
        print(f"\n  Updated {BOLD}pyproject.toml{RESET}")

        # regenerate requirements.txt if it exists
        if has_requirements:
            updated_pyproject = tomllib.loads(content)
            req_content = generate_requirements(updated_pyproject)
            REQUIREMENTS_PATH.write_text(req_content)
            print(f"  Updated {BOLD}requirements.txt{RESET}")
    else:
        # requirements.txt only
        content = REQUIREMENTS_PATH.read_text()
        for dep in to_update:
            name_display = dep["name"] + dep["extras"]
            old_v = dep["operator"] + dep["version"] if dep["version"] else "any"
            new_v = dep["operator"] + dep["latest"] if dep["operator"] else ">=" + dep["latest"]
            content = update_requirements_content(content, dep, dep["latest"])
            print(f"  {name_display:<{name_w}} {RED}{old_v}{RESET} -> {GREEN}{new_v}{RESET}")
        REQUIREMENTS_PATH.write_text(content)
        print(f"\n  Updated {BOLD}requirements.txt{RESET}")

    # run sync with appropriate tool
    if has_pyproject:
        print(f"\n{DIM}Running uv sync --all-extras...{RESET}\n")
        code, _, stderr = run_command("uv sync --all-extras", capture=False)
    else:
        print(f"\n{DIM}Running pip install -r requirements.txt...{RESET}\n")
        code, _, stderr = run_command("pip install -r requirements.txt", capture=False)

    if code != 0:
        print(f"\n  {RED}Sync failed: {stderr}{RESET}")
        sys.exit(1)

    # re-check after sync
    print(f"\n{DIM}Verifying update...{RESET}\n")
    new_installed = get_installed_versions()

    all_good = True
    for dep in to_update:
        name_display = dep["name"] + dep["extras"]
        new_inst = new_installed.get(dep["name"].lower(), "?")
        if is_outdated(new_inst, dep["latest"]):
            print(f"  {RED}!{RESET} {name_display}: installed {new_inst}, expected {dep['latest']}")
            all_good = False
        else:
            print(f"  {GREEN}+{RESET} {name_display}: {new_inst}")

    print()
    if all_good:
        print(f"  {GREEN}Upgrade complete! All packages updated.{RESET}\n")
    else:
        print(f"  {YELLOW}Some packages may not have updated to the exact latest version.{RESET}")
        print(f"  {DIM}This can happen with platform-specific builds.{RESET}\n")


def main():
    usage = "usage: python scripts/pcu.py [check|upgrade]"

    if len(sys.argv) < 2:
        print(usage)
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        cmd_check()
    elif command == "upgrade":
        cmd_upgrade()
    else:
        print(f"Unknown command: {command}")
        print(usage)
        sys.exit(1)


if __name__ == "__main__":
    main()
