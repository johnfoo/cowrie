# Copyright (c) 2009-2014 Upi Tamminen <desaster@gmail.com>
# See the COPYRIGHT file for more information

"""
This module contains ...
"""

from __future__ import division, absolute_import

import os
import time
import hashlib

from twisted.python import log
from twisted.conch.insults import insults

from minio import Minio
from minio.error import (ResponseError, BucketAlreadyExists, BucketAlreadyOwnedByYou)

from cowrie.core import ttylog
from cowrie.core import protocol


class LoggingServerProtocol(insults.ServerProtocol):
    """
    Wrapper for ServerProtocol that implements TTY logging
    """
    stdinlogOpen = False
    ttylogOpen = False
    redirlogOpen = False  # it will be set at core/protocol.py

    def __init__(self, prot=None, *a, **kw):
        insults.ServerProtocol.__init__(self, prot, *a, **kw)
        cfg = a[0].cfg
        self.bytesReceived = 0

        self.ttylogPath = cfg.get('honeypot', 'log_path')
        self.downloadPath = cfg.get('honeypot', 'download_path')

        try:
            self.ttylogEnabled = cfg.getboolean('honeypot', 'ttylog')
        except:
            self.ttylogEnabled = True

        try:
            self.minioEnabled = cfg.getboolean('honeypot', 'minio')
            self.minioServer = cfg.get('honeypot', 'minio_server')
            self.minioAccess = cfg.get('honeypot', 'minio_access_key')
            self.minioSecret = cfg.get('honeypot', 'minio_secret')
            self.minioBucket = cfg.get('honeypot', 'minio_bucket')
            self.minioSecure = cfg.getboolean('honeypot', 'minio_use_ssl')
            self.minioc = Minio(self.minioServer, access_key=self.minioAccess,
                                secret_key=self.minioSecret, secure=self.minioSecure)
        except:
            self.minioEnabled = False

        self.redirFiles = set()

        try:
            self.bytesReceivedLimit = int(cfg.get('honeypot',
                'download_limit_size'))
        except:
            self.bytesReceivedLimit = 0

        if prot is protocol.HoneyPotExecProtocol:
            self.type = 'e' # Execcmd
        else:
            self.type = 'i' # Interactive


    def getSessionId(self):
        """
        """
        transportId = self.transport.session.conn.transport.transportId
        channelId = self.transport.session.id
        return (transportId, channelId)


    def connectionMade(self):
        """
        """
        transportId, channelId = self.getSessionId()
        self.startTime = time.time()

        if self.ttylogEnabled:
            self.ttylogFile = '%s/tty/%s-%s-%s%s.log' % \
                (self.ttylogPath, time.strftime('%Y%m%d-%H%M%S'),
                transportId, channelId, self.type)
            ttylog.ttylog_open(self.ttylogFile, self.startTime)
            self.ttylogOpen = True
            self.ttylogSize = 0
            log.msg(eventid='cowrie.log.open',
                ttylog=self.ttylogFile,
                format='Opening TTY Log: %(ttylog)s')

        self.stdinlogFile = '%s/%s-%s-%s-stdin.log' % \
            (self.downloadPath,
            time.strftime('%Y%m%d-%H%M%S'), transportId, channelId)

        if self.type == 'e':
            self.stdinlogOpen = True
        else: #i
            self.stdinlogOpen = False

        insults.ServerProtocol.connectionMade(self)


    def write(self, bytes):
        """
        Output sent back to user
        """
        if self.ttylogEnabled and self.ttylogOpen:
            ttylog.ttylog_write(self.ttylogFile, len(bytes),
                ttylog.TYPE_OUTPUT, time.time(), bytes)
            self.ttylogSize += len(bytes)

        insults.ServerProtocol.write(self, bytes)


    def dataReceived(self, data):
        """
        Input received from user
        """
        self.bytesReceived += len(data)
        if self.bytesReceivedLimit \
          and self.bytesReceived > self.bytesReceivedLimit:
            log.msg(format='Data upload limit reached')
            #self.loseConnection()
            self.eofReceived()
            return

        if self.stdinlogOpen:
            with open(self.stdinlogFile, 'ab') as f:
                f.write(data)
        elif self.ttylogEnabled and self.ttylogOpen:
            ttylog.ttylog_write(self.ttylogFile, len(data),
                ttylog.TYPE_INPUT, time.time(), data)

        # prevent crash if something like this was passed:
        # echo cmd ; exit; \n\n
        if self.terminalProtocol:
            insults.ServerProtocol.dataReceived(self, data)


    def eofReceived(self):
        """
        Receive channel close and pass on to terminal
        """
        if self.terminalProtocol:
            self.terminalProtocol.eofReceived()


    def loseConnection(self):
        """
        Override super to remove the terminal reset on logout
        """
        self.transport.loseConnection()


    def connectionLost(self, reason):
        """
        FIXME: this method is called 4 times on logout....
        it's called once from Avatar.closed() if disconnected
        """
        if self.stdinlogOpen:
            try:
                with open(self.stdinlogFile, 'rb') as f:
                    shasum = hashlib.sha256(f.read()).hexdigest()
                    shasumfile = os.path.join(self.downloadPath, shasum)
                    if os.path.exists(shasumfile):
                        os.remove(self.stdinlogFile)
                        log.msg("Not storing duplicate content " + shasum)
                    else:
                        os.rename(self.stdinlogFile, shasumfile)
                    # os.symlink(shasum, self.stdinlogFile)
                log.msg(eventid='cowrie.session.file_download',
                        format='Saved stdin contents with SHA-256 %(shasum)s to %(outfile)s',
                        url='stdin',
                        outfile=shasumfile,
                        shasum=shasum)
            except IOError as e:
                pass
            finally:
                self.stdinlogOpen = False

        if self.redirFiles:
            for rp in self.redirFiles:

                rf = rp[0]

                if rp[1]:
                    url = rp[1]
                else:
                    url = rf[rf.find('redir_')+len('redir_'):]

                try:
                    if not os.path.exists(rf):
                        continue

                    if os.path.getsize(rf) == 0:
                        os.remove(rf)
                        continue

                    with open(rf, 'rb') as f:
                        shasum = hashlib.sha256(f.read()).hexdigest()
                        shasumfile = os.path.join(self.downloadPath, shasum)
                        if os.path.exists(shasumfile):
                            os.remove(rf)
                            log.msg("Not storing duplicate content " + shasum)
                        else:
                            os.rename(rf, shasumfile)
                        # os.symlink(shasum, rf)
                    log.msg(eventid='cowrie.session.file_download',
                            format='Saved redir contents with SHA-256 %(shasum)s to %(outfile)s',
                            url=url,
                            outfile=shasumfile,
                            shasum=shasum)
                except IOError:
                    pass
            self.redirFiles.clear()

        if self.ttylogEnabled and self.ttylogOpen:
            log.msg(eventid='cowrie.log.closed',
                    format='Closing TTY Log: %(ttylog)s after %(duration)d seconds',
                    ttylog=self.ttylogFile,
                    size=self.ttylogSize,
                    duration=time.time()-self.startTime)
            ttylog.ttylog_close(self.ttylogFile, time.time())
            self.ttylogOpen = False
            if self.minioEnabled:
                try:
                   self.minioc.fput_object(self.minioBucket, self.ttylogFile, self.ttylogFile)
                   log.msg(eventid='cowrie.log.uploaded',
                           format='Uploaded TTY Log: %(ttylog)s',
                           ttylog=self.ttylogFile)
                except ResponseError as err:
                   pass

        insults.ServerProtocol.connectionLost(self, reason)



class LoggingTelnetServerProtocol(LoggingServerProtocol):
    """
    Wrap LoggingServerProtocol with single method to fetch session id for Telnet
    """

    def getSessionId(self):
        transportId = self.transport.session.transportId
        sn = self.transport.session.transport.transport.sessionno
        return (transportId, sn)
