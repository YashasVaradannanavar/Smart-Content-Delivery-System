#!/usr/bin/env python3

import csv
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# The addressing plan.  Everything the demo needs to know lives right here.
# ---------------------------------------------------------------------------

VIP = '10.0.0.100'                       # the address the client asks for
VIP_MAC = '00:00:00:00:00:aa'            # fake MAC nobody owns: the "virtual" MAC

CLIENT = ('h1', '10.0.0.1', '00:00:00:00:00:01')
SERVERS = [
    ('h2', '10.0.0.11', '00:00:00:00:00:11'),
    ('h3', '10.0.0.12', '00:00:00:00:00:12'),
    ('h4', '10.0.0.13', '00:00:00:00:00:13'),
]

HTTP_PORT = 8080
SMALL_FILE = 'index.txt'                 # tiny file, used for latency
BIG_FILE = 'big.bin'                     # 8 MiB file, used for throughput
BIG_BYTES = 8 * 1024 * 1024

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join('/tmp', 'simple_scdn')            # web pages + logs
RESULTS = os.path.join(HERE, 'results', 'simple_scdn_results.csv')

MACS = {host: mac for host, _, mac in [CLIENT] + SERVERS}
PORTS = {}          # host name -> OpenFlow port number on s1 (filled at start)
ROWS = []           # every measurement, written to the CSV at the end
CLIENT_PROC = None  # the process doing the HTTP requests inside h1


# ---------------------------------------------------------------------------
# Small printing and shell helpers
# ---------------------------------------------------------------------------

def step(title):
    print()
    print('=' * 74)
    print(title)
    print('=' * 74)


def say(text):
    print('  ' + text)


def run(args, check=True):
    """Run a command on this machine (ovs-ofctl, ovs-vsctl, ip ...)."""
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError('%s failed: %s' % (' '.join(args), result.stderr.strip()))
    return result.stdout


def ofctl(*args):
    """Talk to switch s1 with OpenFlow 1.3 messages."""
    return run(['ovs-ofctl', '-O', 'OpenFlow13'] + list(args))


def add_flow(rule):
    ofctl('add-flow', 's1', rule)


def show_flows(comment):
    """Print the flow table, including the per-rule packet counters.

    Those counters are the proof that the rules are really used: n_packets is
    counted inside the switch.  Open vSwitch moves counters from the kernel
    datapath to its OpenFlow view about once a second, so wait for that first,
    otherwise a rule used a millisecond ago still reads zero.
    """
    time.sleep(1.0)
    print('  --- OpenFlow rules currently in s1 (%s) ---' % comment)
    for line in ofctl('dump-flows', 's1').splitlines():
        line = line.strip()
        if line and not line.upper().startswith('OFPST'):
            print('     ' + line)


# ---------------------------------------------------------------------------
# Topology: one client, one OpenFlow switch, three servers
# ---------------------------------------------------------------------------

def cleanup_stale():
    """Remove whatever an interrupted earlier run may have left behind."""
    run(['ovs-vsctl', '--if-exists', 'del-br', 's1'], check=False)
    for index in range(1, 6):
        run(['ip', 'link', 'del', 's1-eth%d' % index], check=False)
    for host in MACS:
        run(['ip', 'link', 'del', '%s-eth0' % host], check=False)


def build_network():
    """Create h1 + s1 + h2/h3/h4 with Mininet's Python API."""
    from mininet.log import setLogLevel
    from mininet.net import Mininet
    from mininet.node import OVSSwitch

    setLogLevel('warning')
    # controller=None: the switch has no controller, we program it ourselves.
    net = Mininet(controller=None, switch=OVSSwitch, build=False,
                  ipBase='10.0.0.0/24')
    switch = net.addSwitch('s1', cls=OVSSwitch, protocols='OpenFlow13')
    client = net.addHost(CLIENT[0], ip=CLIENT[1] + '/24', mac=CLIENT[2])
    hosts = [net.addHost(host, ip=ip + '/24', mac=mac)
             for host, ip, mac in SERVERS]
    for node in [client] + hosts:
        net.addLink(node, switch)
    net.build()
    net.start()

    # With no controller the switch must not fall back to being a learning
    # switch, otherwise it would forward traffic without our rules.
    run(['ovs-vsctl', 'set', 'Bridge', 's1', 'fail_mode=secure'])
    return net


def discover_ports():
    """Map each host to the OpenFlow port number it is plugged into."""
    text = ofctl('show', 's1')
    interfaces = {}
    for line in text.splitlines():
        match = re.match(r'\s*(\d+)\(([^)]+)\)', line)
        if match:
            interfaces[match.group(2)] = int(match.group(1))
    # Links were added in this order, so h1 is s1-eth1, h2 is s1-eth2, ...
    for index, host in enumerate(MACS, start=1):
        name = 's1-eth%d' % index
        if name not in interfaces:
            raise RuntimeError('switch port for %s (%s) not found' % (host, name))
        PORTS[host] = interfaces[name]
    say('switch ports: ' + ', '.join('%s=%d' % item for item in PORTS.items()))


def prepare_hosts(net):
    """Give every server the VIP and pin the client's ARP entry for it."""
    for host, _, _ in SERVERS:
        # Each server owns the VIP as a /32 on its own interface.  Which server
        # actually receives a packet is decided by the switch rule below.
        net.get(host).cmd('ip addr replace %s/32 dev %s-eth0' % (VIP, host))

    # The client believes the VIP is at a MAC address no host owns.  Packets
    # for the VIP therefore arrive at s1 with a known destination MAC, and the
    # switch rewrites it to the MAC of whichever server should answer.
    net.get(CLIENT[0]).cmd(
        'ip neigh replace %s lladdr %s dev %s-eth0 nud permanent'
        % (VIP, VIP_MAC, CLIENT[0]))


# ---------------------------------------------------------------------------
# The actual OpenFlow rules
# ---------------------------------------------------------------------------

def install_base_rules():
    """Rules that never change while the demo runs.

    Each host's MAC is matched so that reply traffic and ARP replies find their
    way back.  ARP itself is broadcast, so it is flooded.
    """
    for host in MACS:
        add_flow('priority=50,dl_dst=%s,actions=output:%d' % (MACS[host], PORTS[host]))
    add_flow('priority=10,arp,actions=FLOOD')


def point_vip_at(host):
    """The one rule that selects a server: delete the old one, install the new.

    A packet for the VIP is rewritten so that it looks like a packet addressed
    to `host`, then handed to that host's switch port.  Nothing else in the
    network is touched.
    """
    ofctl('del-flows', 's1', 'ip,nw_dst=%s' % VIP)
    add_flow('priority=100,ip,nw_dst=%s,'
             'actions=set_field:%s->dl_dst,output:%d'
             % (VIP, MACS[host], PORTS[host]))


# ---------------------------------------------------------------------------
# Web servers (real HTTP servers inside the server namespaces)
# ---------------------------------------------------------------------------

def make_content():
    """One small file per server that names the server, plus one big file.

    Both files start with "served by <host>", so every response says which
    server really produced it, even the big binary one.
    """
    base = os.path.join(SCRATCH, 'www')
    os.makedirs(base, exist_ok=True)
    blob = os.urandom(BIG_BYTES)
    for host, _, _ in SERVERS:
        folder = os.path.join(base, host)
        os.makedirs(folder, exist_ok=True)
        banner = ('served by %s\n' % host).encode()
        with open(os.path.join(folder, SMALL_FILE), 'wb') as handle:
            handle.write(banner)
        with open(os.path.join(folder, BIG_FILE), 'wb') as handle:
            handle.write(banner + blob)
        # start this run with an empty access log (start_server appends to it)
        open(os.path.join(SCRATCH, host + '.log'), 'w').close()


def start_server(node, host):
    """Start python's built-in web server inside that host's namespace."""
    folder = os.path.join(SCRATCH, 'www', host)
    log = os.path.join(SCRATCH, host + '.log')
    # The trailing "echo $!" gives us the pid, so we can stop this one server
    # without touching the other two (the hosts share one pid namespace).
    pid_text = node.cmd('python3 -m http.server %d --directory %s >> %s 2>&1 & echo $!'
                        % (HTTP_PORT, folder, log))
    pid = int(pid_text.strip().split()[-1])
    wait_until_listening(node, host)
    return pid


def wait_until_listening(node, host, timeout=5.0):
    """Wait until the web server listens.  Sockets are per namespace, so the
    'ss' output inside this host only shows this host's listeners."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ':%d' % HTTP_PORT in node.cmd('ss -ltn'):
            return
        time.sleep(0.1)
    raise RuntimeError('the web server on %s did not start' % host)


def stop_server(node, pid):
    """Kill that host's web server (this is our simulated server failure)."""
    node.cmd('kill %d 2>/dev/null' % pid)
    time.sleep(0.3)


# ---------------------------------------------------------------------------
# The client.  It runs inside h1, so the timings are measured in h1 itself.
# ---------------------------------------------------------------------------

def start_client(net):
    """Start this same file with --client inside h1's network namespace."""
    global CLIENT_PROC
    error_log = open(os.path.join(SCRATCH, 'client.log'), 'w')
    CLIENT_PROC = net.get(CLIENT[0]).popen(
        [sys.executable, os.path.abspath(__file__), '--client'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=error_log, universal_newlines=True)


def fetch(name):
    """Ask the in-client process for one file and read its JSON answer.

    The client process does one HTTP GET per line it reads on stdin, so we can
    change the switch rules between two requests.
    """
    CLIENT_PROC.stdin.write(name + '\n')
    CLIENT_PROC.stdin.flush()
    answer = CLIENT_PROC.stdout.readline()
    if not answer:
        raise RuntimeError('the client process inside h1 died, see '
                           + os.path.join(SCRATCH, 'client.log'))
    return json.loads(answer)


def run_client():
    """Runs INSIDE h1 (Mininet starts the same file there).

    One HTTP GET per stdin line, one JSON line per request on stdout.
    """
    for line in sys.stdin:
        name = line.strip()
        if not name:
            continue
        started = time.perf_counter()
        ok, status, size, error, body = False, 0, 0, '', ''
        try:
            connection = http.client.HTTPConnection(VIP, HTTP_PORT, timeout=2.5)
            connection.request('GET', '/' + name)
            response = connection.getresponse()
            data = response.read()
            status, size = response.status, len(data)
            body = data[:40].decode('utf-8', 'replace').strip()
            ok = status == 200
            connection.close()
        except Exception as exc:      # timeout, refused, reset: all are failures
            error = type(exc).__name__
        elapsed = time.perf_counter() - started
        print(json.dumps({'ok': ok, 'status': status, 'seconds': elapsed,
                          'bytes': size, 'body': body, 'error': error}),
              flush=True)


def served_by(body):
    """The servers answer with 'served by h3', so the body names the real one."""
    first_line = body.splitlines()[0] if body else ''
    return first_line.split()[-1] if first_line.startswith('served by') else ''


def report(row):
    verdict = 'OK  ' if row['ok'] else 'FAIL'
    note = ''
    if not row['ok']:
        note = '  (%s)' % (row['error'] or 'no answer')
    elif row['served_by'] and row['served_by'] != row['expected_server']:
        note = '  <-- WRONG SERVER'
    print('  %s %-12s rule->%-2s  served_by=%-2s %8.1f ms%s'
          % (verdict, row['phase'], row['expected_server'],
             row['served_by'] or '-', row['latency_ms'], note))


def do_request(phase, expected):
    """One HTTP request for the small file, recorded and printed."""
    result = fetch(SMALL_FILE)
    row = {'phase': phase,
           'expected_server': expected,
           'served_by': served_by(result['body']),
           'ok': result['ok'],
           'latency_ms': round(result['seconds'] * 1000, 2),
           'bytes': result['bytes'],
           'throughput_mbps': '',
           'error': result['error']}
    ROWS.append(row)
    report(row)
    return row


# ---------------------------------------------------------------------------
# The demonstration, one function per step
# ---------------------------------------------------------------------------

def phase_no_rules():
    step('STEP 1: the flow table is empty, so the switch drops everything')
    say('s1 has no rules and no controller.  Its fail mode is "secure", which')
    say('means unmatched packets are dropped instead of being forwarded.')
    say('The client will ask for the VIP and get nothing back.')
    show_flows('expected to be empty')
    row = do_request('no-rules', '-')
    if not row['ok']:
        say('-> no answer, exactly as expected: without rules there is no service.')


def phase_base_rules():
    step('STEP 2: install the forwarding rules, traffic starts to flow')
    say('Two kinds of rule are added:')
    say('  * one per host, so replies reach it by MAC address')
    say('  * one that floods ARP, so hosts can find each other')
    say('Then the important one: VIP traffic is sent to h2.')
    install_base_rules()
    point_vip_at('h2')
    do_request('baseline', 'h2')
    show_flows('after one request: the VIP rule has forwarded packets')


def phase_distribution():
    step('STEP 3: load distribution over the three servers')
    say('Before each request the VIP rule is re-pointed at the next server, so')
    say('the traffic really goes h2, h3, h4, h2, h3, h4 ...  The client always')
    say('asks for the same VIP; only the switch rule changes.')
    for index in range(9):
        host = SERVERS[index % len(SERVERS)][0]
        point_vip_at(host)
        do_request('distribution', host)
    show_flows('after distribution')


def phase_throughput():
    step('STEP 4: throughput, one 8 MiB download per server')
    say('Same mechanism, bigger file: the VIP rule chooses who sends it.')
    say('The numbers are real bytes per second, but between virtual hosts they')
    say('mostly measure memory speed rather than a real network link.')
    for host, _, _ in SERVERS:
        point_vip_at(host)
        result = fetch(BIG_FILE)
        seconds = result['seconds']
        mbps = (result['bytes'] / 1e6) / seconds if seconds > 0 else 0.0
        row = {'phase': 'throughput',
               'expected_server': host,
               'served_by': served_by(result['body']),
               'ok': result['ok'],
               'latency_ms': round(seconds * 1000, 2),
               'bytes': result['bytes'],
               'throughput_mbps': round(mbps, 2),
               'error': result['error']}
        ROWS.append(row)
        print('  %s rule->%s served_by=%-2s %d bytes in %6.1f ms = %6.1f MB/s'
              % ('OK  ' if row['ok'] else 'FAIL', host, row['served_by'] or '-',
                 row['bytes'], row['latency_ms'], mbps))


def phase_failure(net, pids):
    step('STEP 5: simulate a server failure by killing h3\'s web server')
    say('h3 stops serving.  Nothing else is changed on purpose: the VIP rule is')
    say('still pointed at h3, so the next request that lands on h3 must fail.')
    stop_server(net.get('h3'), pids['h3'])
    point_vip_at('h3')
    row = do_request('failure', 'h3')
    if row['ok']:
        say('-> unexpected: h3 still answered, check that it was really stopped')
    else:
        say('-> the request failed, which is what a dead server looks like.')


def phase_failover():
    step('STEP 6: failover - change the rules so traffic avoids h3')
    say('The VIP rule is now only pointed at the healthy servers.  The client')
    say('is not touched and keeps asking for the same VIP address.')
    healthy = [server for server in SERVERS if server[0] != 'h3']
    for index in range(6):
        host = healthy[index % len(healthy)][0]
        point_vip_at(host)
        row = do_request('failover', host)
        if not row['ok']:
            say('-> a healthy server failed too, so something else is wrong.')
    show_flows('after failover: h3 is no longer in the rotation')


def phase_recovery(net, pids):
    step('STEP 7: recovery - restart h3 and put it back in the rotation')
    say('h3\'s web server is started again, then the VIP rule is pointed at h3')
    say('again.  No other change is needed for the old server to rejoin.')
    pids['h3'] = start_server(net.get('h3'), 'h3')
    for index in range(6):
        host = SERVERS[index % len(SERVERS)][0]
        point_vip_at(host)
        do_request('recovery', host)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def summarise():
    step('SUMMARY')
    for phase in ('no-rules', 'baseline', 'distribution', 'throughput',
                  'failure', 'failover', 'recovery'):
        rows = [row for row in ROWS if row['phase'] == phase]
        if not rows:
            continue
        good = [row for row in rows if row['ok']]
        mean = sum(row['latency_ms'] for row in good) / len(good) if good else 0.0
        print('  %-12s %2d request(s), %2d succeeded, mean %7.1f ms'
              % (phase, len(rows), len(good), mean))

    print()
    print('  requests answered by each server:')
    for host, _, _ in SERVERS:
        count = sum(1 for row in ROWS if row['served_by'] == host)
        print('     %-2s %2d' % (host, count))

    print()
    print('  throughput:')
    for row in ROWS:
        if row['phase'] == 'throughput':
            print('     %-2s %6.1f MB/s  (%d bytes)'
                  % (row['expected_server'], row['throughput_mbps'] or 0.0,
                     row['bytes']))

    print()
    print('  independent check, from each web server\'s own access log:')
    for host, _, _ in SERVERS:
        try:
            with open(os.path.join(SCRATCH, host + '.log')) as handle:
                gets = handle.read().count('"GET ')
        except OSError:
            gets = 0
        print('     %-2s served %2d requests' % (host, gets))


def save_csv():
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    fields = ['phase', 'expected_server', 'served_by', 'ok', 'latency_ms',
              'bytes', 'throughput_mbps', 'error']
    with open(RESULTS, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(ROWS)
    print()
    say('results written to %s' % RESULTS)


# ---------------------------------------------------------------------------

def main():
    if '--client' in sys.argv:
        run_client()
        return 0

    if os.geteuid() != 0:
        sys.exit('Mininet needs root.  Run:  sudo python3 simple_scdn.py')
    for tool in ('mn', 'ovs-vsctl', 'ovs-ofctl'):
        if not shutil.which(tool):
            sys.exit('required program not found: %s' % tool)

    os.makedirs(SCRATCH, exist_ok=True)
    make_content()
    cleanup_stale()

    net = None
    pids = {}
    try:
        step('STEP 0: build the topology')
        net = build_network()
        discover_ports()
        prepare_hosts(net)
        start_client(net)
        for host, _, _ in SERVERS:
            pids[host] = start_server(net.get(host), host)
        say('h1 (client), s1 (OpenFlow 1.3 switch), h2/h3/h4 (servers) are up.')
        say('the client asks for %s, which all three servers own as a /32.' % VIP)

        phase_no_rules()
        phase_base_rules()
        phase_distribution()
        phase_throughput()
        phase_failure(net, pids)
        phase_failover()
        phase_recovery(net, pids)
        summarise()
    finally:
        if CLIENT_PROC is not None:
            CLIENT_PROC.terminate()
        for host, pid in pids.items():
            if net is not None:
                try:
                    net.get(host).cmd('kill %d 2>/dev/null' % pid)
                except Exception:
                    pass
        if net is not None:
            net.stop()

    save_csv()
    return 0


if __name__ == '__main__':
    sys.exit(main())
