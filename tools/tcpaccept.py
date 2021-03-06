#!/usr/bin/python
# @lint-avoid-python-3-compatibility-imports
#
# tcpaccept Trace TCP accept()s.
#           For Linux, uses BCC, eBPF. Embedded C.
#
# USAGE: tcpaccept [-h] [-T] [-t] [-p PID] [-P PORTS]
#
# This uses dynamic tracing of the kernel inet_csk_accept() socket function
# (from tcp_prot.accept), and will need to be modified to match kernel changes.
#
# Copyright (c) 2015 Brendan Gregg.
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 13-Oct-2015   Brendan Gregg   Created this.
# 14-Feb-2016      "      "     Switch to bpf_perf_output.

from __future__ import print_function
from bcc import BPF
from socket import inet_ntop, AF_INET, AF_INET6
from struct import pack
import argparse
from bcc.utils import printb
from time import strftime

# arguments
examples = """examples:
    ./tcpaccept           # trace all TCP accept()s
    ./tcpaccept -t        # include timestamps
    ./tcpaccept -P 80,81  # only trace port 80 and 81
    ./tcpaccept -p 181    # only trace PID 181
"""
parser = argparse.ArgumentParser(
    description="Trace TCP accepts",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-T", "--time", action="store_true",
    help="include time column on output (HH:MM:SS)")
parser.add_argument("-t", "--timestamp", action="store_true",
    help="include timestamp on output")
parser.add_argument("-p", "--pid",
    help="trace this PID only")
parser.add_argument("-P", "--port",
    help="comma-separated list of local ports to trace")
parser.add_argument("--ebpf", action="store_true",
    help=argparse.SUPPRESS)
args = parser.parse_args()
debug = 0

# define BPF program
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <net/sock.h>
#include <bcc/proto.h>

// separate data structs for ipv4 and ipv6
struct ipv4_data_t {
    u64 ts_us;
    u32 pid;
    u32 saddr;
    u32 daddr;
    u64 ip;
    u16 lport;
    u16 dport;
    char task[TASK_COMM_LEN];
};
BPF_PERF_OUTPUT(ipv4_events);

struct ipv6_data_t {
    u64 ts_us;
    u32 pid;
    unsigned __int128 saddr;
    unsigned __int128 daddr;
    u64 ip;
    u16 lport;
    u16 dport;
    char task[TASK_COMM_LEN];
};
BPF_PERF_OUTPUT(ipv6_events);
"""

#
# The following is the code for older kernels(Linux pre-4.16).
# It uses kprobes to instrument inet_csk_accept(). On Linux 4.16 and
# later, the sock:inet_sock_set_state tracepoint should be used instead, as
# is done by the code that follows this. 
#
bpf_text_kprobe = """
int kretprobe__inet_csk_accept(struct pt_regs *ctx)
{
    struct sock *newsk = (struct sock *)PT_REGS_RC(ctx);
    u32 pid = bpf_get_current_pid_tgid();

    ##FILTER_PID##

    if (newsk == NULL)
        return 0;

    // check this is TCP
    u8 protocol = 0;
    // workaround for reading the sk_protocol bitfield:
    
    // Following comments add by Joe Yin:
    // Unfortunately,it can not work since Linux 4.10,
    // because the sk_wmem_queued is not following the bitfield of sk_protocol.
    // And the following member is sk_gso_max_segs.
    // So, we can use this:
    // bpf_probe_read(&protocol, 1, (void *)((u64)&newsk->sk_gso_max_segs) - 3);
    // In order to  diff the pre-4.10 and 4.10+ ,introduce the variables gso_max_segs_offset,sk_lingertime, 
    // sk_lingertime is closed to the gso_max_segs_offset,and  
    // the offset between the two members is 4 

    int gso_max_segs_offset = offsetof(struct sock, sk_gso_max_segs);
    int sk_lingertime_offset = offsetof(struct sock, sk_lingertime);

    if (sk_lingertime_offset - gso_max_segs_offset == 4) 
        // 4.10+ with little endian
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
        protocol = *(u8 *)((u64)&newsk->sk_gso_max_segs - 3);
    else
        // pre-4.10 with little endian
        protocol = *(u8 *)((u64)&newsk->sk_wmem_queued - 3);
#elif __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
        // 4.10+ with big endian
        protocol = *(u8 *)((u64)&newsk->sk_gso_max_segs - 1);
    else
        // pre-4.10 with big endian
        protocol = *(u8 *)((u64)&newsk->sk_wmem_queued - 1);
#else
# error "Fix your compiler's __BYTE_ORDER__?!"
#endif

    if (protocol != IPPROTO_TCP)
        return 0;

    // pull in details
    u16 family = 0, lport = 0, dport;
    family = newsk->__sk_common.skc_family;
    lport = newsk->__sk_common.skc_num;
    dport = newsk->__sk_common.skc_dport;
    dport = ntohs(dport);

    ##FILTER_PORT##

    if (family == AF_INET) {
        struct ipv4_data_t data4 = {.pid = pid, .ip = 4};
        data4.ts_us = bpf_ktime_get_ns() / 1000;
        data4.saddr = newsk->__sk_common.skc_rcv_saddr;
        data4.daddr = newsk->__sk_common.skc_daddr;
        data4.lport = lport;
        data4.dport = dport;
        bpf_get_current_comm(&data4.task, sizeof(data4.task));
        ipv4_events.perf_submit(ctx, &data4, sizeof(data4));

    } else if (family == AF_INET6) {
        struct ipv6_data_t data6 = {.pid = pid, .ip = 6};
        data6.ts_us = bpf_ktime_get_ns() / 1000;
        bpf_probe_read(&data6.saddr, sizeof(data6.saddr),
            &newsk->__sk_common.skc_v6_rcv_saddr.in6_u.u6_addr32);
        bpf_probe_read(&data6.daddr, sizeof(data6.daddr),
            &newsk->__sk_common.skc_v6_daddr.in6_u.u6_addr32);
        data6.lport = lport;
        data6.dport = dport;
        bpf_get_current_comm(&data6.task, sizeof(data6.task));
        ipv6_events.perf_submit(ctx, &data6, sizeof(data6));
    }
    // else drop

    return 0;
}
"""

bpf_text_tracepoint = """
TRACEPOINT_PROBE(sock, inet_sock_set_state)
{
    if (args->protocol != IPPROTO_TCP)
        return 0;
    if (args->oldstate != TCP_SYN_RECV || args->newstate != TCP_ESTABLISHED)
        return 0;
    u32 pid = bpf_get_current_pid_tgid();

    ##FILTER_PID##

    // pull in details
    u16 family = 0, lport = 0, dport;
    family = args->family;
    lport = args->sport;
    dport = args->dport;

    ##FILTER_PORT##

    if (family == AF_INET) {
        struct ipv4_data_t data4 = {.pid = pid, .ip = 4};
        data4.ts_us = bpf_ktime_get_ns() / 1000;
        __builtin_memcpy(&data4.saddr, args->saddr, sizeof(data4.saddr));
        __builtin_memcpy(&data4.daddr, args->daddr, sizeof(data4.daddr));
        data4.lport = lport;
        data4.dport = dport;
        bpf_get_current_comm(&data4.task, sizeof(data4.task));
        ipv4_events.perf_submit(args, &data4, sizeof(data4));
    } else if (family == AF_INET6) {
        struct ipv6_data_t data6 = {.pid = pid, .ip = 6};
        data6.ts_us = bpf_ktime_get_ns() / 1000;
        __builtin_memcpy(&data6.saddr, args->saddr, sizeof(data6.saddr));
        __builtin_memcpy(&data6.daddr, args->daddr, sizeof(data6.daddr));
        data6.lport = lport;
        data6.dport = dport;
        bpf_get_current_comm(&data6.task, sizeof(data6.task));
        ipv6_events.perf_submit(args, &data6, sizeof(data6));
    }
    // else drop

    return 0;
}
"""

if (BPF.tracepoint_exists("sock", "inet_sock_set_state")):
    bpf_text += bpf_text_tracepoint
else:
    bpf_text += bpf_text_kprobe


# code substitutions
if args.pid:
    bpf_text = bpf_text.replace('##FILTER_PID##',
        'if (pid != %s) { return 0; }' % args.pid)
else:
    bpf_text = bpf_text.replace('##FILTER_PID##', '')
if args.port:
    lports = [int(lport) for lport in args.port.split(',')]
    lports_if = ' && '.join(['lport != %d' % lport for lport in lports])
    bpf_text = bpf_text.replace('##FILTER_PORT##',
        'if (%s) { return 0; }' % lports_if)
if debug or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

bpf_text = bpf_text.replace('##FILTER_PORT##', '')

# process event
def print_ipv4_event(cpu, data, size):
    event = b["ipv4_events"].event(data)
    global start_ts
    if args.time:
        print("%-9s" % strftime("%H:%M:%S"), end="")
    if args.timestamp:
        if start_ts == 0:
            start_ts = event.ts_us
        print("%-9.3f" % ((float(event.ts_us) - start_ts) / 1000000), end="")
    printb(b"%-7d %-12.12s %-2d %-16s %-5d %-16s %-5d" % (event.pid,
        event.task, event.ip,
        inet_ntop(AF_INET, pack("I", event.daddr)).encode(),
        event.dport,
        inet_ntop(AF_INET, pack("I", event.saddr)).encode(),
        event.lport))

def print_ipv6_event(cpu, data, size):
    event = b["ipv6_events"].event(data)
    global start_ts
    if args.time:
        print("%-9s" % strftime("%H:%M:%S"), end="")
    if args.timestamp:
        if start_ts == 0:
            start_ts = event.ts_us
        print("%-9.3f" % ((float(event.ts_us) - start_ts) / 1000000), end="")
    printb(b"%-7d %-12.12s %-2d %-16s %-5d %-16s %-5d" % (event.pid,
        event.task, event.ip,
        inet_ntop(AF_INET6, event.daddr).encode(),
        event.dport,
        inet_ntop(AF_INET6, event.saddr).encode(),
        event.lport))

# initialize BPF
b = BPF(text=bpf_text)

# header
if args.time:
    print("%-9s" % ("TIME"), end="")
if args.timestamp:
    print("%-9s" % ("TIME(s)"), end="")
print("%-7s %-12s %-2s %-16s %-5s %-16s %-5s" % ("PID", "COMM", "IP", "RADDR",
    "RPORT", "LADDR", "LPORT"))

start_ts = 0

# read events
b["ipv4_events"].open_perf_buffer(print_ipv4_event)
b["ipv6_events"].open_perf_buffer(print_ipv6_event)
while 1:
    try:
        b.perf_buffer_poll()
    except KeyboardInterrupt:
        exit()
