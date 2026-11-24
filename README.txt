SMART CONTENT DELIVERY SYSTEM - SIMPLIFIED MINI PROJECT
=======================================================

One Python file (simple_scdn.py, ~595 lines including comments) that builds a
small network with Mininet, programs a real Open vSwitch with real OpenFlow 1.3
rules, spreads client requests over three web servers, kills one server,
re-routes traffic away from it, brings it back, and writes the measurements to
a CSV file.

No controller framework (no Ryu), no database, no dashboard, no pip packages.
Just Mininet + Open vSwitch + Python 3's standard library.

See howtorun.txt for the exact commands.


1. WHAT THE PROJECT DEMONSTRATES
--------------------------------
  * A Software Defined Network: forwarding is decided by rules inside the
    switch, not by the client and not by the servers.
  * Load distribution over three servers.
  * Failure detection by the client, then failover by changing a rule.
  * Recovery: the repaired server rejoins without touching anything else.
  * Request success rate, response latency, and throughput, saved to CSV.

The important part is that these are not simulated in Python. The script
installs and replaces rules in an Open vSwitch, and the traffic really moves.
You can prove it from the switch's own packet counters and from each web
server's own access log.


2. TOPOLOGY
-----------
            h1 (client)
                 |
                s1      Open vSwitch, OpenFlow 1.3, controller=None
              / |  \
            h2  h3  h4   three content servers, each a python http.server

  Host  IP address     MAC address        Role
  ----  -------------  -----------------  --------------------------------
  h1    10.0.0.1/24    00:00:00:00:00:01  client, sends HTTP requests
  h2    10.0.0.11/24   00:00:00:00:00:11  content server, owns the VIP too
  h3    10.0.0.12/24   00:00:00:00:00:12  content server, owns the VIP too
  h4    10.0.0.13/24   00:00:00:00:00:13  content server, owns the VIP too
  -    10.0.0.100/32   -                  the VIP the client asks for

  Switch ports: h1=1, h2=2, h3=3, h4=4.


3. THE ONE IDEA THAT MAKES IT WORK
----------------------------------
The client always asks for the same address: the VIP 10.0.0.100.
All three servers have that address configured as a /32 on their own
interface, so any of them can answer.

Then how does the switch pick one? Through ARP.

  * The client is given a permanent ARP entry saying "10.0.0.100 is at
    00:00:00:00:00:aa". That MAC address belongs to nobody.
  * So every packet for the VIP arrives at s1 addressed to a MAC that no host
    owns. It can only go where a rule sends it.
  * One rule decides:

        priority=100, ip, nw_dst=10.0.0.100
            actions = set_field:<server MAC> -> eth_dst, output:<server port>

    The switch rewrites the destination MAC to a real server's MAC and sends
    the packet out of that server's port.

Point that rule at h2 and h2 answers. Point it at h4 and the exact same client
request is answered by h4. The client is never modified. That is the whole
point of an SDN: forwarding is data (rules), not code inside the endpoints.

Note what the rule does NOT do: it does not rewrite IP addresses or TCP ports,
so no checksums change and there are no checksum-offloading surprises.


4. THE FLOW TABLE
-----------------
Six rules in total. Only one of them changes during the demo.

  priority  match                                action
  --------  -----------------------------------  ----------------------------
  100       ip, nw_dst=10.0.0.100               set_field:<server MAC>->eth_dst
                                                 + output:<server port>
                                                 ^ THE rule: re-pointed to pick
                                                   which server answers
  50        dl_dst=<MAC of h1/h2/h3/h4>         output:<that host's port>
                                                 (4 rules: return traffic and
                                                  ARP replies find their host)
  10        arp                                  FLOOD
                                                 (ARP is broadcast, so it is
                                                  flooded to everybody)

The switch runs with fail_mode="secure" and controller=None, so there is no
learning-switch fallback: a packet that matches no rule is dropped. That is
what makes Step 1 of the demo meaningful.

All rules are installed with plain Open vSwitch commands:

    ovs-ofctl -O OpenFlow13 add-flow s1 "<rule>"
    ovs-ofctl -O OpenFlow13 del-flows s1 "ip,nw_dst=10.0.0.100"
    ovs-ofctl -O OpenFlow13 dump-flows s1

Mininet builds the topology and gives us the namespaces; it does not decide
any forwarding.


5. WHAT THE DEMO DOES, STEP BY STEP
-----------------------------------
  0. Build h1 + s1 + h2/h3/h4, give every server the VIP, set h1's ARP entry,
     start a web server inside each server, and start the client inside h1.
  1. Empty flow table -> the request times out. Proof that without rules
     there is no service at all.
  2. Install the 5 static rules and point the VIP rule at h2 -> the request
     succeeds and the flow dump shows the rule's packet counter going up.
  3. Load distribution: before each of 9 requests the VIP rule is re-pointed,
     so requests go h2, h3, h4, h2, h3, h4 ... Every response names the server
     that produced it, so the script can check that the server it aimed at is
     the server that answered.
  4. Throughput: the same mechanism, one 8 MiB download per server.
  5. Failure: kill h3's web server. The VIP rule is deliberately left pointing
     at h3, so the next request fails. That failure is the "detection".
  6. Failover: re-point the VIP rule only at the healthy servers (h2/h4). Six
     requests succeed while h3 is still dead. The client is not changed.
  7. Recovery: restart h3's web server and put h3 back in the rotation. It
     serves again immediately.

Then it prints a summary, cross-checks the request counts against each web
server's own access log, and writes results/simple_scdn_results.csv.


6. RESULTS FROM A REAL RUN
--------------------------
Measured on Ubuntu 26.04.1 LTS on WSL2, Mininet 2.3.0, Open vSwitch 3.7.1,
Python 3.14.4. Your numbers will differ a little; the pattern will not.

  phase          requests  succeeded  mean latency
  -------------  --------  ---------  ------------
  no-rules              1          0   (2.5 s timeout)
  baseline              1          1   15.2 ms
  distribution          9          9    3.1 ms
  throughput            3          3   23.6 ms
  failure               1          0    0.3 ms  (ConnectionRefusedError)
  failover              6          6    1.9 ms
  recovery              6          6    2.5 ms

  requests answered by each server:  h2 = 10, h3 = 6, h4 = 9
  independent count, from the servers' own access logs: h2 = 10, h3 = 6, h4 = 9

  throughput (one 8 MiB file each):  h2 = 418.7 MB/s, h3 = 255.2 MB/s,
                                     h4 = 465.5 MB/s

The two counting methods agree, and that is the strongest evidence in the whole
project: the switch's rules moved the traffic, and the servers' own logs saw
exactly the requests the rules said they would see.

Why 10/6/9 and not 9/9/9? The throughput step also points the rule at each
server once (3 more requests), plus 1 baseline request on h2, so h2 ends with
1 + 3 + 6 = 10. Only 9 of them are the "distribution" step.


7. FILES
--------
  simple_scdn.py                      the whole project
  results/simple_scdn_results.csv     written at the end of every run
  README.txt                          this file
  howtorun.txt                        commands, expected output, troubleshooting

  Created at run time and safe to delete:
  /tmp/simple_scdn/                   web pages, per-server access logs,
                                      client error log (wiped each run)

That is the entire project. The old multi-file version (controller, management
bridge, charts, tests) is not used by simple_scdn.py in any way; it lives on in
this folder only until you delete it.


8. REQUIREMENTS
---------------
  * Linux with Mininet, Open vSwitch, Python 3. WSL2 on Windows is fine.
  * Root (sudo), because Mininet creates network namespaces.
  * Tested on Ubuntu 26.04.1 LTS, Mininet 2.3.0, OVS 3.7.1 (OpenFlow 1.0 to
    1.6 supported; this project uses only 1.3), Python 3.14.4.
  * No pip installs. Standard library only: csv, http.client, json, os, re,
    shutil, subprocess, sys, time.


9. RESULTS FILE FORMAT
----------------------
results/simple_scdn_results.csv, one row per request, 8 columns:

  phase            which demo step: no-rules, baseline, distribution,
                   throughput, failure, failover, recovery
  expected_server  the server the VIP rule pointed at for this request
  served_by        the server that really answered (from the response body)
  ok               True/False
  latency_ms       request to full response, in milliseconds
  bytes            response size in bytes
  throughput_mbps  MB/s, only for the throughput rows
  error            exception name if the request failed, e.g. TimeoutError

A row where expected_server is not equal to served_by would mean the rule did
not do what it claimed. The script prints "<-- WRONG SERVER" if that happens;
in the tested runs it never does.


10. WHAT IT DOES NOT DO (DON'T OVERCLAIM)
-----------------------------------------
  1. The "load balancing" is per-request round-robin: the script re-points the
     one VIP rule before each request. It is not concurrent ECMP and not a
     hash-based group, so a single long TCP connection stays on one server.
     It is still real rule-driven distribution, which is what is being shown.
  2. Failure detection lives in the demo script, not in an always-on control
     loop: the failed request is what reveals the dead server, then the rule is
     changed. That is the price of having no controller.
  3. Latency is client-side request-to-response time. It includes the TCP
     handshake and the Python web server's work (~1.5 ms warm, ~15 ms on the
     very first request). It is not a pure network round-trip time.
  4. Throughput numbers are memory-bound. 8 MiB between virtual hosts on a
     veth pair measures memory bandwidth (hundreds of MB/s), not what a real
     Ethernet link would do. It proves the data path works and identifies
     which server sent the bytes.
  5. Nothing is encrypted or authenticated; the "content" is a text file that
     says "served by h2" plus 8 MiB of random bytes.


11. WSL2 NOTES
--------------
  * Run with sudo. Without root, Mininet cannot create namespaces.
  * Mininet hosts share one PID namespace, so "pkill -f http.server" would kill
    all three servers at once. The script starts each server, records its PID,
    and kills only that PID.
  * If ovs-vsctl says "database connection failed" after a Windows reboot, the
    OVS service is not up yet: sudo service openvswitch-switch start
  * Keep the project in the Linux filesystem (~/...), not under /mnt/c. It
    works from /mnt/c but is slower and mixes Windows/Linux permissions.
  * Everything happens inside WSL's own namespaces, so no Windows firewall or
    port forwarding is involved and nothing conflicts with Windows.
  * If a previous run was interrupted (Ctrl-C), it can leave bridge s1 behind.
    The script deletes that automatically at startup, or do it by hand:
        sudo ovs-vsctl --if-exists del-br s1


12. QUESTIONS YOU MAY BE ASKED
------------------------------
  Q: Where is the controller?
  A: There isn't one. The rules are installed directly with ovs-ofctl. A
     controller would only add a protocol and a process; the switch behaviour
     would be identical.

  Q: Why do all three servers have the same IP?
  A: That is what makes "which server answers" a pure forwarding decision. The
     VIP is a /32 on all three; the switch's rule decides who receives each
     packet. The client cannot tell them apart, which is the point.

  Q: How do you know the rule really forwarded the traffic?
  A: Two independent ways: the switch's per-rule n_packets counter (copied
     into the printed flow dumps) and each server's own access log. Both match
     the script's bookkeeping. With the flow table empty, the same request
     times out.

  Q: What happens to the packets when h3 dies?
  A: They still go to h3, because the rule still points there, and the client
     gets connection-refused. After the rule is re-pointed, the same client
     request succeeds on h2 or h4. Nothing on the client changes.

  Q: Why does the flow dump sometimes show n_packets=0 right after a request?
  A: Open vSwitch copies counters from the kernel datapath about once a second.
     The script waits one second before dumping so the numbers are real.

  Q: How would this scale to a real network?
  A: A controller would watch server health (e.g. probes) and install the same
     rule, and a load-balancer group with select() would replace the manual
     round-robin. The forwarding idea stays exactly the same.
