# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import warnings
import os
import pipes
import socket
import random
import logging
import traceback
import fcntl
import sys
from binascii import hexlify
from ansible.callbacks import vvv
from ansible import errors
from ansible import utils
from ansible import constants as C

# prevent paramiko warning noise -- see http://stackoverflow.com/questions/3920502/
HAVE_PARAMIKO=False
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        import paramiko
        HAVE_PARAMIKO=True
        logging.getLogger("paramiko").setLevel(logging.WARNING)
    except ImportError:
        pass

class MyAutoAddPolicy(object):
    """
    Modified version of AutoAddPolicy in paramiko so we can determine when keys are added.

    Policy for automatically adding the hostname and new host key to the
    local L{HostKeys} object, and saving it.  This is used by L{SSHClient}.
    """

    def missing_host_key(self, client, hostname, key):

        key._added_by_ansible_this_time = True

        # existing implementation below:
        client._host_keys.add(hostname, key.get_name(), key)
        if client._host_keys_filename is not None:
            client.save_host_keys(client._host_keys_filename)
        

# keep connection objects on a per host basis to avoid repeated attempts to reconnect

SSH_CONNECTION_CACHE = {}
SFTP_CONNECTION_CACHE = {}

class Connection(object):
    ''' SSH based connections with Paramiko '''

    def __init__(self, runner, host, port, user, password, private_key_file, *args, **kwargs):

        self.ssh = None
        self.sftp = None
        self.runner = runner
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.private_key_file = private_key_file

    def _cache_key(self):
        return "%s__%s__" % (self.host, self.user)

    def connect(self):
        cache_key = self._cache_key()
        if cache_key in SSH_CONNECTION_CACHE:
            self.ssh = SSH_CONNECTION_CACHE[cache_key]
        else:
            self.ssh = SSH_CONNECTION_CACHE[cache_key] = self._connect_uncached()
        return self

    def _connect_uncached(self):
        ''' activates the connection object '''

        if not HAVE_PARAMIKO:
            raise errors.AnsibleError("paramiko is not installed")

        vvv("ESTABLISH CONNECTION FOR USER: %s on PORT %s TO %s" % (self.user, self.port, self.host), host=self.host)

        ssh = paramiko.SSHClient()
     
        self.keyfile = os.path.expanduser("~/.ssh/known_hosts")

        if C.HOST_KEY_CHECKING:
            ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(MyAutoAddPolicy())

        allow_agent = True
        if self.password is not None:
            allow_agent = False
        try:
            if self.private_key_file:
                key_filename = os.path.expanduser(self.private_key_file)
            elif self.runner.private_key_file:
                key_filename = os.path.expanduser(self.runner.private_key_file)
            else:
                key_filename = None
            ssh.connect(self.host, username=self.user, allow_agent=allow_agent, look_for_keys=True,
                key_filename=key_filename, password=self.password,
                timeout=self.runner.timeout, port=self.port)
        except Exception, e:
            msg = str(e)
            if "PID check failed" in msg:
                raise errors.AnsibleError("paramiko version issue, please upgrade paramiko on the machine running ansible")
            elif "Private key file is encrypted" in msg:
                msg = 'ssh %s@%s:%s : %s\nTo connect as a different user, use -u <username>.' % (
                    self.user, self.host, self.port, msg)
                raise errors.AnsibleConnectionFailed(msg)
            else:
                raise errors.AnsibleConnectionFailed(msg)

        return ssh

    def exec_command(self, cmd, tmp_path, sudo_user, sudoable=False, executable='/bin/sh'):
        ''' run a command on the remote host '''

        bufsize = 4096
        try:
            chan = self.ssh.get_transport().open_session()
        except Exception, e:
            msg = "Failed to open session"
            if len(str(e)) > 0:
                msg += ": %s" % str(e)
            raise errors.AnsibleConnectionFailed(msg)

        if not self.runner.sudo or not sudoable:
            if executable:
                quoted_command = executable + ' -c ' + pipes.quote(cmd)
            else:
                quoted_command = cmd
            vvv("EXEC %s" % quoted_command, host=self.host)
            chan.exec_command(quoted_command)
        else:
            # sudo usually requires a PTY (cf. requiretty option), therefore
            # we give it one, and we try to initialise from the calling
            # environment
            chan.get_pty(term=os.getenv('TERM', 'vt100'),
                         width=int(os.getenv('COLUMNS', 0)),
                         height=int(os.getenv('LINES', 0)))
            shcmd, prompt = utils.make_sudo_cmd(sudo_user, executable, cmd)
            vvv("EXEC %s" % shcmd, host=self.host)
            sudo_output = ''
            try:
                chan.exec_command(shcmd)
                if self.runner.sudo_pass:
                    while not sudo_output.endswith(prompt):
                        chunk = chan.recv(bufsize)
                        if not chunk:
                            if 'unknown user' in sudo_output:
                                raise errors.AnsibleError(
                                    'user %s does not exist' % sudo_user)
                            else:
                                raise errors.AnsibleError('ssh connection ' +
                                    'closed waiting for password prompt')
                        sudo_output += chunk
                    chan.sendall(self.runner.sudo_pass + '\n')
            except socket.timeout:
                raise errors.AnsibleError('ssh timed out waiting for sudo.\n' + sudo_output)

        stdout = ''.join(chan.makefile('rb', bufsize))
        stderr = ''.join(chan.makefile_stderr('rb', bufsize))
        return (chan.recv_exit_status(), '', stdout, stderr)

    def put_file(self, in_path, out_path):
        ''' transfer a file from local to remote '''
        vvv("PUT %s TO %s" % (in_path, out_path), host=self.host)
        if not os.path.exists(in_path):
            raise errors.AnsibleFileNotFound("file or module does not exist: %s" % in_path)
        try:
            self.sftp = self.ssh.open_sftp()
        except Exception, e:
            raise errors.AnsibleError("failed to open a SFTP connection (%s)" % e)
        try:
            self.sftp.put(in_path, out_path)
        except IOError:
            raise errors.AnsibleError("failed to transfer file to %s" % out_path)

    def _connect_sftp(self):
        cache_key = "%s__%s__" % (self.host, self.user)
        if cache_key in SFTP_CONNECTION_CACHE:
            return SFTP_CONNECTION_CACHE[cache_key]
        else:
            result = SFTP_CONNECTION_CACHE[cache_key] = self.connect().ssh.open_sftp()
            return result

    def fetch_file(self, in_path, out_path):
        ''' save a remote file to the specified path '''
        vvv("FETCH %s TO %s" % (in_path, out_path), host=self.host)
        try:
            self.sftp = self._connect_sftp()
        except Exception, e:
            raise errors.AnsibleError("failed to open a SFTP connection (%s)", e)
        try:
            self.sftp.get(in_path, out_path)
        except IOError:
            raise errors.AnsibleError("failed to transfer file from %s" % in_path)

    def _save_ssh_host_keys(self, filename):
        ''' 
        not using the paramiko save_ssh_host_keys function as we want to add new SSH keys at the bottom so folks 
        don't complain about it :) 
        '''

        added_any = False        
        for hostname, keys in self.ssh._host_keys.iteritems():
            for keytype, key in keys.iteritems():
                added_this_time = getattr(key, '_added_by_ansible_this_time', False)
                if added_this_time:
                    added_any = True
                    break

        if not added_any:
            return

        path = os.path.expanduser("~/.ssh")
        if not os.path.exists(path):
            os.makedirs(path)

        f = open(filename, 'w')
        for hostname, keys in self.ssh._host_keys.iteritems():
            for keytype, key in keys.iteritems():
               # was f.write
               added_this_time = getattr(key, '_added_by_ansible_this_time', False)
               if not added_this_time:
                   f.write("%s %s %s\n" % (hostname, keytype, key.get_base64()))
        for hostname, keys in self.ssh._host_keys.iteritems():
            for keytype, key in keys.iteritems():
               added_this_time = getattr(key, '_added_by_ansible_this_time', False)
               if added_this_time:
                   f.write("%s %s %s\n" % (hostname, keytype, key.get_base64()))
        f.close()

    def close(self):
        ''' terminate the connection '''
        cache_key = self._cache_key()
        SSH_CONNECTION_CACHE.pop(cache_key, None)
        SFTP_CONNECTION_CACHE.pop(cache_key, None)
        if self.sftp is not None:
            self.sftp.close()

        # add any new SSH host keys
        lockfile = self.keyfile.replace("known_hosts",".known_hosts.lock") 
        KEY_LOCK = open(lockfile, 'w')
        fcntl.flock(KEY_LOCK, fcntl.LOCK_EX)
        
        try:
            # just in case any were added recently
            self.ssh.load_system_host_keys()
            self.ssh._host_keys.update(self.ssh._system_host_keys)
            #self.ssh.save_host_keys(self.keyfile)
            self._save_ssh_host_keys(self.keyfile)
        except:
            # unable to save keys, including scenario when key was invalid
            # and caught earlier
            traceback.print_exc()
            pass
        fcntl.flock(KEY_LOCK, fcntl.LOCK_UN)

        self.ssh.close()
        
