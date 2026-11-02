#!/usr/bin/env python3
"""Regenerate the caraok-managed nginx server block (idempotent, line-based).

The managed block sits inside http{} right before its closing brace, wrapped
in BEGIN/END markers. real_ip only trusts Cloudflare ranges and the proxy
overwrites CF-Connecting-IP, so spoofed headers never reach the app (#6).
"""

import argparse

BLOCK = """# BEGIN caraokManaged
server {
    listen 80;
    server_name {domain};

    set_real_ip_from 173.245.48.0/20;
    set_real_ip_from 103.21.244.0/22;
    set_real_ip_from 103.22.200.0/22;
    set_real_ip_from 103.31.4.0/22;
    set_real_ip_from 141.101.64.0/18;
    set_real_ip_from 108.162.192.0/18;
    set_real_ip_from 190.93.240.0/20;
    set_real_ip_from 188.114.96.0/20;
    set_real_ip_from 197.234.240.0/22;
    set_real_ip_from 198.41.128.0/17;
    set_real_ip_from 162.158.0.0/15;
    set_real_ip_from 104.16.0.0/13;
    set_real_ip_from 104.24.0.0/14;
    set_real_ip_from 172.64.0.0/13;
    set_real_ip_from 131.0.72.0/22;
    real_ip_header CF-Connecting-IP;
    real_ip_recursive on;

    access_log /var/log/nginx/access-caraok.log upstreamlog;

    location / {
        proxy_pass {upstream};
        proxy_set_header Host $host;
        # 覆写而非透传:应用侧拿到的恒为 nginx 判定的真实客户端
        proxy_set_header CF-Connecting-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
# END caraokManaged
"""


def is_managed_marker(line: str) -> bool:
    return line.startswith("# BEGIN caraokManaged") or line.startswith("# END caraokManaged")


def block_depth(lines: list[str], start: int) -> int:
    """Return the index (exclusive) where the block starting at `start` closes."""
    depth = 0
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth == 0:
            return i + 1
    raise SystemExit(f"unbalanced braces: block at line {start + 1} never closes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conf", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--upstream", default="http://172.17.0.1:8091")
    args = parser.parse_args()

    lines = open(args.conf, encoding="utf-8").read().splitlines()
    out: list[str] = []
    i = 0
    legacy_removed = managed_removed = 0
    while i < len(lines):
        line = lines[i]
        if is_managed_marker(line):
            # 托管块:跳过 BEGIN..END(含其间的 server 块)
            j = i + 1
            while j < len(lines) and not lines[j].startswith("# END caraokManaged"):
                j += 1
            i = j + 1
            managed_removed += 1
            continue
        if line.startswith("server {") and i + 2 < len(lines) and args.domain in lines[i + 2]:
            # 旧式无标记 caraok 块:整块按括号深度跳过
            i = block_depth(lines, i)
            legacy_removed += 1
            continue
        out.append(line)
        i += 1

    # 插入点:http{} 的闭合括号(文件中最后一个只含 "}" 的行)
    insert_at = max(idx for idx, l in enumerate(out) if l.strip() == "}")
    block = BLOCK.replace("{domain}", args.domain).replace("{upstream}", args.upstream).splitlines()
    out = out[:insert_at] + block + out[insert_at:]

    # 校验:全文花括号必须平衡
    depth = 0
    for l in out:
        depth += l.count("{") - l.count("}")
    if depth != 0:
        raise SystemExit(f"brace imbalance after regeneration: {depth}")

    open(args.conf, "w", encoding="utf-8").write("\n".join(out) + "\n")
    print(f"vhost regenerated for {args.domain} (legacy removed: {legacy_removed}, managed refreshed: {managed_removed})")


main()
