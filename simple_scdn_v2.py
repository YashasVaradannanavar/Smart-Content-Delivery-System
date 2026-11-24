#!/usr/bin/env python3
"""
simple_scdn.py - a beginner-friendly "Smart Content Delivery" mini-project
in ONE file.  Mininet + Open vSwitch + OpenFlow 1.3, no controller framework.

TOPOLOGY

        h1 (client)
             |
            s1   Open vSwitch, OpenFlow 1.3, no controller
          / |  \
        h2  h3  h4    three content servers, each running a small web server

HOW THE SWITCH DECIDES WHICH SERVER ANSWERS

    All three servers own the same virtual IP, the "VIP" 10.0.0.100/32.
    The client is given a static ARP entry saying the VIP lives at
    00:00:00:00:00:aa - a MAC address that belongs to no real host.  So every
    packet the client sends to the VIP arrives at s1 addressed to that fake
    MAC, and one single OpenFlow rule decides what happens next:

        priority=100, ip, nw_dst=10.0.0.100
            -> set_field:<that server's MAC> -> dl_dst
            -> output:<that server's switch port>

    Point the rule at h2 and h2 answers.  Point it at h4 and the very same
    client request is answered by h4.  The client never changes, which is the
    whole point of a software-defined network: forwarding is decided by rules
    in the switch, not by the client.

WHAT THE DEMO DOES (every step is printed while it runs)

    1. no rules       -> the client cannot reach the VIP at all.  Proof that
                         the OpenFlow rules really control the traffic.
    2. base rules     -> return-traffic rules + the VIP rule, requests work.
    3. health check   -> probe all three servers from h1 and record who answers.
    4. smart select   -> probe, then point the VIP rule at the healthy server
                         with the lowest measured latency.
    5. distribution   -> round-robin over h2/h3/h4, to show that rule changes
                         alone spread the traffic over all three servers.
    6. throughput     -> one 8 MiB download per server, measured.
    7. path failure   -> the link to h3 is unplugged at the switch.  h3's
                         process still runs, requests to it time out, the
                         policy drops h3, then the link is restored.
    8. server failure -> h3's web server is killed: requests to it are refused
                         and the health check marks h3 DOWN.
    9. availability   -> requests keep being served by the healthy servers.
   10. recovery       -> h3's web server restarts, the next health check sees
                         it, and the policy puts it back in the rotation.

SELECTION POLICY (the whole decision, in one sentence)

    Candidates are the servers that answered the last health check; among them
    the one with the lowest measured latency wins.  No server name is hard-coded
    into that decision, so a server leaves the rotation because of a
    measurement, not because the script was told to skip it.

Every request records success, latency and which server actually answered
(the servers say their own name in the response body, and each server also
logs the requests it served).  The numbers go to results/simple_scdn_results.csv

REQUIREMENTS: Linux with Mininet, Open vSwitch and Python 3 (WSL2 works).
Run as root, because Mininet creates network namespaces:

    sudo python3 simple_scdn.py
"""

import csv
import http.client
import json
import os
import re
import shutil
import socket
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
PROBE_TIMEOUT = 0.4                      # seconds allowed per health check
REQUEST_TIMEOUT = 2.5                    # seconds before a request counts as failed
LINK_SETTLE = 1.5                        # seconds to wait after unplug / replug
SMALL_FILE = 'index.txt'                 # tiny file, used for latency
BIG_FILE = 'big.bin'                     # 8 MiB file, used for throughput
BIG_BYTES = 8 * 1024 * 1024

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join('/tmp', 'simple_scdn')            # web pages + logs
RESULTS = os.path.join(HERE, 'results', 'simple_scdn_v2_results.csv')

MACS = {host: mac for host, _, mac in [CLIENT] + SERVERS}
PORTS = {}          # host name -> OpenFlow port number on s1 (filled at start)
HEALTH = {}         # host name -> {'ok': bool, 'ms': float}, last health check
ROWS = []           # every measurement, written to the CSV at the end
CLIENT_PROC = None  # the process doing the HTTP requests inside h1
CONDITIONS = {}     # scenario name -> snapshot kept for the comparison step
CONDITION_ORDER = ['port connected (h3 cable in)',
                   'port disconnected (cable pulled)',
                   'port reconnected (cable back)',
                   'server process killed (h3)',
                   'server restarted (h3)']
LAST_PHASE = ['']   # so a table header prints once per phase, not per row


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


def explain_units():
    """Say what every number in this output is, and in what unit.

    No time value should ever appear as a bare number: each one names what it
    measured (a request, a probe, a pause) and each rate names what it divides.
    """
    print()
    say('--- how to read the numbers in this output -------------------------')
    say('request time      measured inside h1, from the start of the TCP connect')
    say('                  to the last byte of the body.  It includes the server')
    say('                  building the reply, so it is request completion time and')
    say('                  NOT the network round trip time (RTT).')
    say('probe time        one TCP connect to the server\'s OWN address, no data.')
    say('                  The probe timeout is %.0f ms, so a DOWN probe near %.0f ms'
        % (PROBE_TIMEOUT * 1000, PROBE_TIMEOUT * 1000))
    say('                  means "no packets came back" (broken path), while a DOWN')
    say('                  probe near 0 ms means "refused" (the host is fine, but')
    say('                  nothing is listening on port %d).' % HTTP_PORT)
    say('throughput        MB/s = bytes received / transfer time, with 1 MB =')
    say('                  1,000,000 bytes (decimal MB, not MiB).')
    say('rule->hX          the server the VIP rule pointed at before the request.')
    say('served_by=hX      the server that really answered, read from the body.')
    say('                  These two must match; a mismatch is a real bug.')
    say('OK / FAIL         an HTTP 200 arrived / no usable answer arrived.')
    say('%.1f s timeout     the CLIENT gave up.  That number is the client\'s own'
        % REQUEST_TIMEOUT)
    say('                  patience, not the server\'s speed.')
    say('link settle       a fixed %.0f ms pause after unplug / replug, so that'
        % (LINK_SETTLE * 1000))
    say('                  Open vSwitch notices the port state change.')
    print()


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


FLOW_LEGEND_SHOWN = False


def flow_dump_meaning():
    """Explain one `ovs-ofctl dump-flows` line, once per run.

    Those numbers belong to the switch, not to a request, so they must not be
    read as request times or as a measure of how fast anything answered.
    """
    say('how to read a rule line - these numbers belong to the SWITCH, not to')
    say('any request:')
    say('  cookie=0x0         tag Open vSwitch stamps on every rule; unused here')
    say('  duration=x.xxxs    how many seconds this rule has existed inside the')
    say('                     switch.  It is the AGE of the rule, never the time')
    say('                     a request took.')
    say('  n_packets n_bytes  packets and bytes this rule has matched since it was')
    say('                     installed - the proof that the switch is using it')
    say('  priority=N         when several rules match one packet, the highest')
    say('                     priority wins')
    say('  actions=...        what the switch does: set_field rewrites a header')
    say('                     field, output:N sends the packet out port N')
    say('                     (on s1: h1=1, h2=2, h3=3, h4=4)')


def show_flows(comment):
    """Print the flow table, including the per-rule packet counters.

    Those counters are the proof that the rules are really used: n_packets is
    counted inside the switch.  Open vSwitch moves counters from the kernel
    datapath to its OpenFlow view about once a second, so wait for that first,
    otherwise a rule used a millisecond ago still reads zero.
    """
    global FLOW_LEGEND_SHOWN
    time.sleep(1.0)
    print('  --- OpenFlow rules currently in s1 (%s) ---' % comment)
    lines = [line.strip() for line in ofctl('dump-flows', 's1').splitlines()]
    lines = [line for line in lines
             if line and not line.upper().startswith('OFPST')]
    if lines and not FLOW_LEGEND_SHOWN:
        flow_dump_meaning()
        FLOW_LEGEND_SHOWN = True
    for line in lines:
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


def ask_client(command):
    """Send one line to the client process and read one JSON line back.

    A file name means "HTTP GET this file"; the word "probe" means "check
    whether every server is reachable".  One line in, one line out, so the
    rules or a health check can be changed between two requests.
    """
    CLIENT_PROC.stdin.write(command + '\n')
    CLIENT_PROC.stdin.flush()
    answer = CLIENT_PROC.stdout.readline()
    if not answer:
        raise RuntimeError('the client process inside h1 died, see '
                           + os.path.join(SCRATCH, 'client.log'))
    return json.loads(answer)


def fetch(name):
    """One HTTP GET for a file, timed and reported by the client itself."""
    return ask_client(name)


# ---------------------------------------------------------------------------
# Monitoring (health checks) and selection: the control logic that decides
# where the VIP rule points next.
# ---------------------------------------------------------------------------

def check_health():
    """Probe every server and remember the result.

    The probes leave h1 and go to each server's own address, so they never
    involve the VIP rule: they measure the server, not the current routing.
    """
    for probe in ask_client('probe')['probes']:
        HEALTH[probe['host']] = probe
    return HEALTH


def probe_meaning(probe):
    """Turn a probe result into words, so the number is never bare."""
    if probe['ok']:
        return 'answered: TCP connect completed'
    if probe['ms'] >= PROBE_TIMEOUT * 1000 * 0.9:
        return 'no answer within the %.0f ms probe timeout' % (PROBE_TIMEOUT * 1000)
    return ('connection refused: host up, nothing listening on port %d'
            % HTTP_PORT)


def show_health(comment):
    """One labelled row per server: state, probe time, what that time means."""
    print('  --- health check (%s) ---' % comment)
    print('     %-6s %-11s %-5s %11s   %s'
          % ('server', 'address', 'state', 'probe time',
             'what that probe time means'))
    for host, ip, _ in SERVERS:
        probe = HEALTH.get(host)
        if probe is None:
            print('     %-6s %-11s %-5s %11s   %s'
                  % (host, ip, '-', '-', 'not checked yet'))
            continue
        print('     %-6s %-11s %-5s %11s   %s'
              % (host, ip, 'UP' if probe['ok'] else 'DOWN',
                 '%.1f ms' % probe['ms'], probe_meaning(probe)))


def healthy_servers():
    """Servers that answered the most recent health check."""
    return [host for host, _, _ in SERVERS if HEALTH.get(host, {}).get('ok')]


def choose_server():
    """Which server should answer the next request?

    Policy in one sentence: only servers that answered the last health check
    are candidates, and among those the one with the lowest measured latency
    wins.  Returns None when nothing is healthy, so the caller has to deal with
    it instead of pretending a dead server can serve.
    """
    candidates = healthy_servers()
    if not candidates:
        return None
    return min(candidates, key=lambda host: HEALTH[host]['ms'])


def probe_servers():
    """The health check, run INSIDE h1.

    One plain TCP connect to each server's OWN address (10.0.0.11/12/13, not
    the VIP), so it tests the server itself and not wherever the VIP rule
    currently points.  The connect time doubles as a latency sample.
    """
    probes = []
    for host, ip, _ in SERVERS:
        started = time.perf_counter()
        ok = True
        try:
            socket.create_connection((ip, HTTP_PORT), timeout=PROBE_TIMEOUT).close()
        except OSError:
            ok = False          # refused, timed out or unreachable: unhealthy
        probes.append({'host': host, 'ok': ok,
                       'ms': round((time.perf_counter() - started) * 1000, 2)})
    return {'probes': probes}


def run_client():
    """Runs INSIDE h1 (Mininet starts the same file there).

    One HTTP GET per stdin line, one JSON line per request on stdout.
    """
    for line in sys.stdin:
        name = line.strip()
        if not name:
            continue
        if name == 'probe':         # a health check, not a file request
            print(json.dumps(probe_servers()), flush=True)
            continue
        started = time.perf_counter()
        ok, status, size, error, body = False, 0, 0, '', ''
        try:
            connection = http.client.HTTPConnection(VIP, HTTP_PORT,
                                                   timeout=REQUEST_TIMEOUT)
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


def error_meaning(row):
    """Say in words what failed, and why that produced this duration."""
    error = row['error'] or 'no answer'
    if error == 'TimeoutError':
        return ('gave up after the %.1f s client timeout: nothing came back '
                '(broken path or hung server)' % REQUEST_TIMEOUT)
    if error == 'ConnectionRefusedError':
        return ('refused at once: the server is reachable, nothing listens '
                'on port %d' % HTTP_PORT)
    if error == 'ConnectionResetError':
        return 'connection reset by the other end'
    return error


def report(row):
    """One line per request: a column header per phase, and a plain note."""
    if LAST_PHASE[0] != row['phase']:
        LAST_PHASE[0] = row['phase']
        print('  %-5s %-13s %-6s %-7s %10s   %s'
              % ('pass?', 'phase', 'rule->', 'served', 'time',
                 'what that time measures'))
    if row['ok']:
        note = 'completed: TCP connect + %d byte body' % row['bytes']
        if row['served_by'] and row['served_by'] != row['expected_server']:
            note += '  <-- WRONG SERVER (rule said %s)' % row['expected_server']
    else:
        note = error_meaning(row)
    print('  %-5s %-13s %-6s %-7s %10s   %s'
          % ('OK' if row['ok'] else 'FAIL', row['phase'],
             row['expected_server'], row['served_by'] or '-',
             '%.1f ms' % row['latency_ms'], note))


def do_request(phase, expected):
    """One HTTP request for the small file, recorded and printed."""
    result = fetch(SMALL_FILE)
    row = {'phase': phase,
           'expected_server': expected,
           'served_by': served_by(result['body']),
           'healthy': ','.join(healthy_servers()) or '-',
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


def phase_health():
    step('STEP 3: monitor the servers with a health check')
    say('From h1 we open a plain TCP connection to each server\'s own address')
    say('(10.0.0.11, 10.0.0.12, 10.0.0.13 - not the VIP).  A server that')
    say('answers is healthy, and its connect time is a latency sample we can')
    say('use for selection.  The VIP rule plays no part in this at all.')
    check_health()
    show_health('baseline: all three servers should be UP')


def phase_smart_selection():
    step('STEP 4: select the server from health + latency, not round-robin')
    say('Policy: only healthy servers are candidates, and among them the one')
    say('with the lowest measured latency is chosen.  Each round probes,')
    say('decides, re-points the VIP rule, then sends one request, so the')
    say('reason for the choice is printed before the traffic moves.')
    say('(The three virtual links are identical, so their latencies differ')
    say('only by noise and the same server tends to win again and again.')
    say('That is the correct result for this policy; step 5 keeps the')
    say('round-robin pass precisely to show traffic can be spread too.)')
    for index in range(6):
        check_health()
        best = choose_server()
        show_health('before request %d of 6' % (index + 1))
        print('  decision: healthy=%s, lowest latency=%s -> VIP -> %s'
              % (','.join(healthy_servers()), best, best))
        point_vip_at(best)
        do_request('smart', best)
    show_flows('after latency-based selection')


def phase_distribution():
    step('STEP 5: load distribution over the three servers (round-robin baseline)')
    say('Before each request the VIP rule is re-pointed at the next server, so')
    say('the traffic really goes h2, h3, h4, h2, h3, h4 ...  The client always')
    say('asks for the same VIP; only the switch rule changes.')
    for index in range(9):
        host = SERVERS[index % len(SERVERS)][0]
        point_vip_at(host)
        do_request('distribution', host)
    show_flows('after distribution')


def phase_throughput():
    step('STEP 6: throughput, one 8 MiB download per server')
    say('Same mechanism, bigger file: the VIP rule chooses who sends it.')
    say('The numbers are real bytes per second, but between virtual hosts they')
    say('mostly measure memory speed rather than a real network link.')
    print('  result rule-> served  bytes received  transfer time    throughput')
    for host, _, _ in SERVERS:
        point_vip_at(host)
        result = fetch(BIG_FILE)
        seconds = result['seconds']
        mbps = (result['bytes'] / 1e6) / seconds if seconds > 0 else 0.0
        row = {'phase': 'throughput',
               'expected_server': host,
               'served_by': served_by(result['body']),
               'healthy': ','.join(healthy_servers()) or '-',
               'ok': result['ok'],
               'latency_ms': round(seconds * 1000, 2),
               'bytes': result['bytes'],
               'throughput_mbps': round(mbps, 2),
               'error': result['error']}
        ROWS.append(row)
        print('  %-6s %-6s %-7s %14s %13s %13s'
              % ('OK' if row['ok'] else 'FAIL', host, row['served_by'] or '-',
                 format(row['bytes'], ','),
                 '%.1f ms' % row['latency_ms'], '%.1f MB/s' % mbps))


def set_link(net, host, state):
    """Unplug / replug a server's cable: a genuine link failure.

    Mininet takes the link down on both ends, so the server really has no path
    to the switch.  Its web server keeps running the whole time.
    """
    net.configLinkStatus(host, 's1', state)
    time.sleep(LINK_SETTLE)  # let Open vSwitch notice the port state change


def phase_path_failure(net):
    step('STEP 7: a path failure - unplug h3\'s link at the switch')
    say('h3\'s web server keeps running and its address is unchanged; only the')
    say('cable is pulled.  Compare the error with step 8: a broken path cannot')
    say('answer at all, so the request times out instead of being refused.')

    # Reference measurement with the cable still in: the "connected" half of
    # the side-by-side comparison printed in step 11.
    check_health()
    show_health('before the failure: h3 is UP on both counts')
    point_vip_at('h3')
    row = do_request('path-reference', 'h3')
    snapshot('port connected (h3 cable in)', 'UP', 'running', row)

    say('now pull the cable:')
    set_link(net, 'h3', 'down')
    point_vip_at('h3')
    row = do_request('path-failure', 'h3')
    check_health()
    show_health('h3 should be DOWN: its path is gone')
    snapshot('port disconnected (cable pulled)', 'DOWN', 'running', row)
    say('the policy drops h3 and keeps serving on the other two:')
    for index in range(4):
        best = choose_server()
        point_vip_at(best)
        do_request('path-failure', best)
    say('plug the cable back in:')
    set_link(net, 'h3', 'up')
    check_health()
    show_health('h3 should be UP again')
    # Aim at h3 on purpose: this proves the repaired path carries VIP traffic
    # again, rather than just proving that some other server still works.
    point_vip_at('h3')
    row = do_request('path-back', 'h3')
    snapshot('port reconnected (cable back)', 'UP', 'running', row)


def phase_server_failure(net, pids):
    step('STEP 8: a server failure - kill h3\'s web server')
    say('This time the path is fine and the process dies.  On purpose the VIP')
    say('rule is left pointing at h3, so the next request must fail: that is')
    say('what a dead server looks like to a user.')
    stop_server(net.get('h3'), pids['h3'])
    point_vip_at('h3')
    row = do_request('failure', 'h3')
    if row['ok']:
        say('-> unexpected: h3 still answered, check that it was really stopped')
    else:
        say('-> refused, not timed out: the link is fine, the process is gone.')
    say('Now the health check runs.  h3 refuses the probe, so it is marked')
    say('DOWN.  Nothing in the code mentions h3: the exclusion comes from the')
    say('measurement.')
    check_health()
    show_health('h3 should be DOWN')
    snapshot('server process killed (h3)', 'UP', 'stopped', row)


def phase_availability():
    step('STEP 9: availability - keep serving while h3 is unhealthy')
    say('Requests keep arriving while h3 is dead.  Because the policy only')
    say('offers healthy servers, every request is served.  The client is never')
    say('told anything and still asks for the same VIP address.')
    for index in range(6):
        best = choose_server()
        point_vip_at(best)
        do_request('availability', best)
    show_flows('while h3 is dead the VIP rule only ever points at h2 or h4')


def phase_recovery(net, pids):
    step('STEP 10: recovery - restart h3 and let the policy find it again')
    say('h3\'s web server is started again.  Nothing tells the selection logic')
    say('about it: the next health check simply sees h3 answering, so h3 is a')
    say('candidate again and is chosen like any other server.')
    pids['h3'] = start_server(net.get('h3'), 'h3')
    check_health()
    show_health('h3 should be UP again')

    # One request aimed at h3 on purpose: proves h3 itself serves VIP traffic
    # again, not merely that some other server still works.
    point_vip_at('h3')
    row = do_request('recovery-back', 'h3')
    snapshot('server restarted (h3)', 'UP', 'running', row)

    say('and now six ordinary policy decisions, with h3 back in the pool:')
    for index in range(6):
        best = choose_server()
        point_vip_at(best)
        do_request('recovery', best)


# ---------------------------------------------------------------------------
# Comparison: the same request under the conditions the demo created
# ---------------------------------------------------------------------------

def snapshot(name, link, process, row):
    """Keep one measurement so step 11 can print them side by side."""
    probe = HEALTH.get('h3', {})
    CONDITIONS[name] = {'link': link,
                        'process': process,
                        'ok': row['ok'],
                        'served_by': row['served_by'],
                        'error': row['error'],
                        'latency_ms': row['latency_ms'],
                        'probe_ms': probe.get('ms'),
                        'probe_ok': probe.get('ok')}


def comparison():
    """Side-by-side view of every condition the demo created, plus deltas."""
    step('STEP 11: comparison - one request, five different conditions')
    say('Same client (h1), same VIP (10.0.0.100), same 13-byte file, same server')
    say('(h3).  Only h3\'s cable and h3\'s web server differ between the rows.')
    say('"request time" and "probe time" are defined in the header at step 0.')
    print()
    print('  %-32s %-7s %-9s %-6s %-7s %11s %10s'
          % ('condition', 'h3 link', 'h3 server', 'result', 'served',
             'request time', 'probe time'))
    for name in CONDITION_ORDER:
        item = CONDITIONS.get(name)
        if item is None:
            continue
        probe = ('%.1f ms' % item['probe_ms']
                 if item['probe_ms'] is not None else '-')
        print('  %-32s %-7s %-9s %-6s %-7s %11s %10s'
              % (name, item['link'], item['process'],
                 'OK' if item['ok'] else 'FAIL', item['served_by'] or '-',
                 '%.1f ms' % item['latency_ms'], probe))

    connected = CONDITIONS.get('port connected (h3 cable in)')
    pulled = CONDITIONS.get('port disconnected (cable pulled)')
    replugged = CONDITIONS.get('port reconnected (cable back)')
    killed = CONDITIONS.get('server process killed (h3)')
    restarted = CONDITIONS.get('server restarted (h3)')

    if connected and pulled:
        print()
        say('what DISCONNECTING THE PORT changes:')
        say('  request time   %.1f ms -> %.1f ms  (+%.1f ms, about %.0fx slower)'
            % (connected['latency_ms'], pulled['latency_ms'],
               pulled['latency_ms'] - connected['latency_ms'],
               pulled['latency_ms'] / max(connected['latency_ms'], 0.01)))
        say('  probe time     %.1f ms -> %.1f ms  (hits the %.0f ms probe timeout)'
            % (connected['probe_ms'] or 0.0, pulled['probe_ms'] or 0.0,
               PROBE_TIMEOUT * 1000))
        say('  error          none -> %s' % (pulled['error'] or 'none'))
        say('  why            the frame stops at the switch, so no answer and no')
        say('                 error can come back.  The %.1f s figure is the client'
            % REQUEST_TIMEOUT)
        say('                 giving up, not h3 needing that long to reply.')
    if replugged:
        print()
        say('what RECONNECTING THE PORT changes:')
        say('  request time   %.1f ms again and the error is gone: the repaired path'
            % replugged['latency_ms'])
        say('                 carries VIP traffic, with no change to the client.')
    if connected and killed:
        print()
        say('what KILLING THE SERVER PROCESS changes (cable still in):')
        say('  request time   %.1f ms -> %.1f ms  - FASTER, not slower'
            % (connected['latency_ms'], killed['latency_ms']))
        say('  error          none -> %s' % (killed['error'] or 'none'))
        say('  why            the packet reaches h3 and is rejected immediately, so')
        say('                 there is no timeout to sit through.  A refused')
        say('                 connection and a dead path are different failures,')
        say('                 which is why the client reports two different errors.')
    if restarted:
        print()
        say('what RESTARTING THE SERVER PROCESS changes:')
        say('  request time   %.1f ms again, served_by=%s: h3 is a candidate again'
            % (restarted['latency_ms'], restarted['served_by'] or '-'))
        say('                 because the next health check measured it, not because')
        say('                 any code re-enables h3.')

    if pulled and killed:
        print()
        say('the one-line version for the viva:')
        say('  port disconnected -> TimeoutError after %.0f ms (nothing comes back)'
            % pulled['latency_ms'])
        say('  process killed    -> ConnectionRefusedError after %.1f ms'
            % killed['latency_ms'])
        say('  same user experience (a failed request), completely different cause.')

    # Does the SERVICE keep working while h3 is unhealthy?
    print()
    say('service-level comparison (does the client still get an answer?)')
    windows = [('all healthy, before any failure',
                ('baseline', 'smart', 'distribution', 'path-reference')),
               ('h3 port pulled, policy active', ('path-failure',)),
               ('h3 process killed, policy active',
                ('failure', 'availability')),
               ('after h3 recovered',
                ('path-back', 'recovery-back', 'recovery'))]
    print('  %-36s %8s %7s %6s %9s %13s'
          % ('window', 'requests', 'served', 'failed', 'success', 'mean time'))
    for label, phases in windows:
        rows = [row for row in ROWS if row['phase'] in phases]
        if not rows:
            continue
        good = [row for row in rows if row['ok']]
        mean = (sum(row['latency_ms'] for row in good) / len(good)) if good else None
        print('  %-36s %8d %7d %6d %8.0f%% %13s'
              % (label, len(rows), len(good), len(rows) - len(good),
                 100.0 * len(good) / len(rows),
                 '%.1f ms' % mean if mean is not None else '-'))
    say('the failed requests in the middle windows are deliberate: the rule is')
    say('pointed at the broken server on purpose, to show what a user would see.')
    say('the policy itself never routes to an unhealthy server, which is why the')
    say('requests that follow each health check are all served.')


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def summarise():
    step('SUMMARY')
    say('mean time counts SUCCESSFUL requests only.  A %.1f s timeout is left out'
        % REQUEST_TIMEOUT)
    say('on purpose, otherwise one dead path would hide the real service time.')
    say('request time = TCP connect + body, measured inside h1 (defined at step 0).')
    print()
    print('  %-14s %9s %10s %7s %9s %12s'
          % ('phase', 'requests', 'succeeded', 'failed', 'success', 'mean time'))
    for phase in ('no-rules', 'baseline', 'smart', 'distribution',
                  'throughput', 'path-reference', 'path-failure', 'path-back',
                  'failure', 'availability', 'recovery-back', 'recovery'):
        rows = [row for row in ROWS if row['phase'] == phase]
        if not rows:
            continue
        good = [row for row in rows if row['ok']]
        mean = (sum(row['latency_ms'] for row in good) / len(good)) if good else None
        print('  %-14s %9d %10d %7d %8.0f%% %12s'
              % (phase, len(rows), len(good), len(rows) - len(good),
                 100.0 * len(good) / len(rows),
                 '%.1f ms' % mean if mean is not None else '-'))

    print()
    say('requests answered by each server (from the response body):')
    for host, _, _ in SERVERS:
        count = sum(1 for row in ROWS if row['served_by'] == host)
        print('     %-2s %2d request(s)' % (host, count))

    print()
    say('throughput per server, 8 MiB file, MB/s = 1,000,000 bytes per second:')
    print('     %-4s %14s %14s %12s'
          % ('host', 'bytes received', 'transfer time', 'throughput'))
    for row in ROWS:
        if row['phase'] == 'throughput':
            print('     %-4s %14s %14s %12s'
                  % (row['expected_server'], '{:,}'.format(row['bytes']),
                     '%.1f ms' % row['latency_ms'],
                     '%.1f MB/s' % (row['throughput_mbps'] or 0.0)))

    print()
    say('independent check, from each web server\'s own access log:')
    for host, _, _ in SERVERS:
        try:
            with open(os.path.join(SCRATCH, host + '.log')) as handle:
                gets = handle.read().count('"GET ')
        except OSError:
            gets = 0
        print('     %-2s served %2d requests' % (host, gets))


def save_csv():
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    fields = ['phase', 'expected_server', 'served_by', 'healthy', 'ok',
              'latency_ms', 'bytes', 'throughput_mbps', 'error']
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
        sys.exit('Mininet needs root.  Run:  sudo python3 %s'
                 % os.path.basename(__file__))
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
        explain_units()

        phase_no_rules()
        phase_base_rules()
        phase_health()
        phase_smart_selection()
        phase_distribution()
        phase_throughput()
        phase_path_failure(net)
        phase_server_failure(net, pids)
        phase_availability()
        phase_recovery(net, pids)
        comparison()
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
