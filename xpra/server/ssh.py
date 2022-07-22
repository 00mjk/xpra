# This file is part of Xpra.
# Copyright (C) 2018-2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import shlex
import socket
import base64
import hashlib
import binascii
from subprocess import Popen, PIPE
from threading import Event
from time import monotonic
import paramiko

from xpra.net.ssh import SSHSocketConnection
from xpra.net.bytestreams import pretty_socket
from xpra.util import csv, envint, first_time, decode_str
from xpra.os_util import osexpand, getuid, WIN32, POSIX
from xpra.make_thread import start_thread
from xpra.scripts.config import parse_bool
from xpra.platform.paths import get_ssh_conf_dirs, get_xpra_command
from xpra.log import Logger

log = Logger("network", "ssh")

SERVER_WAIT = envint("XPRA_SSH_SERVER_WAIT", 20)
AUTHORIZED_KEYS = "~/.ssh/authorized_keys"
AUTHORIZED_KEYS_HASHES = os.environ.get("XPRA_AUTHORIZED_KEYS_HASHES",
                                        "md5,sha1,sha224,sha256,sha384,sha512").split(",")


def chan_send(send_fn, data, timeout=5):
    if not data:
        return
    size = len(data)
    start = monotonic()
    while data and monotonic()-start<timeout:
        sent = send_fn(data)
        log("chan_send: sent %i bytes out of %i using %s", sent, size, send_fn)
        if not sent:
            break
        data = data[sent:]
    if data:
        raise Exception("failed to send all the data using %s" % send_fn)


class SSHServer(paramiko.ServerInterface):
    def __init__(self, none_auth=False, pubkey_auth=True, password_auth=None, options=None):
        self.event = Event()
        self.none_auth = none_auth
        self.pubkey_auth = pubkey_auth
        self.password_auth = password_auth
        self.proxy_channel = None
        self.options = options or {}

    def get_allowed_auths(self, username):
        #return "gssapi-keyex,gssapi-with-mic,password,publickey"
        mods = []
        if self.none_auth:
            mods.append("none")
        if self.pubkey_auth:
            mods.append("publickey")
        if self.password_auth:
            mods.append("password")
        log("get_allowed_auths(%s)=%s", username, mods)
        return ",".join(mods)

    def check_channel_request(self, kind, chanid):
        log("check_channel_request(%s, %s)", kind, chanid)
        if kind=="session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_none(self, username):
        log("check_auth_none(%s) none_auth=%s", username, self.none_auth)
        if self.none_auth:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        log("check_auth_password(%s, %s) password_auth=%s", username, "*"*len(password), self.password_auth)
        if not self.password_auth or not self.password_auth(username, password):
            return paramiko.AUTH_FAILED
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_publickey(self, username, key):
        log("check_auth_publickey(%s, %r) pubkey_auth=%s", username, key, self.pubkey_auth)
        if not self.pubkey_auth:
            return paramiko.AUTH_FAILED
        if not POSIX or getuid()!=0:
            import getpass
            sysusername = getpass.getuser()
            if sysusername!=username:
                log.warn("Warning: ssh password authentication failed,")
                log.warn(" username does not match:")
                log.warn(" expected '%s', got '%s'", sysusername, username)
                return paramiko.AUTH_FAILED
        authorized_keys_filename = osexpand(AUTHORIZED_KEYS)
        if not os.path.exists(authorized_keys_filename) or not os.path.isfile(authorized_keys_filename):
            log("file '%s' does not exist", authorized_keys_filename)
            return paramiko.AUTH_FAILED
        fingerprint = key.get_fingerprint()
        hex_fingerprint = binascii.hexlify(fingerprint)
        log("looking for key fingerprint '%s' in '%s'", hex_fingerprint, authorized_keys_filename)
        count = 0
        with open(authorized_keys_filename, "rb") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                line = line.strip("\n\r")
                try:
                    key = base64.b64decode(line.strip().split()[1].encode('ascii'))
                except Exception as e:
                    log("ignoring line '%s': %s", line, e)
                    continue
                for hash_algo in AUTHORIZED_KEYS_HASHES:
                    hash_instance = None
                    try:
                        hash_class = getattr(hashlib, hash_algo) #ie: hashlib.md5
                        hash_instance = hash_class(key)     #can raise ValueError (ie: on FIPS compliant systems)
                    except ValueError:
                        hash_instance = None
                    if not hash_instance:
                        if first_time("hash-%s-missing" % hash_algo):
                            log.warn("Warning: unsupported hash '%s'", hash_algo)
                        continue
                    fp_plain = hash_instance.hexdigest()
                    log("%s(%s)=%s", hash_algo, line, fp_plain)
                    if fp_plain==hex_fingerprint:
                        return paramiko.OPEN_SUCCEEDED
                count += 1
        log("no match in %i keys from '%s'", count, authorized_keys_filename)
        return paramiko.AUTH_FAILED

    def check_auth_gssapi_keyex(self, username, gss_authenticated=paramiko.AUTH_FAILED, cc_file=None):
        log("check_auth_gssapi_keyex%s", (username, gss_authenticated, cc_file))
        return paramiko.AUTH_FAILED

    def check_auth_gssapi_with_mic(self, username, gss_authenticated=paramiko.AUTH_FAILED, cc_file=None):
        log("check_auth_gssapi_with_mic%s", (username, gss_authenticated, cc_file))
        return paramiko.AUTH_FAILED

    def check_channel_shell_request(self, channel):
        log("check_channel_shell_request(%s)", channel)
        return False

    def check_channel_exec_request(self, channel, command):
        def fail():
            self.event.set()
            channel.close()
            return False
        log("check_channel_exec_request(%s, %s)", channel, command)
        cmd = shlex.split(decode_str(command))
        log("check_channel_exec_request: cmd=%s", cmd)
        # not sure if this is the best way to handle this, 'command -v xpra' has len=3
        if cmd[0] in ("type", "which", "command") and len(cmd) in (2,3):
            xpra_cmd = cmd[-1]   #ie: $XDG_RUNTIME_DIR/xpra/run-xpra or "xpra"
            if not POSIX:
                assert WIN32
                #we can't execute "type" or "which" on win32,
                #so we just answer as best we can
                #and only accept "xpra" as argument:
                if xpra_cmd.strip('"').strip("'")=="xpra":
                    chan_send(channel.send, "xpra is xpra")
                    channel.send_exit_status(0)
                else:
                    chan_send(channel.send_stderr, "type: %s: not found" % xpra_cmd)
                    channel.send_exit_status(1)
                return True
            #we don't want to use a shell,
            #but we need to expand the file argument:
            cmd[-1] = osexpand(xpra_cmd)
            try:
                proc = Popen(cmd, stdout=PIPE, stderr=PIPE, close_fds=not WIN32)
                out, err = proc.communicate()
            except Exception as e:
                log("check_channel_exec_request(%s, %s)", channel, command, exc_info=True)
                chan_send(channel.send_stderr, f"failed to execute command: {e}")
                channel.send_exit_status(1)
            else:
                log(f"check_channel_exec_request: out(`{cmd}`)={out!r}")
                log(f"check_channel_exec_request: err(`{cmd}`)={err!r}")
                chan_send(channel.send, out)
                chan_send(channel.send_stderr, err)
                channel.send_exit_status(proc.returncode)
        elif cmd[0].endswith("xpra") and len(cmd)>=2:
            subcommand = cmd[1].strip("\"'").rstrip(";")
            log("ssh xpra subcommand: %s", subcommand)
            if subcommand in ("_proxy_start", "_proxy_start_desktop", "_proxy_shadow_start"):
                proxy_start = parse_bool("proxy-start", self.options.get("proxy-start"), False)
                if not proxy_start:
                    log.warn(f"Warning: received a {subcommand!r} session request")
                    log.warn(" this feature is not yet implemented with the builtin ssh server")
                    return fail()
                self.proxy_start(channel, subcommand, cmd[2:])
            elif subcommand=="_proxy":
                if len(cmd)==3:
                    #only the display can be specified here
                    display = cmd[2]
                    display_name = getattr(self, "display_name", "")
                    if display_name!=display:
                        log.warn(f"Warning: the display requested {display!r}")
                        log.warn(f" does not match the current display {display_name!r}")
                        return fail()
            else:
                log.warn(f"Warning: unsupported xpra subcommand '{cmd[1]}'")
                return fail()
            #we're ready to use this socket as an xpra channel
            self._run_proxy(channel)
        else:
            #plain 'ssh' clients execute a long command with if+else statements,
            #try to detect it and extract the actual command the client is trying to run.
            #ie:
            #['sh', '-c',
            # ': run-xpra _proxy;xpra initenv;\
            #  if [ -x $XDG_RUNTIME_DIR/xpra/run-xpra ]; then $XDG_RUNTIME_DIR/xpra/run-xpra _proxy;\
            #  elif [ -x ~/.xpra/run-xpra ]; then ~/.xpra/run-xpra _proxy;\
            #  elif type "xpra" > /dev/null 2>&1; then xpra _proxy;\
            #  elif [ -x /usr/local/bin/xpra ]; then /usr/local/bin/xpra _proxy;\
            #  else echo "no run-xpra command found"; exit 1; fi']
            #if .* ; then .*/run-xpra _proxy;
            log("parse cmd=%s (len=%i)", cmd, len(cmd))
            if len(cmd)==1:         #ie: 'thelongcommand'
                parse_cmd = cmd[0]
            elif len(cmd)==3 and cmd[:2]==["sh", "-c"]:     #ie: 'sh' '-c' 'thelongcommand'
                parse_cmd = cmd[2]
            else:
                parse_cmd = ""
            #for older clients, try to parse the long command
            #and identify the subcommands from there
            subcommands = []
            for s in parse_cmd.split("if "):
                if (s.startswith("type \"xpra\"") or
                    s.startswith("which \"xpra\"") or
                    s.startswith("[ -x")
                    ) and s.find("then ")>0:
                    then_str = s.split("then ", 1)[1]
                    #ie: then_str="$XDG_RUNTIME_DIR/xpra/run-xpra _proxy; el"
                    if then_str.find(";")>0:
                        then_str = then_str.split(";")[0]
                    parts = shlex.split(then_str)
                    if len(parts)>=2:
                        subcommand = parts[1]       #ie: "_proxy"
                        subcommands.append(subcommand)
            log("subcommands=%s", subcommands)
            if subcommands and tuple(set(subcommands))[0]=="_proxy":
                self._run_proxy(channel)
            else:
                log.warn("Warning: unsupported ssh command:")
                log.warn(f" `{cmd}`")
                return fail()
        return True

    def _run_proxy(self, channel):
        pc = self.proxy_channel
        if pc:
            self.proxy_channel = None
            pc.close()
        self.proxy_channel = channel
        self.event.set()

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        log("check_channel_pty_request%s", (channel, term, width, height, pixelwidth, pixelheight, modes))
        return False

    def enable_auth_gssapi(self):
        log("enable_auth_gssapi()")
        return False

    def proxy_start(self, channel, subcommand, args):
        log("ssh proxy-start(%s, %s, %s)", channel, subcommand, args)
        server_mode = {
                       "_proxy_start"           : "seamless",
                       "_proxy_start_desktop"   : "desktop",
                       "_proxy_shadow_start"    : "shadow",
                       }.get(subcommand, subcommand.replace("_proxy_", ""))
        log.info("ssh channel starting proxy %s session", server_mode)
        cmd = get_xpra_command()+[subcommand]+args
        try:
            proc = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0, close_fds=True)
            proc.poll()
        except OSError:
            log.error(f"Error starting proxy subcommand `{subcommand}`", exc_info=True)
            log.error(f" with args={args}")
            return
        from xpra.child_reaper import getChildReaper
        def proxy_ended(*args):
            log("proxy_ended(%s)", args)
        def close():
            if proc.poll() is None:
                proc.terminate()
        getChildReaper().add_process(proc, f"proxy-start-{subcommand}", cmd, True, True, proxy_ended)
        def proc_to_channel(read, send):
            while proc.poll() is None:
                #log("proc_to_channel(%s, %s) waiting for data", read, send)
                try:
                    r = read(4096)
                except paramiko.buffered_pipe.PipeTimeout:
                    log("proc_to_channel(%s, %s)", read, send, exc_info=True)
                    close()
                    break
                #log("proc_to_channel(%s, %s) %i bytes: %s", read, send, len(r or b""), ellipsizer(r))
                if r:
                    try:
                        chan_send(send, r)
                    except OSError:
                        log("proc_to_channel(%s, %s)", read, send, exc_info=True)
                        close()
                        break
        #forward to/from the process and the channel:
        def stderr_reader():
            proc_to_channel(proc.stderr.read, channel.send_stderr)
        def stdout_reader():
            proc_to_channel(proc.stdout.read, channel.send)
        def stdin_reader():
            stdin = proc.stdin
            while proc.poll() is None:
                r = channel.recv(4096)
                if not r:
                    close()
                    break
                #log("stdin_reader() %i bytes: %s", len(r or b""), ellipsizer(r))
                stdin.write(r)
                stdin.flush()
        tname = subcommand.replace("_proxy_", "proxy-")
        start_thread(stderr_reader, "%s-stderr" % tname, True)
        start_thread(stdout_reader, "%s-stdout" % tname, True)
        start_thread(stdin_reader, "%s-stdin" % tname, True)
        channel.proxy_process = proc


def make_ssh_server_connection(conn, socket_options, none_auth=False, password_auth=None):
    log("make_ssh_server_connection%s", (conn, socket_options, none_auth, password_auth))
    ssh_server = SSHServer(none_auth=none_auth, password_auth=password_auth, options=socket_options)
    DoGSSAPIKeyExchange = parse_bool("ssh-gss-key-exchange", socket_options.get("ssh-gss-key-exchange", False), False)
    sock = conn._socket
    t = None
    def close():
        if t:
            log(f"close() closing {t}")
            try:
                t.close()
            except Exception:
                log(f"{t}.close()", exc_info=True)
        log(f"close() closing {conn}")
        try:
            conn.close()
        except Exception:
            log(f"{conn}.close()")
    try:
        t = paramiko.Transport(sock, gss_kex=DoGSSAPIKeyExchange)
        gss_host = socket_options.get("ssh-gss-host", socket.getfqdn(""))
        t.set_gss_host(gss_host)
        #load host keys:
        PREFIX = "ssh_host_"
        SUFFIX = "_key"
        host_keys = {}
        def add_host_key(fd, f):
            ff = os.path.join(fd, f)
            keytype = f[len(PREFIX):-len(SUFFIX)]
            if not keytype:
                log.warn(f"Warning: unknown host key format '{f}'")
                return False
            keyclass = getattr(paramiko, "%sKey" % keytype.upper(), None)
            if keyclass is None:
                #Ed25519Key
                keyclass = getattr(paramiko, "%s%sKey" % (keytype[:1].upper(), keytype[1:]), None)
            if keyclass is None:
                log(f"key type {keytype} is not supported, cannot load {ff!r}")
                return False
            log(f"loading {keytype} key from {ff!r} using {keyclass}")
            try:
                host_key = keyclass(filename=ff)
                if host_key not in host_keys:
                    host_keys[host_key] = ff
                    t.add_server_key(host_key)
                    return True
            except IOError as e:
                log("cannot add host key '%s'", ff, exc_info=True)
            except paramiko.SSHException as e:
                log("error adding host key '%s'", ff, exc_info=True)
                log.error("Error: cannot add %s host key '%s':", keytype, ff)
                log.error(" %s", e)
            return False
        host_key = socket_options.get("ssh-host-key")
        if host_key:
            d, f = os.path.split(host_key)
            if f.startswith(PREFIX) and f.endswith(SUFFIX):
                add_host_key(d, f)
            if not host_keys:
                log.error("Error: failed to load host key '%s'", host_key)
                close()
                return None
        else:
            ssh_key_dirs = get_ssh_conf_dirs()
            log("trying to load ssh host keys from: %s", csv(ssh_key_dirs))
            for d in ssh_key_dirs:
                fd = osexpand(d)
                log("osexpand(%s)=%s", d, fd)
                if not os.path.exists(fd) or not os.path.isdir(fd):
                    log("ssh host key directory '%s' is invalid", fd)
                    continue
                for f in os.listdir(fd):
                    if f.startswith(PREFIX) and f.endswith(SUFFIX):
                        add_host_key(fd, f)
            if not host_keys:
                log.error("Error: cannot start SSH server,")
                log.error(" no readable SSH host keys found in:")
                log.error(" %s", csv(ssh_key_dirs))
                close()
                return None
        log("loaded host keys: %s", tuple(host_keys.values()))
        t.start_server(server=ssh_server)
    except (paramiko.SSHException, EOFError) as e:
        log("failed to start ssh server", exc_info=True)
        log.error("Error handling SSH connection:")
        log.error(" %s", e)
        close()
        return None
    try:
        chan = t.accept(SERVER_WAIT)
        if chan is None:
            log.warn("Warning: SSH channel setup failed")
            #prevent errors trying to access this connection, now likely dead:
            conn.set_active(False)
            close()
            return None
    except paramiko.SSHException as e:
        log("failed to open ssh channel", exc_info=True)
        log.error("Error opening channel:")
        log.error(" %s", e)
        close()
        return None
    log("client authenticated, channel=%s", chan)
    timedout = not ssh_server.event.wait(SERVER_WAIT)
    proxy_channel = ssh_server.proxy_channel
    log("proxy channel=%s, timedout=%s", proxy_channel, timedout)
    if not ssh_server.event.is_set() or not proxy_channel:
        if timedout:
            log.warn("Warning: timeout waiting for xpra SSH subcommand,")
            log.warn(" closing connection from %s", pretty_socket(conn.target))
        close()
        return None
    if getattr(proxy_channel, "proxy_process", None):
        log("proxy channel is handled using a subprocess")
        return None
    log("client authenticated, channel=%s", chan)
    return SSHSocketConnection(proxy_channel, sock,
                               conn.local, conn.endpoint, conn.target,
                               socket_options=socket_options)
