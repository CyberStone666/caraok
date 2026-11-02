#!/bin/sh
# caraok(#5):源站 80 端口仅允许 Cloudflare 网段,阻断绕过 CF 的直连。
# 幂等;开机由 caraok-firewall.service 调用本脚本恢复。
set -e

command -v ipset >/dev/null 2>&1 || apt-get install -y -qq ipset >/dev/null

ipset create caraok_cf hash:net family inet -exist
ipset create caraok_private hash:net family inet -exist
ipset add caraok_private 10.0.0.0/8 -exist
ipset add caraok_private 172.16.0.0/12 -exist
ipset add caraok_private 192.168.0.0/16 -exist
for R in \
  173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 \
  141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 \
  197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 \
  104.24.0.0/14 172.64.0.0/13 131.0.72.0/22; do
  ipset add caraok_cf "$R" -exist
done

# 宿主机本地进程路径(INPUT);loopback 放行(127.0.0.1 自检用)
iptables -C INPUT -p tcp --dport 80 ! -i lo -m set ! --match-set caraok_cf src -j DROP 2>/dev/null || \
  iptables -I INPUT 1 -p tcp --dport 80 ! -i lo -m set ! --match-set caraok_cf src -j DROP
# docker 发布端口路径(FORWARD):丢弃非 CF 且非私有源的流量;
# 私有源豁免是关键——docker-proxy 自己的上游连接源地址是宿主网桥 IP
iptables -C DOCKER-USER -p tcp --dport 80 -m set ! --match-set caraok_cf src -m set ! --match-set caraok_private src -j DROP 2>/dev/null || \
  iptables -I DOCKER-USER 1 -p tcp --dport 80 -m set ! --match-set caraok_cf src -m set ! --match-set caraok_private src -j DROP

echo "caraok firewall: port 80 restricted to Cloudflare ranges"
