# -*- coding: utf-8 -*-
# Разделение трафика: какие сайты идут через VPN, а какие напрямую.
# Режимы: all (весь трафик), only (только перечисленные), except (все, кроме перечисленных).

import ipaddress
import json
import os
import socket

SITES_CONF = os.path.expanduser("~/.config/sarpik/sites.json")

MODES = ("all", "only", "except")


def default():
    return {"mode": "all", "sites": []}


def load():
    data = default()
    try:
        with open(SITES_CONF, encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("mode") in MODES:
            data["mode"] = raw["mode"]
        sites = [line.strip() for line in raw.get("sites", [])]
        data["sites"] = [line for line in sites if line and not line.startswith("#")]
    except (OSError, ValueError):
        pass
    return data


def save(mode, sites):
    with open(SITES_CONF, "w", encoding="utf-8") as fh:
        json.dump({"mode": mode, "sites": sites}, fh, ensure_ascii=False, indent=1)


def policy():
    """None — весь трафик через VPN; иначе ("only"|"except", [строки])."""
    data = load()
    if data["mode"] == "all" or not data["sites"]:
        return None
    return (data["mode"], data["sites"])


def is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def is_network(value):
    try:
        ipaddress.ip_network(value, strict=False)
        return True
    except ValueError:
        return False


def resolve_sites(sites, timeout=3):
    """Домены → список IP (A и AAAA), IP/CIDR — как есть."""
    resolved = []
    seen = set()
    for line in sites:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if is_ip(line) or is_network(line):
            if line not in seen:
                seen.add(line)
                resolved.append(line)
            continue
        try:
            infos = socket.getaddrinfo(line, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except OSError:
            continue
        for info in infos:
            ip = info[4][0]
            if ip not in seen:
                seen.add(ip)
                resolved.append(ip)
    return resolved


def awg_allowed_ips(conf_path, sites):
    """Список AllowedIPs для режима «только эти сайты»: IP сайтов + DNS-серверы конфига."""
    dns_ips = []
    with open(conf_path, encoding="utf-8") as fh:
        for line in fh:
            key, sep, value = line.partition("=")
            if sep and key.strip().upper() == "DNS":
                for entry in value.split(","):
                    entry = entry.strip().split("/")[0]
                    if entry and entry not in dns_ips:
                        dns_ips.append(entry)
    allowed = list(dns_ips)
    for ip in ("1.1.1.1", "1.0.0.1", "2606:4700:4700::1111", "2606:4700:4700::1001"):
        if ip not in allowed:
            allowed.append(ip)
    allowed.extend(resolve_sites(sites))
    return allowed
