import os
import abc
import sys
import time
import dpkt
import signal
import socket
import logging
import fnconfig
import fnpacket
import threading
import subprocess
from debuglevels import *
from collections import namedtuple
from collections import OrderedDict


class DivertParms(object):
    """Class to abstract all criteria possible out of the respective diverters.

    These criteria largely derive from both the diverter state and the packet
    contents. It seems more ideal to create this friend class for DiverterBase
    than to load down the fnpacket.PacketCtx abstraction with extraneous
    concepts.

    Many of these critera are only applicable if the transport layer has
    been parsed and validated.
    """

    def __init__(self, diverter, pkt):
        self.diverter = diverter
        self.pkt = pkt

    @property
    def is_loopback0(self):
        return (self.pkt.src_ip0 == self.pkt.dst_ip0 ==
                self.diverter.loopback_ip)

    @property
    def is_loopback(self):
        return self.pkt.src_ip == self.pkt.dst_ip == self.diverter.loopback_ip

    @property
    def dport_hidden_listener(self):
        """Does the destination port for the packet correspond to a hidden
        listener (i.e. should the packet be redirected to the proxy)?

        Returns:
            True if dport corresponds to hidden listener, else False
        """
        return self.diverter.diverted_ports.get(self.pkt.dport) is True

    @property
    def src_local(self):
        """Is the source address one of the local IPs of this system?

        Returns:
            True if local source IP, else False
        """
        return self.pkt.src_ip in self.diverters.ip_addrs[self.pkt.ipver]

    @property
    def sport_bound(self):
        """Is the source port bound by a FakeNet-NG listener?

        Returns:
            True if sport is bound by FakeNet-NG, else False
        """
        return (self.pkt.sport in
                self.diverter.diverted_ports.get(self.pkt.proto_name))

    @property
    def dport_bound(self):
        """Is the destination port bound by a FakeNet-NG listener?

        Returns:
            True if dport is bound by FakeNet-NG, else False
        """
        return (self.pkt.dport in
                self.diverter.diverted_ports.get(self.pkt.proto_name))

    @property
    def first_packet_new_session(self):
        """Is this the first datagram from this conversation?

        Returns:
            True if this pair of endpoints hasn't conversed before, else False
        """
        return not (self.diverter.sessions.get(self.pkt.sport) ==
                    (self.pkt.dst_ip, self.pkt.dport))


class DiverterPerOSDelegate(object):
    """Delegate class for OS-specific methods that FakeNet-NG implementors must
    override.

    TODO: The following methods may need to be combined to ensure that there is
    at least a single Ethernet interface with all valid settings (instead of,
    say, several interfaces, each with only one of the components that are
    needed to make the system work).
        check_active_ethernet_adapters
        check_ipaddresses
        check_gateways (currently only a warning)
        check_dns_servers (currently only a warning)
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def check_active_ethernet_adapters(self):
        """Check that there is at least one Ethernet interface.
        
        Returns:
            True if there is at least one interface, else False
        """
        pass

    @abc.abstractmethod
    def check_ipaddresses(self):
        """Check that there is at least one non-null IP address associated with
        at least one interface.
        
        Returns:
            True if at least one IP address, else False
        """
        pass

    @abc.abstractmethod
    def check_gateways(self):
        """Check that at least one interface has a non-NULL gateway set.
        
        Returns:
            True if at least one gateway, else False
        """
        pass

    @abc.abstractmethod
    def fix_gateway(self):
        """Check if there is a gateway configured on any of the Ethernet
        interfaces. If not, then locate a configured IP address and set a gw
        automatically. This is necessary for VMware's Host-Only DHCP server
        which leaves the default gateway empty.

        Returns:
            True if successful, else False
        """
        pass

    @abc.abstractmethod
    def check_dns_servers(self):
        """Check that a DNS server is set.

        Returns:
            True if a DNS server is set, else False
        """
        pass

    @abc.abstractmethod
    def fix_dns(self):
        """Check if there is a DNS server on any of the Ethernet interfaces. If
        not, then locate configured IP address and set a DNS server
        automatically.

        Returns:
            True if successful, else False
        """
        pass

    @abc.abstractmethod
    def get_pid_comm(self, pkt):
        """Get the PID and process name by IP/port info.

        NOTE: the term "comm" is short for "command" and comes from the Linux
        term for process name within task_struct and displayed in ps.

        Args:
            pkt: A fnpacket.PacketCtx or derived object

        Returns:
            Tuple of length 2, containing:
                (pid, comm)
            If the pid or comm cannot be discerned, the corresponding member of
            the tuple will be None.
        """
        pass

    @abc.abstractmethod
    def getNewDestinationIp(self, src_ip):
        """Get IP to redirect to after a redirection decision has been made.

        This is OS-specific due to varying empirical results on Windows and
        Linux, and may be subject to change.

        On Windows, and possibly other operating systems, simply redirecting
        external packets to the loopback address will cause the packets not to
        be routable, so it is necessary to choose an external interface IP in
        some cases.

        Contrarywise, the Linux FTP tests will fail if all redirections are not
        routed to 127.0.0.1.

        Args:
            src_ip: A str of the source IP address represented in ASCII

        Returns:
            A str of the destination IP address represented in ASCII that the
            packet should be redirected to.
        """
        pass


class DiverterBase(fnconfig.Config):


    def __init__(self, diverter_config, listeners_config, ip_addrs,
                  logging_level=logging.INFO):
        """Initialize the DiverterBase.

        TODO: Replace the sys.exit() calls from this function with exceptions
        or some other mechanism appropriate for allowing the user of this class
        to programmatically detect and handle these cases in their own way.
        This may entail moving configuration parsing to a method with a return
        value, or modifying fakenet.py to handle Diverter exceptions.

        Args:
            diverter_config: A dict of [Diverter] config section
            listeners_config: A dict of listener configuration sections
            ip_addrs: dictionary keyed by integers 4 and 6, with each element
                being a list and each list member being a str that is an ASCII
                representation of an IP address that is associated with a local
                interface on this system.
            logging_level: Optional integer logging level such as logging.DEBUG

        Returns:
            None
        """
        # For fine-grained control of subclass debug output. Does not control
        # debug output from DiverterBase. To see DiverterBase debug output,
        # pass logging.DEBUG as the logging_level argument to init_base.
        self.pdebug_level = 0
        self.pdebug_labels = dict()

        self.running_on_windows = False # Override in Windows implementation

        self.pid = os.getpid()

        self.ip_addrs = ip_addrs

        self.pcap = None
        self.pcap_filename = ''
        self.pcap_lock = None

        self.logger = logging.getLogger('Diverter')
        self.logger.setLevel(logging_level)

        portlists = ['BlackListPortsTCP', 'BlackListPortsUDP']
        stringlists = ['HostBlackList']
        self.configure(diverter_config, portlists, stringlists)
        self.listeners_config = dict((k.lower(), v)
                                     for k, v in listeners_config.iteritems())

        # Local IP address
        self.external_ip = socket.gethostbyname(socket.gethostname())
        self.loopback_ip = socket.gethostbyname('localhost')

        # Sessions cache
        # NOTE: A dictionary of source ports mapped to destination address,
        # port tuples
        self.sessions = dict()

        # Manage logging of foreign-destined packets
        self.nonlocal_ips_already_seen = []
        self.log_nonlocal_only_once = True

        # Port forwarding table, for looking up original unbound service ports
        # when sending replies to foreign endpoints that have attempted to
        # communicate with unbound ports. Allows fixing up source ports in
        # response packets. Similar to the `sessions` member of the Windows
        # Diverter implementation.
        self.port_fwd_table = dict()
        self.port_fwd_table_lock = threading.Lock()

        # Track conversations that will be ignored so that e.g. an RST response
        # from a closed port does not erroneously trigger port forwarding and
        # silence later replies to legitimate clients.
        self.ignore_table = dict()
        self.ignore_table_lock = threading.Lock()

        # IP forwarding table, for looking up original foreign destination IPs
        # when sending replies to local endpoints that have attempted to
        # communicate with other machines e.g. via hard-coded C2 IP addresses.
        self.ip_fwd_table = dict()
        self.ip_fwd_table_lock = threading.Lock()

        #######################################################################
        # Listener specific configuration
        # NOTE: All of these definitions have protocol as the first key
        #       followed by a list or another nested dict with the actual
        #       definitions

        # Diverted ports
        # TODO: a more meaningful name might be BOUND ports indicating ports
        # that FakeNet-NG has bound to with a listener
        self.diverted_ports = dict()

        # Listener Port Process filtering
        # TODO: Allow PIDs
        self.port_process_whitelist = dict()
        self.port_process_blacklist = dict()

        # Listener Port Host filtering
        # TODO: Allow domain name resolution
        self.port_host_whitelist = dict()
        self.port_host_blacklist = dict()

        # Execute command list
        self.port_execute = dict()

        # Parse listener configurations
        self.parse_listeners_config(listeners_config)

        #######################################################################
        # Diverter settings

        # Default TCP/UDP listeners
        self.default_listener = dict()

        # Global TCP/UDP port blacklist
        self.blacklist_ports = {'TCP': [], 'UDP': []}

        # Global process blacklist
        # TODO: Allow PIDs
        self.blacklist_processes = []
        self.whitelist_processes = []

        # Global host blacklist
        # TODO: Allow domain resolution
        self.blacklist_hosts = []

        # Parse diverter config
        self.parse_diverter_config()

        slists = ['DebugLevel',]
        self.reconfigure(portlists=[], stringlists=slists)

        dbg_lvl = 0
        if self.is_configured('DebugLevel'):
            for label in self.getconfigval('DebugLevel'):
                label = label.upper()
                if label == 'OFF':
                    dbg_lvl = 0
                    break
                if not label in DLABELS_INV:
                    self.logger.warning('No such DebugLevel as %s' % (label))
                else:
                    dbg_lvl |= DLABELS_INV[label]
        self.set_debug_level(dbg_lvl, DLABELS)

        #######################################################################
        # Network verification - Implemented in OS-specific mixin

        # Check active interfaces
        if not self.check_active_ethernet_adapters():
            self.logger.warning('WARNING: No active ethernet interfaces ' +
                                'detected!')
            self.logger.warning('         Please enable a network interface.')
            sys.exit(1)

        # Check configured ip addresses
        if not self.check_ipaddresses():
            self.logger.warning('ERROR: No interface had IP address configured!')
            self.logger.warning('         Please configure an IP address on a network interface.')
            sys.exit(1)

        # Check configured gateways
        gw_ok = self.check_gateways()
        if not gw_ok:
            self.logger.warning('WARNING: No gateways configured!')
            if self.is_set('fixgateway'):
                gw_ok = self.fix_gateway()
                if not gw_ok:
                    self.logger.warning('Cannot fix gateway')

        if not gw_ok:
            self.logger.warning('         Please configure a default ' +
                                'gateway or route in order to intercept ' +
                                'external traffic.')
            self.logger.warning('         Current interception abilities ' +
                                'are limited to local traffic.')

        # Check configured DNS servers
        dns_ok = self.check_dns_servers()
        if not dns_ok:
            self.logger.warning('WARNING: No DNS servers configured!')
            if self.is_set('fixdns'):
                dns_ok = self.fix_dns()
                if not dns_ok:
                    self.logger.warning('Cannot fix DNS')

        if not dns_ok:
            self.logger.warning('         Please configure a DNS server ' +
                                'in order to allow network resolution.')

        # OS-specific Diverters must initialize e.g. WinDivert,
        # libnetfilter_queue, pf/alf, etc.

    def set_debug_level(self, lvl, labels={}):
        """Enable debug output if necessary, set the debug output level, and
        maintain a reference to the dictionary of labels to print when a given
        logging level is encountered.
        
        Args:
            lvl: An int mask of all debug logging levels
            labels: A dict of int => str assigning names to each debug level

        Returns:
            None
        """
        if lvl:
            self.logger.setLevel(logging.DEBUG)

        self.pdebug_level = lvl

        self.pdebug_labels = labels

    def pdebug(self, lvl, s):
        """Log only the debug trace messages that have been enabled via
        set_debug_level.

        Args:
            lvl: An int indicating the debug level of this message
            s: The mssage

        Returns:
            None
        """
        if self.pdebug_level & lvl:
            label = self.pdebug_labels.get(lvl)
            prefix = '[' + label + '] ' if label else '[some component] '
            self.logger.debug(prefix + str(s))

    def check_privileged(self):
        """UNIXy and Windows-oriented check for superuser privileges.

        Returns:
            True if superuser, else False
        """
        try:
            privileged = (os.getuid() == 0)
        except AttributeError:
            privileged = (ctypes.windll.shell32.IsUserAnAdmin() != 0)

        return privileged

    def parse_listeners_config(self, listeners_config):
        """Parse listener config sections.

        TODO: Replace the sys.exit() calls from this function with exceptions
        or some other mechanism appropriate for allowing the user of this class
        to programmatically detect and handle these cases in their own way.
        This may entail modifying fakenet.py.

        Args:
            listeners_config: A dict of listener configuration sections

        Returns:
            None
        """

        #######################################################################
        # Populate diverter ports and process filters from the configuration
        for listener_name, listener_config in listeners_config.iteritems():

            if 'port' in listener_config:

                port = int(listener_config['port'])

                hidden = (listener_config.get('hidden', 'false').lower() ==
                          'true')

                if not 'protocol' in listener_config:
                    self.logger.error('ERROR: Protocol not defined for ' +
                                      'listener %s', listener_name)
                    sys.exit(1)

                protocol = listener_config['protocol'].upper()

                if not protocol in ['TCP', 'UDP']:
                    self.logger.error('ERROR: Invalid protocol %s for ' +
                                      'listener %s', protocol, listener_name)
                    sys.exit(1)

                if not protocol in self.diverted_ports:
                    self.diverted_ports[protocol] = dict()

                # diverted_ports[protocol][port] is True if the listener is
                # configured as 'Hidden', which means it will not receive
                # packets unless the ProxyListener determines that the protocol
                # matches the listener
                self.diverted_ports[protocol][port] = hidden

                ###############################################################
                # Process filtering configuration
                if 'processwhitelist' in listener_config and 'processblacklist' in listener_config:
                    self.logger.error('ERROR: Listener can\'t have both ' +
                                      'process whitelist and blacklist.')
                    sys.exit(1)

                elif 'processwhitelist' in listener_config:

                    self.logger.debug('Process whitelist:')

                    if not protocol in self.port_process_whitelist:
                        self.port_process_whitelist[protocol] = dict()

                    self.port_process_whitelist[protocol][port] = [
                        process.strip() for process in
                        listener_config['processwhitelist'].split(',')]

                    for port in self.port_process_whitelist[protocol]:
                        self.logger.debug(' Port: %d (%s) Processes: %s',
                                          port, protocol, ', '.join(
                            self.port_process_whitelist[protocol][port]))

                elif 'processblacklist' in listener_config:
                    self.logger.debug('Process blacklist:')

                    if not protocol in self.port_process_blacklist:
                        self.port_process_blacklist[protocol] = dict()

                    self.port_process_blacklist[protocol][port] = [
                        process.strip() for process in
                        listener_config['processblacklist'].split(',')]

                    for port in self.port_process_blacklist[protocol]:
                        self.logger.debug(' Port: %d (%s) Processes: %s',
                                          port, protocol, ', '.join(
                            self.port_process_blacklist[protocol][port]))

                ###############################################################
                # Host filtering configuration
                if 'hostwhitelist' in listener_config and 'hostblacklist' in listener_config:
                    self.logger.error('ERROR: Listener can\'t have both ' +
                                      'host whitelist and blacklist.')
                    sys.exit(1)

                elif 'hostwhitelist' in listener_config:

                    self.logger.debug('Host whitelist:')

                    if not protocol in self.port_host_whitelist:
                        self.port_host_whitelist[protocol] = dict()

                    self.port_host_whitelist[protocol][port] = [host.strip()
                        for host in
                        listener_config['hostwhitelist'].split(',')]

                    for port in self.port_host_whitelist[protocol]:
                        self.logger.debug(' Port: %d (%s) Hosts: %s', port,
                                          protocol, ', '.join(
                            self.port_host_whitelist[protocol][port]))

                elif 'hostblacklist' in listener_config:
                    self.logger.debug('Host blacklist:')

                    if not protocol in self.port_host_blacklist:
                        self.port_host_blacklist[protocol] = dict()

                    self.port_host_blacklist[protocol][port] = [host.strip()
                        for host in
                        listener_config['hostblacklist'].split(',')]

                    for port in self.port_host_blacklist[protocol]:
                        self.logger.debug(' Port: %d (%s) Hosts: %s', port,
                                          protocol, ', '.join(
                            self.port_host_blacklist[protocol][port]))

                ###############################################################
                # Execute command configuration
                if 'executecmd' in listener_config:
                    template = listener_config['executecmd'].strip()

                    # Would prefer not to get into the middle of a debug
                    # session and learn that a typo has ruined the day, so we
                    # test beforehand by
                    test = self._build_cmd(template, 0, 'test', '1.2.3.4',
                                           12345, '4.3.2.1', port)
                    if not test:
                        self.logger.error(('Terminating due to incorrectly ' +
                                          'configured ExecuteCmd for ' +
                                          'listener %s') % (listener_name))
                        sys.exit(1)

                    if not protocol in self.port_execute:
                        self.port_execute[protocol] = dict()

                    self.port_execute[protocol][port] = template
                    self.logger.debug('Port %d (%s) ExecuteCmd: %s', port,
                                      protocol,
                                      self.port_execute[protocol][port])

    def build_cmd(self, pkt, pid, comm):
        """Retrieve the ExecuteCmd directive if applicable and build the
        command to execute.

        """
        cmd = None

        if ((pkt.proto_name in self.port_execute) and
                (pkt.dport in self.port_execute[pkt.proto_name])
           ):
            template = self.port_execute[pkt.proto_name][pkt.dport]
            cmd = self._build_cmd(template, pid, comm, pkt.src_ip, pkt.sport,
                                  pkt.dst_ip, pkt.dport)

        return cmd

    def _build_cmd(self, tmpl, pid, comm, src_ip, sport, dst_ip, dport):
        """Build a command based on the template specified in an ExecuteCmd
        config directive, applying the parameters as needed.

        Args:
            tmpl: A str containing the body of the ExecuteCmd config directive
            pid: An int that is the process ID of the process that sent the
                packet
            comm: The command (process name) that sent the packet
            src_ip: The source IP address that originated the packet
            sport: The source port that originated the packet
            dst_ip: The destination IP that the packet was directed at
            dport: The destination port that the packet was directed at

        Returns:
            A str that is the resultant command to execute
        """
        cmd = None

        try:
            cmd = tmpl.format(
                pid = str(pid),
                procname = str(comm),
                src_addr = str(src_ip),
                src_port = str(sport),
                dst_addr = str(dst_ip),
                dst_port = str(dport))
        except KeyError as e:
            self.logger.error(('Failed to build ExecuteCmd for port %d due ' +
                              'to erroneous format key: %s') %
                              (dport, e.message))

        return cmd

    ###########################################################################
    # Execute process and detach
    def execute_detached(self, execute_cmd):
        """OS-agnostic asynchronous subprocess creation.

        Executes the process via UNIXy methods unless Windows is specified.
        Isolates the process from FakeNet-NG to prevent it from being
        interrupted by termination of FakeNet-NG, Ctrl-C, etc.

        Args:
            execute_cmd: A str that is the command to execute

        Side-effects:
            Creates the specified process.

        Returns:
            An int that is the pid of the new process, else None
        """
        DETACHED_PROCESS = 0x00000008
        cflags = DETACHED_PROCESS if self.running_on_windows else 0
        cfds = False if self.running_on_windows else True
        shl = False if self.running_on_windows else True

        def ign_sigint():
            # Prevent KeyboardInterrupt in FakeNet-NG's console from
            # terminating child processes
            signal.signal(signal.SIGINT, signal.SIG_IGN)

        preexec = None if self.running_on_windows else ign_sigint

        self.logger.error('Preexec = %s' % (str(preexec)))

        try:
            pid = subprocess.Popen(execute_cmd, creationflags=cflags,
                                   shell=shl,
                                   close_fds = cfds,
                                   preexec_fn = preexec).pid
        except Exception as e:
            self.logger.error('Exception of type %s' % (str(type(e))))
            self.logger.error('Error: Failed to execute command: %s', execute_cmd)
            self.logger.error('       %s', e)
        else:
            return pid

    def parse_diverter_config(self):
        """Parse [Diverter] config section.

        Args: N/A

        Side-effects:
            Diverter members (whitelists, pcap, etc.) initialized.

        Returns:
            None
        """
        # SingleHost vs MultiHost mode
        self.network_mode = 'SingleHost'  # Default
        self.single_host_mode = True
        if self.is_configured('networkmode'):
            self.network_mode = self.getconfigval('networkmode')
            available_modes = ['singlehost', 'multihost']

            # Constrain argument values
            if self.network_mode.lower() not in available_modes:
                self.logger.error('NetworkMode must be one of %s' %
                                  (available_modes))
                sys.exit(1)

            # Adjust previously assumed mode if user specifies MultiHost
            if self.network_mode.lower() == 'multihost':
                self.single_host_mode = False

        if self.getconfigval('processwhitelist') and self.getconfigval('processblacklist'):
            self.logger.error('ERROR: Diverter can\'t have both process ' +
                              'whitelist and blacklist.')
            sys.exit(1)

        if self.is_set('dumppackets'):
            self.pcap_filename = '%s_%s.pcap' % (self.getconfigval(
                'dumppacketsfileprefix', 'packets'),
                time.strftime('%Y%m%d_%H%M%S'))
            self.logger.info('Capturing traffic to %s', self.pcap_filename)
            self.pcap = dpkt.pcap.Writer(open(self.pcap_filename, 'wb'),
                linktype=dpkt.pcap.DLT_RAW)
            self.pcap_lock = threading.Lock()

        # Do not redirect blacklisted processes
        if self.is_configured('processblacklist'):
            self.blacklist_processes = [process.strip() for process in
                self.getconfigval('processblacklist').split(',')]
            self.logger.debug('Blacklisted processes: %s', ', '.join(
                [str(p) for p in self.blacklist_processes]))

        # Only redirect whitelisted processes
        if self.is_configured('processwhitelist'):
            self.whitelist_processes = [process.strip() for process in
                self.getconfigval('processwhitelist').split(',')]
            self.logger.debug('Whitelisted processes: %s', ', '.join(
                [str(p) for p in self.whitelist_processes]))

        # Do not redirect blacklisted hosts
        if self.is_configured('hostblacklist'):
            self.blacklist_hosts = self.getconfigval('hostblacklist')
            self.logger.debug('Blacklisted hosts: %s', ', '.join(
                [str(p) for p in self.getconfigval('hostblacklist')]))

        # Redirect all traffic
        self.default_listener = {'TCP': None, 'UDP': None}
        if self.is_set('redirectalltraffic'):
            if self.is_unconfigured('defaulttcplistener'):
                self.logger.error('ERROR: No default TCP listener specified ' +
                                  'in the configuration.')
                sys.exit(1)

            elif self.is_unconfigured('defaultudplistener'):
                self.logger.error('ERROR: No default UDP listener specified ' +
                                  'in the configuration.')
                sys.exit(1)

            elif not self.getconfigval('defaulttcplistener').lower() in self.listeners_config:
                self.logger.error('ERROR: No configuration exists for ' +
                                  'default TCP listener %s', self.getconfigval(
                    'defaulttcplistener'))
                sys.exit(1)

            elif not self.getconfigval('defaultudplistener').lower() in self.listeners_config:
                self.logger.error('ERROR: No configuration exists for ' +
                                  'default UDP listener %s', self.getconfigval(
                                  'defaultudplistener'))
                sys.exit(1)

            else:
                self.default_listener['TCP'] = int(
                    self.listeners_config[self.getconfigval('defaulttcplistener').lower()]['port'])
                self.logger.error('Using default listener %s on port %d', self.getconfigval(
                    'defaulttcplistener').lower(), self.default_listener['TCP'])

                self.default_listener['UDP'] = int(
                    self.listeners_config[self.getconfigval('defaultudplistener').lower()]['port'])
                self.logger.error('Using default listener %s on port %d', self.getconfigval(
                    'defaultudplistener').lower(), self.default_listener['UDP'])

            # Re-marshall these into a readily usable form...

            # Do not redirect blacklisted TCP ports
            if self.is_configured('blacklistportstcp'):
                self.blacklist_ports['TCP'] = \
                    self.getconfigval('blacklistportstcp')
                self.logger.debug('Blacklisted TCP ports: %s', ', '.join(
                    [str(p) for p in self.getconfigval('BlackListPortsTCP')]))

            # Do not redirect blacklisted UDP ports
            if self.is_configured('blacklistportsudp'):
                self.blacklist_ports['UDP'] = \
                    self.getconfigval('blacklistportsudp')
                self.logger.debug('Blacklisted UDP ports: %s', ', '.join(
                    [str(p) for p in self.getconfigval('BlackListPortsUDP')]))

    def write_pcap(self, pkt):
        """Writes a packet to the pcap.

        Args:
            pkt: A fnpacket.PacketCtx or derived object

        Returns:
            None

        Side-effects:
            Calls dpkt.pcap.Writer.writekpt to persist the octets
        """
        if self.pcap and self.pcap_lock:
            self.pcap_lock.acquire()
            try:
                mangled = 'mangled' if pkt.mangled else 'initial'
                self.pdebug(DPCAP, 'Writing %s packet %s' %
                            (mangled, pkt.hdrToStr2()))
                self.pcap.writepkt(pkt.octets)
            finally:
                self.pcap_lock.release()

    def handle_pkt(self, pkt, callbacks3, callbacks4):
        """Generic packet hook.

        Applies FakeNet-NG decision making to packet.

        Args:
            pkt: A fnpacket.PacketCtx child class
            callbacks3: Layer 3 (network) callbacks
            callbacks4: Layer 4 (network) callbacks

        Side-effects:
            1.) Unconditionally Write unmangled packet to pcap
            2.) Call layer 3 (network) callbacks...
            3.) Call layer 4 (transport) callbacks...
            4.) If the packet headers have been modified, double-write the
                mangled packet to the pcap for SSL decoding purposes

        The caller is responsible for checking if the packet was mangled,
        updating the contents of the datagram with the network hooking specific
        to their OS, and accepting/transmitting the final packet.

        Params
        ------
        pkt: fnpacket.PacketCtx object
        callbacks3: Array of L3 (network) callbacks
        callbacks4: Array of L4 (transport) callbacks

        Side-effects:
            Mangles pkt as necessary

        Returns:
            None
        """

        # 1: Unconditionally write unmangled packet to pcap
        self.write_pcap(pkt)

        no_further_processing = False

        if (pkt._hdr, pkt.proto) == (None, None):
            self.logger.warning('%s: Failed to parse IP packet' % (pkt.label))
        else:
            self.pdebug(DGENPKT, '%s %s' % (pkt.label, pkt.hdrToStr()))

            crit = DivertParms(self, pkt)

            # fnpacket has parsed all that can be parsed, so
            pid, comm = self.get_pid_comm(pkt)
            if self.pdebug_level & DGENPKTV:
                logline = self.formatPkt(pkt, pid, comm)
                self.pdebug(DGENPKTV, logline)
            elif pid and (pid != self.pid) and crit.first_packet_new_session:
                self.logger.info('  pid:  %d name: %s' %
                                 (pid, comm if comm else 'Unknown'))

            # 2: Call layer 3 (network) callbacks
            for cb in callbacks3:
                # These debug outputs are useful for figuring out which
                # callback is responsible for an exception that was masked by
                # python-netfilterqueue's global callback.
                self.pdebug(DCB, 'Calling %s' % (cb))

                cb(crit, pkt)

                self.pdebug(DCB, '%s finished' % (cb))

            if pkt.proto_name:

                if len(callbacks4):
                    # Windows Diverter has always allowed loopback packets to
                    # fall where they may. This behavior now applies to all
                    # Diverters.
                    if crit.is_loopback:
                        self.logger.debug('Ignoring loopback packet')
                        self.logger.debug('  %s:%d -> %s:%d', pkt.src_ip, pkt.sport, pkt.dst_ip, pkt.dport)
                        no_further_processing = True

                    # 3: Layer 4 (Transport layer) callbacks
                    if not no_further_processing:
                        for cb in callbacks4:
                            # These debug outputs are useful for figuring out
                            # which callback is responsible for an exception
                            # that was masked by python-netfilterqueue's global
                            # callback.
                            self.pdebug(DCB, 'Calling %s' % (cb))

                            cb(crit, pkt, pid, comm)

                            self.pdebug(DCB, '%s finished' % (cb))

            else:
                self.pdebug(DGENPKT, '%s: Not handling protocol %s' %
                                     (pkt.label, pkt.proto))

        # 4: Double write mangled packets to represent changes made by
        # FakeNet-NG while still allowing SSL decoding with the old packets
        if pkt.mangled:
            self.write_pcap(pkt)

    def formatPkt(self, pkt, pid, comm):
        """Format a packet analysis log line for DGENPKTV.

        Args:
            pkt: A fnpacket.PacketCtx or derived object
            pid: Process ID
            comm: Process executable name

        Returns:
            A str containing the log line
        """
        logline = ''

        if pkt.proto_name == 'UDP':
            fmt = '| {label} {proto} | {pid:>6} | {comm:<8} | {src:>15}:{sport:<5} | {dst:>15}:{dport:<5} | {length:>5} | {flags:<11} | {seqack:<35} |'
            logline = fmt.format(
                    label=pkt.label,
                    proto=pkt.proto_name,
                    pid=pid,
                    comm=comm,
                    src=pkt.src_ip,
                    sport=pkt.sport,
                    dst=pkt.dst_ip,
                    dport=pkt.dport,
                    length=len(pkt),
                    flags='',
                    seqack='',
                )

        elif pkt.proto_name == 'TCP':
            tcp = pkt._hdr.data

            sa = 'Seq=%d, Ack=%d' % (tcp.seq, tcp.ack)

            f = []
            if (tcp.flags & dpkt.tcp.TH_RST) != 0:
                f.append('RST')
            if (tcp.flags & dpkt.tcp.TH_SYN) != 0:
                f.append('SYN')
            if (tcp.flags & dpkt.tcp.TH_ACK) != 0:
                f.append('ACK')
            if (tcp.flags & dpkt.tcp.TH_FIN) != 0:
                f.append('FIN')
            if (tcp.flags & dpkt.tcp.TH_PUSH) != 0:
                f.append('PSH')

            fmt = '| {label} {proto} | {pid:>6} | {comm:<8} | {src:>15}:{sport:<5} | {dst:>15}:{dport:<5} | {length:>5} | {flags:<11} | {seqack:<35} |'
            logline = fmt.format(
                    label=pkt.label,
                    proto=pkt.proto_name,
                    pid=pid,
                    comm=comm,
                    src=pkt.src_ip,
                    sport=pkt.sport,
                    dst=pkt.dst_ip,
                    dport=pkt.dport,
                    length=len(pkt),
                    flags=','.join(f),
                    seqack=sa,
                )
        else:
            fmt = '| {label} {proto} | {pid:>6} | {comm:<8} | {src:>15}:{sport:<5} | {dst:>15}:{dport:<5} | {length:>5} | {flags:<11} | {seqack:<35} |'
            logline = fmt.format(
                    label = pkt.label,
                    proto = 'UNK',
                    pid = pid,
                    comm = comm,
                    src = str(pkt.src_ip),
                    sport = str(pkt.sport),
                    dst = str(pkt.dst_ip),
                    dport = str(pkt.dport),
                    length = len(pkt),
                    flags = '',
                    seqack = '',
                )
        return logline

    def check_should_ignore(self, pkt, pid, comm):
        """Indicate whether a packet should be passed without mangling.

        Checks whether the packet matches black and whitelists, or whether it
        signifies an FTP Active Mode connection.

        Args:
            pkt: A fnpacket.PacketCtx or derived object
            pid: Process ID
            comm: Process executable name

        Returns:
            True if the packet should be left alone, else False.
        """

        src_ip = pkt.src_ip0
        sport = pkt.sport0
        dst_ip = pkt.dst_ip0
        dport = pkt.dport0

        if not self.is_set('redirectalltraffic'):
            self.pdebug(DIGN, 'Ignoring %s packet %s' %
                        (pkt.proto_name, pkt.hdrToStr()))
            return True

        # SingleHost mode checks
        if self.single_host_mode:
            if comm:
                # Check process blacklist
                if comm in self.blacklist_processes:
                    self.pdebug(DIGN, ('Ignoring %s packet from process %s ' +
                                'in the process blacklist.') % (pkt.proto_name,
                                comm))
                    self.pdebug(DIGN, '  %s' %
                                (pkt.hdrToStr()))
                    return True

                # Check process whitelist
                elif (len(self.whitelist_processes) and (comm not in
                      self.whitelist_processes)):
                    self.pdebug(DIGN, ('Ignoring %s packet from process %s ' +
                                'not in the process whitelist.') % (pkt.proto_name,
                                comm))
                    self.pdebug(DIGN, '  %s' %
                                (pkt.hdrToStr()))
                    return True

                # Check per-listener blacklisted process list
                elif ((pkt.proto_name in self.port_process_blacklist) and
                        (dport in self.port_process_blacklist[pkt.proto_name])):
                    # If program DOES match blacklist
                    if comm in self.port_process_blacklist[pkt.proto_name][dport]:
                        self.pdebug(DIGN, ('Ignoring %s request packet from ' +
                                    'process %s in the listener process ' +
                                    'blacklist.') % (pkt.proto_name, comm))
                        self.pdebug(DIGN, '  %s' %
                                    (pkt.hdrToStr()))

                    return True

                # Check per-listener whitelisted process list
                elif ((pkt.proto_name in self.port_process_whitelist) and
                        (dport in self.port_process_whitelist[pkt.proto_name])):
                    # If program does NOT match whitelist
                    if not comm in self.port_process_whitelist[pkt.proto_name][dport]:
                        self.pdebug(DIGN, ('Ignoring %s request packet from ' +
                                    'process %s not in the listener process ' +
                                    'whitelist.') % (pkt.proto_name, comm))
                        self.pdebug(DIGN, '  %s' %
                                    (pkt.hdrToStr()))
                        return True

        # MultiHost mode checks
        else:
            pass  # None as of yet

        # Checks independent of mode

        # Forwarding blacklisted port
        if set(self.blacklist_ports[pkt.proto_name]).intersection([sport, dport]):
            self.pdebug(DIGN, 'Forwarding blacklisted port %s packet:' %
                        (pkt.proto_name))
            self.pdebug(DIGN, '  %s' % (pkt.hdrToStr()))
            return True

        # Check host blacklist
        global_host_blacklist = self.getconfigval('hostblacklist')
        if global_host_blacklist and dst_ip in global_host_blacklist:
            self.pdebug(DIGN, ('Ignoring %s packet to %s in the host ' +
                        'blacklist.') % (pkt.proto_name, dst_ip))
            self.pdebug(DIGN, '  %s' % (pkt.hdrToStr()))
            return True

        # Check the port host whitelist
        if ((pkt.proto_name in self.port_host_whitelist) and
                (dport in self.port_host_whitelist[pkt.proto_name])):
            # If host does NOT match whitelist
            if not dst_ip in self.port_host_whitelist[pkt.proto_name][dport]:
                self.pdebug(DIGN, ('Ignoring %s request packet to %s not in ' +
                            'the listener host whitelist.') % (pkt.proto_name,
                            dst_ip))
                self.pdebug(DIGN, '  %s' % (pkt.hdrToStr()))
                return True

        # Check the port host blacklist
        if ((pkt.proto_name in self.port_host_blacklist) and
                (dport in self.port_host_blacklist[pkt.proto_name])):
            # If host DOES match blacklist
            if dst_ip in self.port_host_blacklist[pkt.proto_name][dport]:
                self.pdebug(DIGN, ('Ignoring %s request packet to %s in the ' +
                            'listener host blacklist.') % (pkt.proto_name, dst_ip))
                self.pdebug(DIGN, '  %s' % (pkt.hdrToStr()))
                return True

        # Duplicated from diverters/windows.py:
        # HACK: FTP Passive Mode Handling
        # Check if a listener is initiating a new connection from a
        # non-diverted port and add it to blacklist. This is done to handle a
        # special use-case of FTP ACTIVE mode where FTP server is initiating a
        # new connection for which the response may be redirected to a default
        # listener.  NOTE: Additional testing can be performed to check if this
        # is actually a SYN packet
        if pid == self.pid:
            if (
                ((dst_ip in self.ip_addrs[pkt.ipver]) and
                (not dst_ip.startswith('127.'))) and
                ((src_ip in self.ip_addrs[pkt.ipver]) and
                (not dst_ip.startswith('127.'))) and
                (not set([sport, dport]).intersection(self.diverted_ports[pkt.proto_name]))
                ):

                self.pdebug(DIGN | DFTP, 'Listener initiated %s connection' %
                            (pkt.proto_name))
                self.pdebug(DIGN | DFTP, '  %s' % (pkt.hdrToStr()))
                self.pdebug(DIGN | DFTP, '  Blacklisting port %d' % (sport))
                self.blacklist_ports[pkt.proto_name].append(sport)

            return True

        return False

    def check_log_icmp(self, crit, pkt):
        """Log an ICMP packet if the header was parsed as ICMP.

        Args:
            crit: A DivertParms object
            pkt: An fnpacket.PacketCtx or derived object

        Returns:
            None
        """
        if pkt.is_icmp:
            self.logger.info('ICMP type %d code %d %s' % (
                pkt.icmp_type, pkt.icmp_code, pkt.hdrToStr()))

        return None

    def getOriginalDestPort(self, orig_src_ip, orig_src_port, proto):
        """Return original destination port, or None if it was not redirected.

        Called by proxy listener.
        """

        orig_src_key = fnpacket.PacketCtx.gen_endpoint_key(proto, orig_src_ip,
                                                  orig_src_port)
        self.port_fwd_table_lock.acquire()

        try:
            return self.port_fwd_table.get(orig_src_key)
        finally:
            self.port_fwd_table_lock.release()

    def maybe_redir_ip(self, crit, pkt, pid, comm):
        """Conditionally redirect foreign destination IPs to localhost.

        Used only under SingleHost mode.

        Returns:
            None - if unmodified
            dpkt.ip.hdr - if modified
        """
        if self.check_should_ignore(pkt, pid, comm):
            return None

        self.pdebug(DIPNAT, 'Condition 1 test')
        # Condition 1: If the remote IP address is foreign to this system,
        # then redirect it to a local IP address.
        if self.single_host_mode and (pkt.dst_ip not in self.ip_addrs[pkt.ipver]):
            self.pdebug(DIPNAT, 'Condition 1 satisfied')
            self.ip_fwd_table_lock.acquire()
            try:
                self.ip_fwd_table[pkt.skey] = pkt.dst_ip

            finally:
                self.ip_fwd_table_lock.release()

            newdst = self.getNewDestinationIp(pkt.src_ip)

            self.pdebug(DIPNAT, 'REDIRECTING %s to IP %s' %
                        (pkt.hdrToStr(), newdst))
            pkt.dst_ip = newdst

        else:
            # Delete any stale entries in the IP forwarding table: If the
            # local endpoint appears to be reusing a client port that was
            # formerly used to connect to a foreign host (but not anymore),
            # then remove the entry. This prevents a packet hook from
            # faithfully overwriting the source IP on a later packet to
            # conform to the foreign endpoint's stale connection IP when
            # the host is reusing the port number to connect to an IP
            # address that is local to the FakeNet system.

            self.ip_fwd_table_lock.acquire()
            try:
                if pkt.skey in self.ip_fwd_table:
                    self.pdebug(DIPNAT, ' - DELETING ipfwd key entry: ' + pkt.skey)
                    del self.ip_fwd_table[pkt.skey]
            finally:
                self.ip_fwd_table_lock.release()

        return pkt.hdr if pkt.mangled else None

    def maybe_fixup_srcip(self, crit, pkt, pid, comm):
        """Conditionally fix up the source IP address if the remote endpoint
        had their connection IP-forwarded.

        Check is based on whether the remote endpoint corresponds to a key in
        the IP forwarding table.

        Returns:
            None - if unmodified
            dpkt.ip.hdr - if modified
        """
        # Condition 4: If the local endpoint (IP/port/proto) combo
        # corresponds to an endpoint that initiated a conversation with a
        # foreign endpoint in the past, then fix up the source IP for this
        # incoming packet with the last destination IP that was requested
        # by the endpoint.
        self.pdebug(DIPNAT, "Condition 4 test: was remote endpoint IP fwd'd?")
        self.ip_fwd_table_lock.acquire()
        try:
            if self.single_host_mode and (pkt.dkey in self.ip_fwd_table):
                self.pdebug(DIPNAT, 'Condition 4 satisfied')
                self.pdebug(DIPNAT, ' = FOUND ipfwd key entry: ' + pkt.dkey)
                new_srcip = self.ip_fwd_table[pkt.dkey]
                self.pdebug(DIPNAT, 'MASQUERADING %s from IP %s' %
                            (pkt.hdrToStr(), new_srcip))
                pkt.src_ip = new_srcip
            else:
                self.pdebug(DIPNAT, ' ! NO SUCH ipfwd key entry: ' + pkt.dkey)
        finally:
            self.ip_fwd_table_lock.release()

        return pkt.hdr if pkt.mangled else None

    def maybe_redir_port(self, crit, pkt, pid, comm):
        # Get default listener port for this proto, or bail if none
        default = self.default_listener.get(pkt.proto_name)

        # A: Check: There is a default listener for this protocol
        if not default:
            return None

        # Pre-condition 1: destination must not be present in port forwarding
        # table (prevents masqueraded ports responding to unbound ports from
        # being mistaken as starting a conversation with an unbound port).

        # C: Check: Destination was recorded as talking to a local port that
        # was not bound and was consequently redirected to the default listener
        found = False
        self.port_fwd_table_lock.acquire()
        try:
            # Uses dkey to cross-reference
            found = pkt.dkey in self.port_fwd_table
        finally:
            self.port_fwd_table_lock.release()

        if found:
            return None

        bound_ports = self.diverted_ports.get(pkt.proto_name, [])

        # First, check if this packet is sent from a listener/diverter
        # If so, don't redir for 'Hidden' status because it is already
        # being forwarded from proxy listener to bound/hidden listener
        # Next, check if listener for this port is 'Hidden'. If so, we need to
        # divert it to the proxy as per the Hidden config

        # D: Check: Proxy: dport is bound and listener is hidden
        dport_hidden_listener = bound_ports.get(pkt.dport) is True

        # Condition 2: If the packet is destined for an unbound port, then
        # redirect it to a bound port and save the old destination IP in
        # the port forwarding table keyed by the source endpoint identity.

        if dport_hidden_listener or self.decide_redir_port(pkt, bound_ports):
            self.pdebug(DDPFV, 'Condition 2 satisfied: Packet destined for unbound port or hidden listener')

            # Post-condition 1: General ignore conditions are not met, or this
            # is part of a conversation that is already being ignored.
            #
            # Placed after the decision to redirect for three reasons:
            # 1.) We want to ensure that the else condition below has a chance
            #     to check whether to delete a stale port forwarding table
            #     entry.
            # 2.) Checking these conditions is, on average, more expensive than
            #     checking if the packet would be redirected in the first
            #     place.
            # 3.) Reporting of packets that are being ignored (i.e. not
            #     redirected), which is integrated into this check, should only
            #     appear when packets would otherwise have been redirected.

            # Is this conversation already being ignored for DPF purposes?
            self.ignore_table_lock.acquire()
            try:
                if pkt.dkey in self.ignore_table and self.ignore_table[pkt.dkey] == pkt.sport:
                    # This is a reply (e.g. a TCP RST) from the
                    # non-port-forwarded server that the non-port-forwarded
                    # client was trying to talk to. Leave it alone.
                    return None
            finally:
                self.ignore_table_lock.release()

            if self.check_should_ignore(pkt, pid, comm):
                self.ignore_table_lock.acquire()
                try:
                    self.ignore_table[pkt.skey] = pkt.dport
                finally:
                    self.ignore_table_lock.release()
                return None

            # Record the foreign endpoint and old destination port in the port
            # forwarding table
            self.pdebug(DDPFV, ' + ADDING portfwd key entry: ' + pkt.skey)
            self.port_fwd_table_lock.acquire()
            try:
                self.port_fwd_table[pkt.skey] = pkt.dport
            finally:
                self.port_fwd_table_lock.release()

            self.pdebug(DDPF, 'Redirecting %s to go to port %d' %
                        (pkt.hdrToStr(), default))
            pkt.dport = default

        else:
            # Delete any stale entries in the port forwarding table: If the
            # foreign endpoint appears to be reusing a client port that was
            # formerly used to connect to an unbound port on this server,
            # remove the entry. This prevents the OUTPUT or other packet
            # hook from faithfully overwriting the source port to conform
            # to the foreign endpoint's stale connection port when the
            # foreign host is reusing the port number to connect to an
            # already-bound port on the FakeNet system.

            self.delete_stale_port_fwd_key(pkt.skey)

        if crit.first_packet_new_session:
            self.addSession(pkt)

            # Execute command if applicable
            self.maybeExecuteCmd(pkt, pid, comm)

        return pkt.hdr if pkt.mangled else None

    def maybe_fixup_sport(self, crit, pkt, pid, comm):
        """Conditionally fix up source port if the remote endpoint had their
        connection port-forwarded.

        Check is based on whether the remote endpoint corresponds to a key in
        the port forwarding table.

        Returns:
            None - if unmodified
            dpkt.ip.hdr - if modified
        """
        hdr_modified = None

        # Condition 3: If the remote endpoint (IP/port/proto) combo
        # corresponds to an endpoint that initiated a conversation with an
        # unbound port in the past, then fix up the source port for this
        # outgoing packet with the last destination port that was requested
        # by that endpoint. The term "endpoint" is (ab)used loosely here to
        # apply to UDP host/port/proto combos and any other protocol that
        # may be supported in the future.
        new_sport = None
        self.pdebug(DDPFV, "Condition 3 test: was remote endpoint port fwd'd?")

        self.port_fwd_table_lock.acquire()
        try:
            new_sport = self.port_fwd_table.get(pkt.dkey)
        finally:
            self.port_fwd_table_lock.release()

        if new_sport:
            self.pdebug(DDPFV, 'Condition 3 satisfied: must fix up ' +
                        'source port')
            self.pdebug(DDPFV, ' = FOUND portfwd key entry: ' + pkt.dkey)
            self.pdebug(DDPF, 'MASQUERADING %s to come from port %d' %
                              (pkt.hdrToStr(), new_sport))
            pkt.sport = new_sport
        else:
            self.pdebug(DDPFV, ' ! NO SUCH portfwd key entry: ' + pkt.dkey)

        return pkt.hdr if pkt.mangled else None

    def delete_stale_port_fwd_key(self, skey):
        self.port_fwd_table_lock.acquire()
        try:
            if skey in self.port_fwd_table:
                self.pdebug(DDPFV, ' - DELETING portfwd key entry: ' + skey)
                del self.port_fwd_table[skey]
        finally:
            self.port_fwd_table_lock.release()

    def decide_redir_port(self, pkt, bound_ports):
        """Decide whether to redirect a port.

        Optimized logic derived by truth table + k-map. See docs/internals.md
        for details.
        """
        # A, B, C, D for easy manipulation; full names for readability only.
        a = src_local = (pkt.src_ip in self.ip_addrs[pkt.ipver])
        c = sport_bound = pkt.sport in (bound_ports)
        d = dport_bound = pkt.dport in (bound_ports)

        if self.pdebug_level & DDPFV:
            # Unused logic term not calculated except for debug output
            b = dst_local = (pkt.dst_ip in self.ip_addrs[pkt.ipver])

            self.pdebug(DDPFV, 'src %s (%s)' %
                        (str(pkt.src_ip), ['foreign', 'local'][a]))
            self.pdebug(DDPFV, 'dst %s (%s)' %
                        (str(pkt.dst_ip), ['foreign', 'local'][b]))
            self.pdebug(DDPFV, 'sport %s (%sbound)' %
                        (str(pkt.sport), ['un', ''][c]))
            self.pdebug(DDPFV, 'dport %s (%sbound)' %
                        (str(pkt.dport), ['un', ''][d]))

            def bn(x): return '1' if x else '0'  # Bool -> binary
            self.pdebug(DDPFV, 'abcd = ' + bn(a) + bn(b) + bn(c) + bn(d))

        return (not a and not d) or (not c and not d)

    def addSession(self, pkt):
        self.sessions[pkt.sport] = (pkt.dst_ip, pkt.dport)

    def maybeExecuteCmd(self, pkt, pid, comm):
        execCmd = None

        if not pid:
            return

        port_exec = self.port_execute.get(pkt.proto_name)
        if not port_exec:
            return

        default = self.default_listener.get(pkt.proto_name)

        if (pkt.dport in port_exec) or (default and default in port_exec):
            execCmd = self.build_cmd(pkt, pid, comm)
        if execCmd:
            self.logger.info('Executing command: %s' % (execCmd))
            self.execute_detached(execCmd)

def test_redir_logic(diverter_factory):
    diverter_config = dict()
    diverter_config['dumppackets'] = 'Yes'
    diverter_config['dumppacketsfileprefix'] = 'packets'
    diverter_config['modifylocaldns'] = 'No'
    diverter_config['stopdnsservice'] = 'Yes'
    diverter_config['redirectalltraffic'] = 'Yes'
    diverter_config['defaulttcplistener'] = 'RawTCPListener'
    diverter_config['defaultudplistener'] = 'RawUDPListener'
    diverter_config['blacklistportstcp'] = '139'
    diverter_config['blacklistportsudp'] = '67, 68, 137, 138, 1900, 5355'

    listeners_config = OrderedDict()

    listeners_config['dummytcp'] = dict()
    listeners_config['dummytcp']['enabled'] = 'True'
    listeners_config['dummytcp']['port'] = '65535'
    listeners_config['dummytcp']['protocol'] = 'TCP'
    listeners_config['dummytcp']['listener'] = 'RawListener'
    listeners_config['dummytcp']['usessl'] = 'No'
    listeners_config['dummytcp']['timeout'] = '10'

    listeners_config['rawtcplistener'] = dict()
    listeners_config['rawtcplistener']['enabled'] = 'True'
    listeners_config['rawtcplistener']['port'] = '1337'
    listeners_config['rawtcplistener']['protocol'] = 'TCP'
    listeners_config['rawtcplistener']['listener'] = 'RawListener'
    listeners_config['rawtcplistener']['usessl'] = 'No'
    listeners_config['rawtcplistener']['timeout'] = '10'

    listeners_config['dummyudp'] = dict()
    listeners_config['dummyudp']['enabled'] = 'True'
    listeners_config['dummyudp']['port'] = '65535'
    listeners_config['dummyudp']['protocol'] = 'UDP'
    listeners_config['dummyudp']['listener'] = 'RawListener'
    listeners_config['dummyudp']['usessl'] = 'No'
    listeners_config['dummyudp']['timeout'] = '10'

    listeners_config['rawudplistener'] = dict()
    listeners_config['rawudplistener']['enabled'] = 'True'
    listeners_config['rawudplistener']['port'] = '1337'
    listeners_config['rawudplistener']['protocol'] = 'UDP'
    listeners_config['rawudplistener']['listener'] = 'RawListener'
    listeners_config['rawudplistener']['usessl'] = 'No'
    listeners_config['rawudplistener']['timeout'] = '10'

    listeners_config['httplistener80'] = dict()
    listeners_config['httplistener80']['enabled'] = 'True'
    listeners_config['httplistener80']['port'] = '80'
    listeners_config['httplistener80']['protocol'] = 'TCP'
    listeners_config['httplistener80']['listener'] = 'HTTPListener'
    listeners_config['httplistener80']['usessl'] = 'No'
    listeners_config['httplistener80']['webroot'] = 'defaultFiles/'
    listeners_config['httplistener80']['timeout'] = '10'
    listeners_config['httplistener80']['dumphttpposts'] = 'Yes'
    listeners_config['httplistener80']['dumphttppostsfileprefix'] = 'http'

    ip_addrs = dict()
    ip_addrs[4] = ['192.168.19.222', '127.0.0.1']
    ip_addrs[6] = []

    div = diverter_factory(diverter_config, listeners_config, ip_addrs)
    testcase = namedtuple(
        'testcase', ['src', 'sport', 'dst', 'dport', 'expect'])

    foreign = '192.168.19.132'
    LOCAL = '192.168.19.222'
    LOOPBACK = '127.0.0.1'
    unbound = 33333
    BOUND = 80

    bound_ports = []
    for k, v in listeners_config.iteritems():
        bound_ports.append(int(v['port'], 10))

    testcases = [
        testcase(foreign, unbound, LOCAL, unbound, True),
        testcase(foreign, unbound, LOCAL, BOUND, False),
        testcase(foreign, BOUND, LOCAL, unbound, True),
        testcase(foreign, BOUND, LOCAL, BOUND, False),

        testcase(LOCAL, unbound, foreign, unbound, True),
        testcase(LOCAL, unbound, foreign, BOUND, False),
        testcase(LOCAL, BOUND, foreign, unbound, False),
        testcase(LOCAL, BOUND, foreign, BOUND, False),

        testcase(LOOPBACK, unbound, LOOPBACK, unbound, True),
        testcase(LOOPBACK, unbound, LOOPBACK, BOUND, False),
        testcase(LOOPBACK, BOUND, LOOPBACK, unbound, False),
        testcase(LOOPBACK, BOUND, LOOPBACK, BOUND, False),
    ]

    for tc in testcases:
        r = div.decide_redir_port(4, 'TCP', 1337, bound_ports, tc.src,
                                  tc.sport, tc.dst, tc.dport)
        if r != tc.expect:
            print('TEST CASE FAILED: %s:%d -> %s:%d expected %d got %d' %
                  (tc.src, tc.sport, tc.dst, tc.dport, tc.expect, r))
        else:
            print('Test case passed: %s:%d -> %s:%d expected %d got %d' %
                  (tc.src, tc.sport, tc.dst, tc.dport, tc.expect, r))
