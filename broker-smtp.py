#! /usr/bin/env python


"""An RFC 2821 SMTP Server"""


from __future__ import print_function

import re
import os
import sys
import errno
import socket


from circuits import Component, Event
from circuits.net.sockets import TCPServer
from circuits.net.events import close, write


LINESEP = re.compile("\r?\n")
DATAEND = re.compile("\r?\n[.]\r?\n")


def parse(line):
    if not line:
        return "", ()

    if ":" in line:
        tokens = iter(line.split(":", 1))
    else:
        tokens = iter(line.split(" "))

    cmd = next(tokens).upper()
    cmd = cmd.split(" ", 1)[0] if " " in cmd else cmd
    args = tuple(tokens)

    return cmd, args


def getaddr(s):
    if not s:
        return None

    if s[0] == "<" and s[-1] == ">" and s != "<>":
        # Addresses can be in the form <person@dom.com> but watch out
        # for null address, e.g. <>
        return s[1:-1]


class command(Event):
    """command Event"""


class message(Event):
    """message Event"""


class process(Event):
    """process Event"""


class SMTPChannel(Component):

    COMMAND = 0
    DATA = 1

    def init(self, server, sock, addr):
        self.server = server
        self.sock = sock
        self.addr = addr

        self.__buffer = ""
        self.__state = self.COMMAND
        self.__greeting = 0
        self.__mailfrom = None
        self.__rcpttos = []
        self.__data = ""
        self.__fqdn = socket.getfqdn()

        try:
            self.__peer = self.sock.getpeername()
        except socket.error, err:
            # a race condition  may occur if the other end is closing
            # before we can get the peername
            self.fire(close(self.sock))
            if err[0] != errno.ENOTCONN:
                raise
            return

        self.push("220 {0} {1}".format(self.__fqdn, self.server.version))

    def push(self, msg):
        self.fire(write(self.sock, "{0}\r\n".format(msg)))

    def disconnect(self, sock):
        if sock == self.sock:
            self.unregister()

    def read(self, sock, s):
        if sock != self.sock:
            return

        self.__buffer += s

        while self.__buffer:
            try:
                if self.__state == self.COMMAND:
                    line, self.__buffer = LINESEP.split(self.__buffer, 1)
                    if line:
                        yield self.call(command(line))
                elif self.__state == self.DATA:
                    data, self.__buffer = DATAEND.split(self.__buffer, 1)
                    if data:
                        yield self.call(process(data))
            except ValueError:
                break

    def command(self, line):
        cmd, args = parse(line)
        if not cmd:
            return self.push("500 Error: bad syntax")

        method = getattr(self, "smtp_{0}".format(cmd), None)
        if method is None:
            return self.push("502 Error: command '{0}' not implemented".format(cmd))

        return method(*args)

    def process(self, s):
        if self.__state != self.DATA:
            self.push("451 Internal confusion")
            return

        # Remove extraneous carriage returns and de-transparency according
        # to RFC 821, Section 4.5.2.
        data = []
        for text in s.split("\r\n"):
            if text and text[0] == ".":
                data.append(text[1:])
            else:
                data.append(text)

        self.__data = "\n".join(data)

        status = yield self.call(
            message(
                self.__peer,
                self.__mailfrom,
                self.__rcpttos,
                self.__data
            )
        )

        self.__rcpttos = []
        self.__mailfrom = None
        self.__state = self.COMMAND

        status = status.value

        if status is None:
            self.push("250 Ok")
        else:
            self.push(status)

    # SMTP and ESMTP commands
    def smtp_HELO(self, arg):
        if not arg:
            self.push("501 Syntax: HELO hostname")
            return

        if self.__greeting:
            self.push("503 Duplicate HELO/EHLO")
        else:
            self.__greeting = arg
            self.push("250 %s" % self.__fqdn)

    def smtp_NOOP(self, arg=None):
        if arg:
            self.push("501 Syntax: NOOP")
        else:
            self.push("250 Ok")

    def smtp_QUIT(self, arg=None):
        # arg is ignored
        self.push("221 Bye")
        self.fire(close(self.sock))

    def smtp_MAIL(self, arg):
        address = getaddr(arg) if arg else None

        if not address:
            self.push("501 Syntax: MAIL FROM:<address>")
            return

        if self.__mailfrom:
            self.push("503 Error: nested MAIL command")
            return

        self.__mailfrom = address
        self.push("250 Ok")

    def smtp_RCPT(self, arg):
        if not self.__mailfrom:
            self.push("503 Error: need MAIL command")
            return

        address = getaddr(arg) if arg else None
        if not address:
            self.push("501 Syntax: RCPT TO: <address>")
            return

        self.__rcpttos.append(address)
        self.push("250 Ok")

    def smtp_RSET(self, arg):
        if arg:
            self.push("501 Syntax: RSET")
            return

        # Resets the sender, recipients, and data, but not the greeting
        self.__mailfrom = None
        self.__rcpttos = []
        self.__data = ""
        self.__state = self.COMMAND
        self.push("250 Ok")

    def smtp_DATA(self, arg=None):
        if not self.__rcpttos:
            self.push("503 Error: need RCPT command")
            return

        if arg:
            self.push("501 Syntax: DATA")
            return

        self.__state = self.DATA
        self.push("354 End data with <CR><LF>.<CR><LF>")


class SMTPServer(Component):

    name = "smtpd"
    version = "0.0.1"

    def init(self, bind):
        self.bind = bind

        self.transport = TCPServer(self.bind).register(self)

    def ready(self, server, bind):
        bind = "{0}:{1}".format(*bind)
        print("{0} {1} ready! Listening on: {2}".format(self.name, self.version, bind))

    def connect(self, sock, host, port):
        SMTPChannel(self, sock, (host, port)).register(self)

    def message(self, peer, mailfrom, rcpttos, data):
        """Override this abstract method to handle messages from the client.

        peer is a tuple containing (ipaddr, port) of the client that made the
        socket connection to our smtp port.

        mailfrom is the raw address the client claims the message is coming
        from.

        rcpttos is a list of raw addresses the client wishes to deliver the
        message to.

        data is a string containing the entire full text of the message,
        headers (if supplied) and all.  It has been `de-transparencied"
        according to RFC 821, Section 4.5.2.  In other words, a line
        containing a `." followed by other text has had the leading dot
        removed.

        This function should return None, for a normal `250 Ok" response;
        otherwise it returns the desired response string in RFC 821 format.
        """


class DebugSMTPServer(SMTPServer):

    def message(self, peer, mailfrom, rcpttos, data):
        print("------------ NEW MESSAGE ------------")
        print("Client: {0}:{1}".format(*peer))
        print("From: {0}".format(mailfrom))
        print("To: {0}".format(",".join(rcpttos)))
        print("------------ END MESSAGE ------------")

        inheaders = 1
        lines = data.split("\n")
        print("---------- MESSAGE FOLLOWS ----------")
        for line in lines:
            # headers first
            if inheaders and not line:
                print("X-Peer: {0}".format(peer[0]))
                inheaders = 0
            print(line)
        print("------------ END MESSAGE ------------")


def main():
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", 0)

    args = iter(sys.argv)
    next(args)
    port = int(next(args, "25"))

    DebugSMTPServer(("0.0.0.0", port)).run()


if __name__ == "__main__":
    main()
