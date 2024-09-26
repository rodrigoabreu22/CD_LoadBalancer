# coding: utf-8

import socket
import selectors
import signal
import logging
import argparse
import time as time

# configure logger output format
logging.basicConfig(level=logging.DEBUG,format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',datefmt='%m-%d %H:%M:%S')
logger = logging.getLogger('Load Balancer')


# used to stop the infinity loop
done = False

sel = selectors.DefaultSelector()

policy = None
mapper = None


# implements a graceful shutdown
def graceful_shutdown(signalNumber, frame):  
    logger.debug('Graceful Shutdown...')
    global done
    done = True


# n to 1 policy
class N2One:
    def __init__(self, servers):
        self.servers = servers  

    def select_server(self):
        return self.servers[0]

    def update(self, *arg):
        pass


# round robin policy
class RoundRobin:
    def __init__(self, servers):
        self.servers = servers
        self.index = 0

    def select_server(self):
        server = self.servers[self.index]
        self.index = (self.index + 1) % len(self.servers)
        return server
    
    def update(self, *arg):
        pass


# least connections policy
class LeastConnections:
    def __init__(self, servers):
        self.servers = servers
        self.connections = {server: 0 for server in servers}

    def select_server(self):
        min_conn = min(self.servers, key=lambda s: self.connections[s])

        self.connections[min_conn]+=1
        return min_conn

    def update(self, *args):
        server = args[0]
        self.connections[server] -= 1


# least response time
class LeastResponseTime:
    def __init__(self, servers):
        self.servers = servers
        self.response_times = {server: [[0], 0, 0] for server in self.servers}  # [timestamps, avg_response_time, num_requests]

    def select_server(self):
        # Select server with minimum average response time
        server = min(self.response_times, key=lambda key: self.response_times[key][1])

        temp = []
        for item in self.response_times.items():
            if item[1][1] == self.response_times[server][1]:
                temp.append(item)

        if len(temp) > 1:
            server = min(temp, key=lambda item: len(item[1][0]))[0]
        
        self.response_times[server][0][-1] = time.time()
        self.response_times[server][0].append(0)
        
        return server

    def update(self, server):
        server_times_info = self.response_times[server]
        
        server_start_time = server_times_info[0][0]
        server_current_time = time.time()
        delta = server_current_time - server_start_time

        num_requests = server_times_info[2]
        avg_response_time = (server_times_info[1] * num_requests + delta) / (num_requests + 1)
        server_times_info[1] = avg_response_time
        
        server_times_info[2] += 1
        server_times_info[0] = server_times_info[0][1:] + [0]


POLICIES = {
    "N2One": N2One,
    "RoundRobin": RoundRobin,
    "LeastConnections": LeastConnections,
    "LeastResponseTime": LeastResponseTime
}

class SocketMapper:
    def __init__(self, policy):
        self.policy = policy
        self.map = {}

    def add(self, client_sock, upstream_server):
        client_sock.setblocking(False)
        sel.register(client_sock, selectors.EVENT_READ, read)
        upstream_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream_sock.connect(upstream_server)
        upstream_sock.setblocking(False)
        sel.register(upstream_sock, selectors.EVENT_READ, read)
        logger.debug("Proxying to %s %s", *upstream_server)
        self.map[client_sock] =  upstream_sock

    def delete(self, sock):
        paired_sock = self.get_sock(sock)
        sel.unregister(sock)
        sock.close()
        sel.unregister(paired_sock)
        paired_sock.close()
        if sock in self.map:
            self.map.pop(sock)
        else:
            self.map.pop(paired_sock)

    def get_sock(self, sock):
        for client, upstream in self.map.items():
            if upstream == sock:
                return client
            if client == sock:
                return upstream
        return None
    
    def get_upstream_sock(self, sock):
        return self.map.get(sock)

    def get_all_socks(self):
        """ Flatten all sockets into a list"""
        return list(sum(self.map.items(), ())) 

def accept(sock, mask):
    client, addr = sock.accept()
    logger.debug("Accepted connection %s %s", *addr)
    mapper.add(client, policy.select_server())

def read(conn,mask):
    data = conn.recv(4096)
    if len(data) == 0: # No messages in socket, we can close down the socket
        mapper.delete(conn)
    else:
        mapper.get_sock(conn).send(data)


def main(addr, servers, policy_class):
    global policy
    global mapper

    # register handler for interruption 
    # it stops the infinite loop gracefully
    signal.signal(signal.SIGINT, graceful_shutdown)

    policy = policy_class(servers)
    mapper = SocketMapper(policy)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(addr)
    sock.listen()
    sock.setblocking(False)

    sel.register(sock, selectors.EVENT_READ, accept)

    try:
        logger.debug("Listening on %s %s", *addr)
        while not done:
            events = sel.select(timeout=1)
            for key, mask in events:
                if(key.fileobj.fileno()>0):
                    callback = key.data
                    callback(key.fileobj, mask)
                
    except Exception as err:
        logger.error(err)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Pi HTTP server')
    parser.add_argument('-a', dest='policy', choices=POLICIES)
    parser.add_argument('-p', dest='port', type=int, help='load balancer port', default=8080)
    parser.add_argument('-s', dest='servers', nargs='+', type=int, help='list of servers ports')
    args = parser.parse_args()
    
    servers = [('localhost', p) for p in args.servers]
    
    main(('127.0.0.1', args.port), servers, POLICIES[args.policy])