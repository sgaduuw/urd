import pathlib
import re

import urd

ROOT = pathlib.Path(__file__).parent


def _read(name):
    return (ROOT / name).read_text()


def test_the_image_bakes_in_no_configuration():
    """A working example is exactly where a real site, key or token gets
    hardcoded, and the image is the one artefact that travels."""
    text = _read("Dockerfile") + _read("compose.yaml")
    for pattern in (r"ATATT", r"\bURD_TOKEN\s*[:=]\s*\S", r"atlassian\.net"):
        assert not re.search(pattern, text), f"{pattern} is baked into the image"


def test_the_token_is_passed_through_rather_than_defined():
    """Compose should take it from the host environment, so it never sits in a
    file that gets committed."""
    compose = _read("compose.yaml")
    assert "URD_TOKEN" in compose
    # No re.MULTILINE: `$` here only ever matches at the very end of the whole
    # file, not the end of the URD_TOKEN line, so a bare `URD_TOKEN\s*$`
    # alternative would be dead code that happens to pass on the second
    # alternative instead. Keep only the alternatives that can actually match.
    assert re.search(r"URD_TOKEN\s*\n|\$\{URD_TOKEN", compose), compose


def test_the_compose_file_passes_through_every_seed_key():
    """A key seed_from_env reads but compose does not list can never be set
    from the environment: this is exactly how a container once shipped a
    project that could sync but never derive, since derive refuses without
    status_order."""
    compose = _read("compose.yaml")
    for key in urd.SEED_ENV_KEYS:
        assert key in compose, key


def test_the_database_is_a_volume():
    """Without it a restart loses the sync, which is the only state there is."""
    compose = _read("compose.yaml")
    assert "volumes:" in compose
    assert "/var/lib/urd" in compose


def test_the_published_port_is_loopback_by_default():
    """Exposing an unauthenticated report to a network has to be a deliberate
    edit, not the default."""
    compose = _read("compose.yaml")
    ports = re.findall(r'"?(\d+\.\d+\.\d+\.\d+:)?(\d+):(\d+)"?', compose)
    assert ports, compose
    assert any(p[0] == "127.0.0.1:" for p in ports), f"not bound to loopback: {ports}"


def test_the_image_pins_a_python_that_can_parse_the_timestamps():
    """_ts relies on 3.11's fromisoformat parsing both the +0000 and Z shapes."""
    match = re.search(r"FROM python:(\d+)\.(\d+)", _read("Dockerfile"))
    assert match, "no pinned python base image"
    assert (int(match.group(1)), int(match.group(2))) >= (3, 11), match.group(0)


def test_the_image_installs_only_the_two_dependencies():
    text = _read("Dockerfile")
    installed = re.findall(r"pip install[^\n]*", text)
    assert installed, text
    joined = " ".join(installed)
    assert "duckdb" in joined and "flask" in joined
    for extra in ("fastapi", "uvicorn", "gunicorn", "pandas", "requests"):
        assert extra not in joined, f"{extra} is not a dependency of this project"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
