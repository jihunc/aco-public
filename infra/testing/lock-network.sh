#!/bin/sh
# Trusted container bootstrap only. Apply before Python/Node/plugins/browser startup.
set -eu
: "${ACO_LOCAL_PG_IP:?}" "${ACO_LOCAL_OIDC_IP:?}" "${ACO_LOCAL_S3_IP:?}"
# Reject embedded resolver BEFORE Docker's DNS DNAT, regardless of listener port.
iptables -t raw -A OUTPUT -d 127.0.0.11 -j DROP
iptables -P OUTPUT DROP
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -p tcp -d 127.0.0.1 -m multiport --dports 53000,58000 -j ACCEPT
iptables -A OUTPUT -p tcp -d "$ACO_LOCAL_PG_IP" --dport 5432 -j ACCEPT
iptables -A OUTPUT -p tcp -d "$ACO_LOCAL_OIDC_IP" --dport 8080 -j ACCEPT
iptables -A OUTPUT -p tcp -d "$ACO_LOCAL_S3_IP" --dport 8333 -j ACCEPT
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited
ip6tables -P OUTPUT DROP
ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A OUTPUT -p tcp -d ::1 -m multiport --dports 53000,58000 -j ACCEPT
ip6tables -A OUTPUT -j REJECT --reject-with icmp6-adm-prohibited
# Remove every capability (including NET_ADMIN, NET_RAW, SETUID/SETPCAP), set no-new-privs.
# Child processes cannot reset firewall, obtain raw sockets, setns or delegate to Docker.
exec setpriv --reuid=10001 --regid=10001 --clear-groups --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs "$@"
