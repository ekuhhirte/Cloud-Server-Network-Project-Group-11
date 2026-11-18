
import socket
import threading
import time


def pair():
    """two connected sockets with a timeout so a broken test can't hang"""
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    return a, b


def dribble(sock, data, delay=0.002):
    """send data one byte at a time from a thread"""

    def run():
        for i in range(len(data)):
            sock.sendall(data[i : i + 1])
            time.sleep(delay)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
