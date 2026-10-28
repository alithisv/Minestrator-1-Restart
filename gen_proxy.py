import sys
import json
import urllib.parse

def parse_node(url_str):
    u = urllib.parse.urlsplit(url_str.strip())
    scheme = u.scheme.lower()
    host = u.hostname
    port = u.port
    params = dict(urllib.parse.parse_qsl(u.query))

    if scheme in ("hy2", "hysteria2"):
        outbound = {
            "type": "hysteria2",
            "tag": "proxy",
            "server": host,
            "server_port": port,
            "password": u.username or u.password or "",
            "tls": {
                "enabled": True,
                "server_name": params.get("sni", host),
                "insecure": params.get("insecure", "0") in ("1", "true")
            }
        }
    elif scheme == "vless":
        outbound = {
            "type": "vless",
            "tag": "proxy",
            "server": host,
            "server_port": port,
            "uuid": u.username,
            "flow": params.get("flow", "")
        }
        # TLS 配置
        security = params.get("security", "")
        if security in ("tls", "reality"):
            tls_cfg = {
                "enabled": True,
                "server_name": params.get("sni", host),
                "insecure": params.get("allowInsecure", "0") in ("1", "true")
            }
            if security == "reality":
                tls_cfg["reality"] = {
                    "enabled": True,
                    "public_key": params.get("pbk", ""),
                    "short_id": params.get("sid", "")
                }
            outbound["tls"] = tls_cfg
            
        # 传输层配置
        net_type = params.get("type", "tcp")
        if net_type == "ws":
            outbound["transport"] = {
                "type": "ws",
                "path": params.get("path", "/"),
                "headers": {"Host": params.get("host", host)}
            }
        elif net_type == "grpc":
            outbound["transport"] = {
                "type": "grpc",
                "service_name": params.get("serviceName", "")
            }
    else:
        raise ValueError(f"暂不支持的协议格式: {scheme}")

    # 生成 sing-box 完整配置：监听本地 8080 端口作为 HTTP 代理
    config = {
        "log": {"level": "info"},
        "inbounds": [
            {
                "type": "http",
                "tag": "http-in",
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
    if len(sys.argv) < 2:
        sys.exit(1)
    cfg = parse_node(sys.argv[1])
    with open("sing-box-config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
