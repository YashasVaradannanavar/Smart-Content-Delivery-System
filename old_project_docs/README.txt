SMART CONTENT DELIVERY SYSTEM
=============================

A working Software-Defined Networking project: three TCP content servers, one
OpenFlow 1.3 switch, and a controller that decides which server answers each
client connection - then proves the decision by rewriting the switch's flow
table.

The client only ever knows one address, 10.0.0.100. Which physical machine
answers is decided by the controller, installed as an OpenFlow rule, and
enforced by the switch. Kill a server and the switch stops sending traffic to
it, without the client knowing anything changed.


-------------------------------------------------------------------------
1. STATUS
-------------------------------------------------------------------------

This is a complete, working implementation, not a plan.  Its current state was
verified on the development machine:

    unit tests                 28 tests, all passing
    end-to-end demonstration   all five phases pass
    acceptance assertions       passed (tests/check_result
                               - see section 15)

Verified on:
    Windows 11 host, Freebuff, Ubuntu 26.04 in WSL2
    Python 3.14.4, Mininet 2.3.0, Open vSwitch 3.7.1

Nothing here is a placeholder.  Every rule described in section 9 was observed
in a real flow table with ovs-ofctl, and every number in section 12 came out of
a real run of this code.


-------------------------------------------------------------------------
2. WHAT IT DEMONSTRATES
-------------------------------------------------------------------------

    TCP client/server communication
    Multiple content servers
    Server availability and load monitoring
    Dynamic server selection with a logged reason
    Software-Defined Networking with a real controller
    OpenFlow 1.3 flow installation, matching and rewriting
    Packet-in processing and proxy ARP in the controller
    Failure detection, failover and recovery
    Response time and throughput measurement
    Reproducible results in CSV, a summary table and SVG charts


-------------------------------------------------------------------------
3. REQUIREMENTS
-------------------------------------------------------------------------

    A Linux system with network namespaces.  This project was built and tested
    in Ubuntu 26.04 on WSL2, but any Linux distribution works.

    Mininet 2.3 or newer          (provides mn and the mininet package)
    Open vSwitch 2.13 or newer    (ovs-vswitchd, ovs-vsctl, ovs-ofctl)
    Python 3.8 or newer           (standard library only; verified on 3.14.4)
    root access                   (Mininet creates network namespaces)

There is NO requirements.txt because the project depends on no third-party
Python packages at all.  The OpenFlow protocol layer is implemented in this
repository using only the struct module.

Check your machine with:

    sudo ./scripts/check_env.sh


-------------------------------------------------------------------------
4. QUICK START
-------------------------------------------------------------------------

One command runs everything: content generation, controller, topology, the five
demonstration phases, shutdown, and the report.

From Windows (recommended, no password prompt):

    wsl.exe -u root -d Ubuntu -- bash -lc "cd /home/<user>/smart-content-delivery && ./run_demo.sh"

From inside WSL:

    cd /home/<user>/smart-content-delivery
    sudo ./run_demo.sh

The run takes about two minutes.  It prints progress as it goes and finishes
with either "DEMONSTRATION COMPLETED SUCCESSFULLY" or a list of problems.

If you run it without root it refuses and prints the exact command to use
instead; it never asks for a password itself.


-------------------------------------------------------------------------
5. WHAT THE DEMONSTRATION DOES
-------------------------------------------------------------------------

    PHASE 0  builds the topology, starts the servers and the controller,
             shows the switch connection and the management network.

    PHASE 1  20 sequential requests to the virtual IP.  Every request gets a
             fresh TCP connection, so every request is an independent
             selection decision made by the controller.

    PHASE 2  12 requests at concurrency 3.  Requests overlap on the servers,
             so the load signal is real and connections spread across all
             three servers.

    PHASE 3  five 4 MB transfers, for the throughput figures.

    PHASE 4  kills the content server that was serving traffic.  The
             controller notices within about two seconds, deletes every flow
             that points at that server, and sends new connections elsewhere.
             The demo prints the flow count before and after.

    PHASE 5  restarts the killed server.  It is probed back into the pool and
             receives traffic again.


-------------------------------------------------------------------------
6. WHAT IT PRODUCES
-------------------------------------------------------------------------

    results/results.csv                 one row per request: phase, ok,
                                        server, latency, ttfb, bytes,
                                        throughput, error
    results/summary.md                  per-phase and per-server tables plus
                                        the definitions used
    results/charts/latency_by_phase.svg
    results/charts/throughput_by_server.svg
    results/charts/requests_by_server.svg
    results/logs/controller.log         human-readable controller log
    results/logs/controller_events.jsonl  machine-readable event log
                                        (startup, selection, flow changes,
                                        server up/down, failover)
    results/logs/flows_*.txt            ovs-ofctl dumps for every phase, so
                                        the rule changes can be shown
    results/logs/server_h2.log, ...     content server logs
    results/logs/status_*.json          controller state snapshots
    results/logs/demo.log               the console output of the run

The charts are generated as plain SVG with no plotting library, so they open
in any browser and the project keeps its zero-dependency property.


-------------------------------------------------------------------------
7. NETWORK DESIGN
-------------------------------------------------------------------------

    DATA PLANE - everything here goes through the OpenFlow switch

        h1  client      10.0.0.1     MAC 00:00:00:00:00:01
        h2  server      10.0.0.11    MAC 00:00:00:00:00:11
        h3  server      10.0.0.12    MAC 00:00:00:00:00:12
        h4  server      10.0.0.13    MAC 00:00:00:00:00:13
        virtual IP      10.0.0.100   MAC 00:00:00:00:00:aa  (no host owns it)

                h1
                 |
                s1   Open vSwitch, OpenFlow 1.3, port 6653
               / | \
             h2  h3  h4        one switch port per server

    MANAGEMENT PLANE - no OpenFlow anywhere on this path

              controller (in the root namespace) 10.255.0.254
                        |
                       m1   plain Linux bridge, not an OpenFlow switch
                     / | \
                .11  .12  .13      the same three servers

Why two planes: the controller polls every server's /stats endpoint once a
second.  If those probes travelled through the OpenFlow switch, then a rule the
controller deleted could make a healthy server look dead, and the controller
would react to its own rules instead of to server health.  Keeping probes on a
separate Linux bridge means "the server is down" and "the flow is gone" are
always distinguishable.

The servers bind to 0.0.0.0:8080, so they answer on both interfaces.  The
management address is only ever used for health and load polling.


-------------------------------------------------------------------------
8. REQUEST FLOW, STEP BY STEP
-------------------------------------------------------------------------

 1. The client opens a TCP connection to 10.0.0.100:8080.  It has no idea
    which server will answer.

 2. The client needs the MAC address for 10.0.0.100 and sends an ARP request.
    The switch matches its ARP rule and sends the request to the controller.

 3. The controller answers the ARP itself, claiming 10.0.0.100 is at
    00:00:00:00:00:aa.  No host owns that address; the controller is acting as
    the ARP responder for the virtual service.  This is real packet-in and
    packet-out handling, not a static ARP entry.

 4. The client's SYN arrives at the switch.  It matches the VIP rule, so it is
    sent to the controller (packet-in) with the TCP source port included.

 5. The controller chooses a server for THIS connection: the healthy server
    with the fewest active connections, with ties broken by probe latency and
    then by index.  It logs the decision and the reason.

 6. The controller installs two flows for that one connection:
        request direction: rewrite destination to the chosen server
        reply direction:   rewrite source back to the virtual IP
    then forwards the buffered SYN to the server with the same rewrite.

 7. The switch handles the rest at line rate; the controller is not in the
    data path.  Every further packet of that connection hits the installed
    flows.

 8. The server sees a normal TCP connection and replies.  The switch rewrites
    the reply's source address back to 10.0.0.100 and its source MAC to the
    virtual MAC, so the client's TCP stack sees a consistent peer.

 9. The client receives the content, measures the time, and records the server
    identity that the server itself reported in the X-Served-By header.


-------------------------------------------------------------------------
9. THE OPENFLOW RULES
-------------------------------------------------------------------------

These are the rules the controller installs on s1.  They were read back from a
real switch with "ovs-ofctl -O OpenFlow13 dump-flows s1".

  PRIORITY 200  ARP to the controller
      match:   arp
      action:  output to controller (max_len 65535)
      purpose: the controller answers every ARP request, for the virtual IP
               and for the real hosts, so the client can reach the VIP and the
               servers can reach the client.

  PRIORITY 200  new connections to the VIP to the controller
      match:   tcp, nw_dst=10.0.0.100, tp_dst=8080
      action:  output to controller (max_len 128, headers only)
      purpose: every new TCP connection is a fresh decision.  128 bytes is
               enough for the Ethernet, IP and TCP headers.

  PRIORITY 300  per-connection request rule (one per client connection)
      match:   tcp, in_port=1, nw_dst=10.0.0.100, tp_src=<client port>,
               tp_dst=8080
      actions: set_field 00:00:00:00:00:11 -> eth_dst
               set_field 10.0.0.11        -> ip_dst
               output:2
      purpose: pin this connection to this server.  The idle timeout is 60
               seconds, so the table cleans itself up.

  PRIORITY 300  per-connection reply rule
      match:   tcp, in_port=2, nw_src=10.0.0.11, tp_src=8080,
               tp_dst=<client port>
      actions: set_field 00:00:00:00:00:aa -> eth_src
               set_field 10.0.0.100        -> ip_src
               output:1
      purpose: make the reply look like it came from the virtual service.

  PRIORITY 200  static reply scaffolding (one per server)
      match:   tcp, in_port=2, nw_src=10.0.0.11, tp_src=8080
      actions: set_field 00:00:00:00:00:aa -> eth_src
      purpose: a safety net; the per-connection rule above does the real work.

  PRIORITY 0    table miss
      match:   (empty)
      action:  drop, with counters
      purpose: nothing is forwarded unless the controller installed a rule.
               This is what makes the demonstration strict: you can show a
               packet count on the drop rule and a packet count on the rules
               that carry real traffic.

Every rule carries a cookie: 0x1a | kind | server index + 1 | sequence.
Bits 63-56 are the project tag, 55-48 the rule kind (1 request, 2 reply,
3 ARP), 47-40 the server, and the rest a sequence number.  This is what makes
failure handling a single operation: to remove a dead server from the data
path, the controller sends one flow-mod delete with that cookie and a mask that
ignores the kind byte and the sequence.

Cookies for rules that belong to no server (ARP, VIP catch-all) use a server
byte of 0, which no real server uses.  Getting this wrong is subtle: an early
version encoded the ARP rule with the same server byte as h2, so killing h2
deleted the ARP rule and the whole data path died.  The current code refuses to
delete with an invalid server index.


-------------------------------------------------------------------------
10. SERVER SELECTION
-------------------------------------------------------------------------

The rule, in one sentence:

    Pick the healthy server with the fewest active connections; break ties on
    the lowest measured probe round-trip time, then on the lowest index.

Two details make it behave sensibly in practice.

Bucketed latency.  Probe round-trip times on a virtual network differ by
fractions of a millisecond between probes.  If the tie-break used the raw
value, the winner would change on almost every probe and the demonstration
would look like random assignment.  Latency is therefore bucketed at 2 ms, so
only a real difference moves traffic.

Recent-steer accounting.  The health monitor samples once a second, so a burst
of simultaneous connections would all see "0 active" on every server and all
land on the same machine.  The controller therefore also counts the
connections it steered in the last 0.5 seconds, and uses whichever count is
higher.  This is why concurrency 3 reliably spreads one connection to each
server.

A real decision, from results/logs/controller.log:

    selection: initial selection: h2 has the lowest load (0 active, 1.67 ms)
    selection: initial selection: h3 has the lowest load (0 active, 2.10 ms)
    selection: initial selection: h4 has the lowest load (0 active, 2.44 ms)

The policy itself is a pure function in controller/selector.py, so it is fully
unit-tested without starting Mininet (tests/test_selector.py, 13 tests).


-------------------------------------------------------------------------
11. FAILURE DETECTION AND FAILOVER
-------------------------------------------------------------------------

Detection: every server is polled once a second over the management bridge.
Two consecutive failures mark it down, so detection takes about two seconds.

Failover: when a server goes down the controller

    1. sends one flow-mod delete with that server's cookie and mask, which
       removes every request and reply rule pointing at it,
    2. drops the server from the selection pool,
    3. logs a failover event.

New connections then go to a healthy server.  Connections that were already
established to the dead server die with it, which is the honest behaviour of a
real load balancer; the demonstration uses sequential requests around the kill
so the measured failover shows no request failures.

Recovery: when the restarted server answers two probes it re-enters the pool
and can receive traffic again.

Observed in the passing end-to-end run:

    killed server: h4
    flows for h4: 21 before failure, 0 after
    failover phase: 10 requests, 0 failures, served by h2 and h3
    recovery phase: 10 requests, served by h2, h3 and h4 again


-------------------------------------------------------------------------
12. MEASUREMENTS
-------------------------------------------------------------------------

Definitions, applied identically to every request:

    latency_ms        from the start of the TCP connect to the last byte of
                      the response body
    ttfb_ms           from the start of the connect to the first byte of the
                      body
    throughput_mbps   body_bytes * 8 / (first byte -> last byte) / 1e6

Throughput deliberately excludes connection setup and server think time, so
the figure describes the transfer itself.  Latency includes both, which is why
the latency column is dominated by the 150 ms simulated server delay.

Results from the last verified run (results/summary.md):

    phase        requests  ok  failed  mean latency  p95 latency  servers
    baseline           20  20       0      192.21 ms    202.86 ms  h2,h3,h4
    load               12  12       0      200.08 ms    240.21 ms  h2,h3,h4
    throughput          5   5       0      235.31 ms    245.43 ms  h2,h3,h4
    failover           10  10       0      198.80 ms    205.32 ms  h2,h3
    recovery           10  10       0      197.56 ms    202.43 ms  h2,h3,h4

    throughput phase, per server: h2 1295, h3 1640, h4 855 Mbps

Honest limitations of these numbers:

    - The transfer rates are limited by virtual Ethernet inside one kernel,
      not by any real network.  They are meaningful for comparison between
      servers and between phases, not as internet-grade bandwidth.
    - The 150 ms server delay is simulated (--delay-ms) so that concurrent
      requests overlap and load is observable.  It is documented, not hidden.
    - Failures are induced by killing the server process, not by a physical
      fault.
    - The client is a scripted TCP client, not a browser, and the content is
      static files.


-------------------------------------------------------------------------
13. DIRECTORY LAYOUT
-------------------------------------------------------------------------

    smart-content-delivery/
        README.txt                 this file
        DEMO.md                    demonstration script and viva questions
        config.py                  addressing plan and every tuning value
        topo.py                    Mininet topology, management bridge,
                                   switch port discovery
        demo.py                    the five demonstration phases
        run_demo.sh                the one command that runs everything
        controller/
            of13.py                OpenFlow 1.3 encoder/decoder (struct only)
            sdn_controller.py      the controller: handshake, ARP, flow
                                   installation, failover, control API
            selector.py            selection policy (pure, unit-tested)
            monitor.py             health and load probes
        servers/
            content_server.py      /content, /health, /stats
            content/               small.txt (4 KB), large.bin (4 MB)
        client/
            client.py              measuring TCP client, writes CSV
        scripts/
            make_content.py        deterministic sample content
            summarize.py           CSV + events -> summary.md
            make_charts.py         CSV -> SVG charts
            check_env.sh           environment verification
            show_flows.sh          readable flow table with cookie decoding
        tests/
            test_of13.py           15 wire-format tests
            test_selector.py       13 selection-policy tests
            check_results.py       asserts a completed run's artifacts
            test_e2e.sh            full run plus assertions
        results/                   generated at run time
        state/                     runtime state


-------------------------------------------------------------------------
14. FILE BY FILE
-------------------------------------------------------------------------

    config.py
        One source of truth for addresses, ports, MACs, timings and paths.
        Changing the topology means changing this file.

    controller/of13.py
        The OpenFlow 1.3 wire protocol: HELLO, ECHO, FEATURES, PACKET_IN,
        PACKET_OUT, FLOW_MOD, BARRIER, plus Ethernet/ARP helpers.  Written
        with struct only.  Unit-tested against the byte layout in the spec.

    controller/sdn_controller.py
        The controller itself.  Accepts the switch connection, installs base
        rules, answers ARP, decides per connection, installs flow pairs,
        polls servers, deletes a dead server's flows, and exposes a small
        HTTP control API on 127.0.0.1:9100 (/status, /ports, /healthz).

    controller/selector.py
        The policy: healthy only, fewest active connections, 2 ms latency
        buckets, lowest index.  Returns the chosen server plus a human
        readable reason, which is what gets logged.

    controller/monitor.py
        Polls http://<mgmt ip>:8080/stats once per second, records probe
        latency and server-reported load, and reports up/down transitions.

    topo.py
        Builds s1, h1..h4, the management bridge m1, discovers the OpenFlow
        port number of each host, publishes the port map to the controller and
        starts the three content servers inside their own namespaces.

    demo.py
        The five phases, the flow dumps, the status snapshots and the final
        pass/fail decision.

    servers/content_server.py
        Threaded HTTP server.  /content/<file> serves content and reports its
        identity in X-Served-By / X-Served-IP; /health is liveness; /stats
        returns active connections, total served, uptime and pid.  Probe
        endpoints are excluded from the active count.

    client/client.py
        Measure one request per TCP connection, with optional concurrency,
        writing one CSV row per request and printing a summary table.

    scripts/summarize.py, scripts/make_charts.py
        Turn the raw CSV and event log into the tables and charts.


-------------------------------------------------------------------------
15. COMMANDS
-------------------------------------------------------------------------

    Full demonstration (about 2 minutes, root):

        wsl.exe -u root -d Ubuntu -- bash -lc "cd /home/<user>/smart-content-delivery && ./run_demo.sh"

    Unit tests (no root needed, instant):

        python3 -m unittest discover -s tests -q

    End-to-end acceptance test (about 3 minutes, root): runs the whole
    demonstration and then asserts on the artifacts it produced:

        wsl.exe -u root -d Ubuntu -- bash -lc "cd /home/<user>/smart-content-delivery && ./tests/test_e2e.sh"

    Environment check:

        sudo ./scripts/check_env.sh

    Inspect the flow table while a demonstration is running:

        ./scripts/show_flows.sh            summary plus full table
        ./scripts/show_flows.sh --short    summary only

    Interactive topology for manual exploration (root):

        python3 -m topo --interactive

    Controller status while it runs:

        curl -s http://127.0.0.1:9100/status | python3 -m json.tool


-------------------------------------------------------------------------
16. TROUBLESHOOTING
-------------------------------------------------------------------------

  "This demonstration must run as root"
      Mininet needs network namespaces.  Use the wsl.exe command in section 4
      or sudo.  Do not try to work around it.

  "controller did not start"
      Check results/logs/controller.log.  The usual cause is that a previous
      run is still holding port 6653 or 9100:

          sudo pkill -f "controller.sdn_controller"
          sudo pkill -f content_server.py
          sudo ip link del m1

  "switch is not connected to the controller"
      The switch connects to 127.0.0.1:6653.  If the controller is not
      listening, the switch retries; check the log.  Also confirm the bridge
      protocol is OpenFlow 1.3:

          sudo ovs-vsctl get bridge s1 protocols

  Requests fail with "No route to host"
      That is ARP failing, which means the ARP rule or the ARP responder is
      not working.  Check that the ARP rule exists and is counting packets:

          ./scripts/show_flows.sh --short

      If the count is not increasing, the controller is not receiving
      packet-ins; check results/logs/controller.log.

  Requests fail with "timed out"
      The SYN reached the switch but nothing answered.  Check that the
      per-connection rules were installed and that the server is actually
      listening on its data address:

          sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=300

  Flow mods rejected by the switch
      The controller logs the OpenFlow error with its type and code.  Two
      layout rules matter: the match length field excludes padding, and delete
      requests must use OFPP_ANY (0xffffffff), not OFPP_MAX.

  Leftover state after a crash
      ip link del m1; ovs-vsctl del-br s1; pkill -f content_server.py
      Then run again.  The demonstration is idempotent: it recreates
      everything it needs.

  Charts missing
      scripts/make_charts.py only draws when there is at least one successful
      request.  Run the demonstration first.

  WSL2 notes
      Mininet works in WSL2 because it uses namespaces, not virtual machines,
      and the Open vSwitch datapath used here is the in-kernel one.  Nothing
      needs systemd.  Root is reached with "wsl.exe -u root", which does not
      prompt for a password.


-------------------------------------------------------------------------
17. DESIGN DECISIONS AND LIMITATIONS
-------------------------------------------------------------------------

Why a custom controller instead of Ryu or OS-Ken
    This machine has only Python 3.14, and the OS-Ken package could not be made
    to work there: its metadata reported no entry points, the manager command
    did not exist, and os_ken.ofproto failed to import.  Rather than depend on
    a package that was not functional, the OpenFlow 1.3 protocol layer is
    implemented here in about 300 lines of standard library code.  The result
    is real OpenFlow: the controller speaks the wire protocol to Open vSwitch,
    and every forwarding change is a flow-mod the switch accepted.  It is also
    easier to defend in a viva, because every byte of the protocol can be
    explained from this repository.

Why per-connection rules instead of one rule for the whole VIP
    A single rule per VIP can only ever point at one server at a time, which is
    a load balancer that moves all traffic whenever it feels like it.  Deciding
    per connection gives genuine load spreading: at concurrency 3 the three
    simultaneous connections go to three different servers, and the flow table
    shows one rule pair per connection.

What is real
    The switch, the controller, the OpenFlow channel, the flow modifications,
    the packet-in and ARP handling, the failover decision, the measurements.

What is simulated
    Server-side processing delay (150 ms, so concurrency is observable),
    failures induced by killing a process, static content, and a scripted
    client instead of a browser.

Known limitations
    - One switch, so there is no path diversity; "network condition" is probe
      latency plus server load.
    - Virtual Ethernet throughput numbers are comparative, not absolute.
    - The management plane is a plain Linux bridge, deliberately outside the
      SDN data path.
    - The code uses only the standard library and needs Python 3.8 or newer;
      it was developed and verified on 3.14.4, which is what this machine has.


-------------------------------------------------------------------------
18. VIVA QUESTIONS AND SHORT ANSWERS
-------------------------------------------------------------------------

  What makes this SDN and not a load balancer wearing an SDN costume?
      The forwarding decision is not made by a proxy sitting in the traffic
      path.  The controller speaks OpenFlow to the switch and installs match
      and action rules; the switch then forwards packets itself.  You can show
      the rule in ovs-ofctl dump-flows and watch its packet counter increase
      while the controller is idle.

  Where exactly does the controller make the decision?
      On the first packet of each TCP connection to 10.0.0.100:8080.  That
      packet is sent to the controller as a packet-in; the controller picks a
      server, installs two flows, and forwards that first packet.

  How does the client reach an IP address that no host owns?
      The controller answers ARP for 10.0.0.100 with its own virtual MAC.  The
      client sends frames to that MAC and the switch rewrites them to the real
      server.

  Why rewrite the source on the reply?
      The client's TCP connection is to 10.0.0.100.  If replies arrived from
      10.0.0.11 the client's TCP stack would reject them, because the source
      address would not match the connection.  The reply rule rewrites the
      source address and MAC back to the virtual service.

  How is load measured, and why also count recent steers?
      Each server reports its active connections and total served on /stats.
      The controller polls once a second; a 1 Hz sample cannot see a burst of
      simultaneous connections, so the controller also counts the connections
      it steered in the last half second.  The higher of the two is used.

  What happens if the chosen server dies mid-connection?
      That connection breaks; the server is gone.  The controller detects the
      failure within about two seconds, deletes every flow pointing at it, and
      new connections are steered to a healthy server.  This is stated openly
      rather than hidden by retries.

  How do you know the flows were really deleted?
      The demo dumps the flow table before and after the kill, and the
      acceptance test counts rules by cookie and requires zero for the dead
      server.  It also requires that no successful request in the failover
      phase was served by that server.

  Why two networks, data and management?
      So that health probes are never affected by the forwarding rules the
      controller is changing.  Without it, deleting a flow could make a
      healthy server look dead.

  What is the table-miss rule and what does it prove?
      An empty match with a drop action.  It proves that nothing is forwarded
      unless the controller installed a rule: the counters on that rule and on
      the real rules together tell the whole story.

  What would you change to scale this up?
      Move the decision to a group table with selection buckets instead of one
      flow per connection, add a second switch and multiple paths so path
      choice becomes meaningful, and use conntrack-based NAT instead of
      per-connection address rewriting.  The measurement harness would not
      change.
