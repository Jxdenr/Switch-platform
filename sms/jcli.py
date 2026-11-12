"""Minimal client for Jasmin's jCli management console (telnet, port 8990).

Python 3.13 removed telnetlib, so this speaks the plain-text protocol over a socket.
"""
import re
import socket
import time

PROMPT = re.compile(rb"(jcli : |> |\.+ )$")


class JCli:
    def __init__(self, host, port, user, password, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.timeout = timeout
        self._read_until(b"Username: ")
        self._send(user)
        self._read_until(b"Password: ")
        self._send(password)
        out = self._read_prompt()
        if b"Incorrect" in out:
            raise PermissionError("jCli login failed")

    def _send(self, line):
        self.sock.sendall(line.encode() + b"\r\n")

    def _read_until(self, marker):
        buf, end = b"", time.time() + self.timeout
        while marker not in buf and time.time() < end:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    def _read_prompt(self):
        buf, end = b"", time.time() + self.timeout
        while time.time() < end:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if PROMPT.search(buf.rstrip(b"\r\n") + b" ") or buf.endswith((b"jcli : ", b"> ")):
                break
        return buf

    def run(self, line):
        self._send(line)
        return self._read_prompt().decode(errors="replace")

    def block(self, opener, fields):
        """Run an interactive add/update block, e.g. smppccm -a, then 'ok'."""
        out = [self.run(opener)]
        for k, v in fields.items():
            out.append(self.run(f"{k} {v}"))
        out.append(self.run("ok"))
        return "".join(out)

    def close(self):
        try:
            self._send("quit")
        finally:
            self.sock.close()
