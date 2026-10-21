"""Read the deployment's private DNS name and native proxy aliases."""

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import yaml


def http_url(host, port):
    """Format a host field as an HTTP origin, including IPv6 literals."""
    host = host.strip("[]")
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def proxy_services(path=None):
    """Use the host's native Traefik routes as the owner of service aliases."""
    configured = path or os.environ.get("AI_PROXY_ROUTES")
    if not configured:
        return {}
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("AI_PROXY_ROUTES must be an absolute path.")
    if not path.exists():
        return {}
    http = yaml.safe_load(path.read_text())["http"]
    result = {}
    for name, route in http["routers"].items():
        hostname = re.match(r"Host\(`([a-z0-9.-]+)`\)", route["rule"])
        if hostname is None or "tls" not in route:
            continue
        servers = http["services"][route["service"]]["loadBalancer"]["servers"]
        if len(servers) != 1:
            continue
        backend = urlparse(servers[0]["url"])
        if backend.hostname in {"127.0.0.1", "localhost"}:
            prefix = re.search(r"PathPrefix\(`(/[a-z0-9/-]+)`\)", route["rule"])
            suffix = prefix.group(1) if prefix else "/"
            result[name] = {
                "port": backend.port,
                "url": "https://" + hostname.group(1) + suffix,
            }
    return result


def proxy_endpoints(path=None):
    result = {}
    for service in proxy_services(path).values():
        result.setdefault(service["port"], service["url"])
    return result


def endpoints():
    """Read the private DNS name and the native proxy's HTTPS aliases without parsing Compose."""
    dns = os.environ.get("AI_DNS") or os.environ.get("AI_HOST")
    if not dns:
        status = json.loads(
            subprocess.run(
                ["tailscale", "status", "--json"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        dns = status["Self"]["DNSName"].rstrip(".")
    return dns, proxy_endpoints()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, help="Print the hostname for a configured HTTPS backend"
    )
    arguments = parser.parse_args()
    dns, secure = endpoints()
    print(
        urlparse(secure[arguments.port]).hostname if arguments.port in secure else dns
    )
