import sys
import json
import urllib.parse

def parse_node(url_str):
    url_str = url_str.strip()
    u = urllib.parse.urlsplit(url_str)
    scheme = u.scheme.lower()
    host = u.hostname
    port = int(u.port)
    params = dict(urllib.parse.parse_qsl(u.query))

    if scheme in ("hy2", "hysteria2"):
        # URL 解码防止密码中含有特殊符号
        auth = urllib.parse.unquote(u.username or "")
        if u.password:
            auth += ":" + urllib.parse.unquote(u.password)

        outbound = {
            "type": "hysteria2",
            "tag": "proxy",
            "server": host,
            "server_port": port,
            "password": auth,
            "tls": {
                "enabled": True,
                "server_name": params.get("sni", params.get("peer", host)),
                "insecure": params.get("insecure", "0") in ("1", "true")
            }
        }
        if "obfs" in params and params["obfs"] == "salamander":
            outbound["obfs"] = {
                "type": "salamander",
                "password": urllib.parse.unquote(params.get("obfs-password", ""))
            }

    elif scheme == "vless":
        outbound = {
            "type": "vless",
            "tag": "proxy",
            "server": host,
            "server_port": port,
            "uuid": urllib.parse.unquote(u.username or ""),
        }
        if params.get("flow"):
            outbound["flow"] = params["flow"]

        security = params.get("security", "")
        if security in ("tls", "reality"):
            tls_cfg = {
                "enabled": True,
                "server_name": params.get("sni", host),
                "insecure": params.get("allowInsecure", "0") in ("1", "true"),
                "utls": {
                    "enabled": True,
                    "fingerprint": params.get("fp", "chrome") or "chrome"
                }
            }
            if security == "reality":
                tls_cfg["reality"] = {
                    "enabled": True,
                    "public_key": params.get("pbk", ""),
                    "short_id": params.get("sid", "")
                }
            outbound["tls"] = tls_cfg

        net_type = params.get("type", "tcp")
        if net_type == "ws":
            outbound["transport"] = {
                "type": "ws",
                "path": urllib.parse.unquote(params.get("path", "/")),
                "headers": {"Host": params.get("host", params.get("sni", host))}
            }
        elif net_type == "grpc":
            outbound["transport"] = {
                "type": "grpc",
                "service_name": urllib.parse.unquote(params.get("serviceName", ""))
            }
    else:
        raise ValueError(f"不支持的节点协议: {scheme}")

    config = {
        "log": {"level": "info"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": 8080
            }
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"}
        ]
    }
    return config

if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("⚠️ 未提供节点链接")
        sys.exit(1)
    cfg = parse_node(sys.argv[1])
    with open("sing-box-config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
