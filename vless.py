# -*- coding: utf-8 -*-
# vless:// → конфигурация Xray (TUN на Linux).
# Порт логики VlessConfig.kt + VlessXrayConfig.kt из мобильного Nova
# (поля сверены с вендоренным форком tools/xray-core, версия v26.7.28).

import ipaddress
import json
import re
import urllib.parse

KNOWN_PARAMS = {
    "type", "security", "encryption", "sni", "peer", "fp", "alpn", "allowinsecure",
    "pbk", "sid", "spx", "path", "host", "servicename", "mode", "headertype", "flow",
    "seed", "quicsecurity", "key",
}
TLS12_ONLY_FINGERPRINTS = {"android", "qq", "360"}
BASE64URL_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _decode(value):
    return urllib.parse.unquote(value)


def _parse_query(query):
    params = {}
    for pair in query.split("&"):
        if not pair:
            continue
        if "=" in pair:
            key, value = pair.split("=", 1)
            params[key.lower()] = _decode(value)
        else:
            params[pair.lower()] = ""
    return params


def _normalize_network(value):
    value = value.strip().lower()
    if value in ("", "tcp", "raw"):
        return "tcp"
    if value in ("xhttp", "splithttp"):
        return "xhttp"
    if value in ("ws", "websocket"):
        return "ws"
    if value in ("kcp", "mkcp"):
        return "kcp"
    if value == "httpupgrade":
        return "httpupgrade"
    if value in ("h2", "http"):
        return "http"
    return value


def _split_host_port(value):
    if value.startswith("["):
        close = value.index("]")
        host = value[1:close]
        rest = value[close + 1:]
        if not rest.startswith(":"):
            return None
        try:
            port = int(rest[1:])
        except ValueError:
            return None
        return host, port
    colon = value.rfind(":")
    if colon <= 0:
        return None
    try:
        port = int(value[colon + 1:])
    except ValueError:
        return None
    return value[:colon], port


def _acceptable_user_id(value):
    length = len(value)
    if 32 <= length <= 36:
        return bool(UUID_RE.match(value) or HEX32_RE.match(value))
    return 1 <= length <= 30


def parse(raw):
    trimmed = raw.strip().replace(" ", "%20").replace("|", "%7C")
    if not trimmed.lower().startswith("vless://"):
        return None

    without_scheme = trimmed[len("vless://"):]
    if "#" in without_scheme:
        before_fragment, remark = without_scheme.split("#", 1)
        remark = _decode(remark)
    else:
        before_fragment, remark = without_scheme, ""

    if "?" in before_fragment:
        authority, query = before_fragment.split("?", 1)
    else:
        authority, query = before_fragment, ""

    at_index = authority.rfind("@")
    if at_index <= 0:
        return None
    uuid = _decode(authority[:at_index]).strip()
    if not uuid or not _acceptable_user_id(uuid):
        return None

    host_port = authority[at_index + 1:].strip().split("/", 1)[0]
    split = _split_host_port(host_port)
    if not split:
        return None
    host, port = split
    if not host or not (1 <= port <= 65535):
        return None

    params = _parse_query(query)

    def param(*names):
        for name in names:
            value = params.get(name)
            if value is not None and value != "":
                return value
        return ""

    security = (param("security") or "none").lower()
    network = _normalize_network(param("type"))
    alpn = [item.strip() for item in (param("alpn") or "").split(",") if item.strip()]
    allow_insecure = param("allowinsecure").lower() in ("1", "true")

    config = {
        "uuid": uuid,
        "host": host,
        "port": port,
        "remark": remark,
        "security": security,
        "sni": param("sni", "peer"),
        "alpn": alpn,
        "fingerprint": param("fp"),
        "allowInsecure": allow_insecure,
        "realityPublicKey": param("pbk"),
        "realityShortId": param("sid"),
        "realitySpiderX": param("spx"),
        "network": network,
        "path": param("path"),
        "hostHeader": param("host"),
        "serviceName": param("servicename"),
        "mode": param("mode"),
        "headerType": param("headertype"),
        "flow": param("flow"),
        "encryption": (param("encryption") or "none"),
        "extraParams": {k: v for k, v in params.items() if k not in KNOWN_PARAMS},
    }
    error = validate(config)
    if error:
        return None
    return config


def validate(config):
    if config["security"] == "reality":
        if not config["realityPublicKey"]:
            return "REALITY без публичного ключа (pbk)"
        if not config["sni"]:
            return "REALITY без SNI"
        if config["realityShortId"] and not re.fullmatch(r"[0-9a-fA-F]{2,16}", config["realityShortId"]):
            return "shortId (sid) должен быть hex чётной длины до 16 символов"
        if _base64url_length(config["realityPublicKey"]) != 32:
            return "публичный ключ (pbk) должен быть 32-байтным X25519 в base64url"
        if config["flow"] and config["network"] != "tcp":
            return "flow=%s работает только с type=tcp" % config["flow"]
    if config["encryption"] not in ("", "none") and not config["encryption"].lower().startswith("mlkem768"):
        return "неизвестное значение encryption=%s" % config["encryption"]
    return None


def _base64url_length(value):
    symbols = value.strip().rstrip("=")
    if not symbols or any(c not in BASE64URL_ALPHABET and c not in "+/" for c in symbols):
        return -1
    return len(symbols) * 6 // 8


def effective_fingerprint(config):
    if config["security"] != "reality":
        return config["fingerprint"]
    if config["fingerprint"].lower() in TLS12_ONLY_FINGERPRINTS or not config["fingerprint"]:
        return "chrome"
    return config["fingerprint"]


def display_name(config):
    return config["remark"] or "%s:%d" % (config["host"], config["port"])


def build_xray_config(config, tun_name="sarpik0", site_policy=None):
    """site_policy: None — весь трафик через прокси;
       ("only", [...]) — через прокси только перечисленные сайты;
       ("except", [...]) — всё через прокси, кроме перечисленных сайтов.
       Элементы списка: домен, "domain:домен", "full:домен", IP или CIDR."""
    def normalize_domain(value):
        value = value.strip().lower()
        if value.startswith("domain:"):
            return ("suffix", value[len("domain:"):].lstrip("*."))
        if value.startswith("full:"):
            return ("full", value[len("full:"):])
        value = value.lstrip("*.")
        return ("suffix", value)

    def classify_sites(lines):
        suffixes, fulls, ips = [], [], []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("regexp:"):
                continue
            if "/" in line:
                try:
                    ipaddress.ip_network(line, strict=False)
                except ValueError:
                    continue
                ips.append(line)
                continue
            try:
                ipaddress.ip_address(line)
            except ValueError:
                kind, value = normalize_domain(line)
                if not value:
                    continue
                (fulls if kind == "full" else suffixes).append(value)
            else:
                ips.append(line)
        return suffixes, fulls, ips

    def site_rules(suffixes, fulls, ips, via):
        rules = []
        if suffixes:
            rules.append({"type": "field", "domain": suffixes, "outboundTag": via})
        if fulls:
            rules.append({"type": "field", "domain": [{"domain": value, "type": "full"} for value in fulls], "outboundTag": via})
        if ips:
            rules.append({"type": "field", "ip": ips, "outboundTag": via})
        return rules

    def stream_settings():
        network = config["network"]
        stream = {"network": "raw" if network == "tcp" else network}
        security = config["security"]
        if security == "reality":
            reality = {
                "serverName": config["sni"],
                "publicKey": config["realityPublicKey"],
                "fingerprint": effective_fingerprint(config),
            }
            if config["realityShortId"]:
                reality["shortId"] = config["realityShortId"]
            if config["realitySpiderX"]:
                reality["spiderX"] = config["realitySpiderX"]
            if "mldsa65verify" in config["extraParams"]:
                reality["mldsa65Verify"] = config["extraParams"]["mldsa65verify"]
            stream["security"] = "reality"
            stream["realitySettings"] = reality
        elif security == "tls":
            server_name = config["sni"] or config["hostHeader"] or config["host"]
            tls = {"serverName": server_name}
            if config["allowInsecure"]:
                tls["verifyPeerCertByName"] = server_name
            if config["fingerprint"]:
                tls["fingerprint"] = config["fingerprint"]
            if config["alpn"]:
                tls["alpn"] = config["alpn"]
            stream["security"] = "tls"
            stream["tlsSettings"] = tls
        else:
            stream["security"] = "none"

        network_key = stream["network"]
        if network_key == "raw":
            if config["headerType"] and config["headerType"] != "none":
                stream["rawSettings"] = {"header": {"type": config["headerType"]}}
        elif network_key == "ws":
            ws = {"path": config["path"] or "/"}
            if config["hostHeader"]:
                ws["host"] = config["hostHeader"]
            stream["wsSettings"] = ws
        elif network_key == "httpupgrade":
            upgrade = {"path": config["path"] or "/"}
            if config["hostHeader"]:
                upgrade["host"] = config["hostHeader"]
            stream["httpupgradeSettings"] = upgrade
        elif network_key == "grpc":
            grpc = {"serviceName": config["serviceName"]}
            if config["mode"].lower() == "multi":
                grpc["multiMode"] = True
            if config["hostHeader"]:
                grpc["authority"] = config["hostHeader"]
            stream["grpcSettings"] = grpc
        elif network_key == "xhttp":
            xhttp = {"path": config["path"] or "/", "mode": config["mode"] or "auto"}
            if config["hostHeader"]:
                xhttp["host"] = config["hostHeader"]
            extra = config["extraParams"].get("extra")
            if extra and extra != "null":
                try:
                    for key, value in json.loads(extra).items():
                        xhttp[key] = value
                except ValueError:
                    pass
            stream["xhttpSettings"] = xhttp
        elif network_key == "kcp":
            kcp = {}
            if config["headerType"]:
                kcp["header"] = {"type": config["headerType"]}
            if "seed" in config["extraParams"]:
                kcp["seed"] = config["extraParams"]["seed"]
            stream["kcpSettings"] = kcp
        return stream

    user = {"id": config["uuid"], "encryption": config["encryption"] or "none", "level": 0}
    if config["flow"]:
        user["flow"] = config["flow"]
    outbound = {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {"vnext": [{"address": config["host"], "port": config["port"], "users": [user]}]},
        "streamSettings": stream_settings(),
    }
    rules = [
        {"type": "field", "inboundTag": ["tun-in"], "port": 53, "outboundTag": "proxy"},
    ]
    if site_policy is None:
        rules.append({"type": "field", "inboundTag": ["tun-in"], "outboundTag": "proxy"})
    else:
        mode, sites = site_policy
        suffixes, fulls, ips = classify_sites(sites)
        if mode == "only":
            rules.extend(site_rules(suffixes, fulls, ips, "proxy"))
            rules.append({"type": "field", "inboundTag": ["tun-in"], "network": "tcp,udp", "outboundTag": "direct"})
        elif mode == "except":
            rules.extend(site_rules(suffixes, fulls, ips, "direct"))
            rules.append({"type": "field", "inboundTag": ["tun-in"], "network": "tcp,udp", "outboundTag": "proxy"})

    outbounds = [outbound]
    if site_policy is not None:
        outbounds.append({"tag": "direct", "protocol": "freedom"})

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "tun-in",
            "protocol": "tun",
            "settings": {
                "name": tun_name,
                "mtu": 1500,
                "gateway": ["10.8.0.1/30"],
                "autoSystemRoutingTable": ["0.0.0.0/0", "::/0"],
                "autoOutboundsInterface": "auto",
            },
        }],
        "outbounds": outbounds,
        "routing": {"rules": rules},
    }