# Copyright 2019 Uber Technologies, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import collections
import copy
import errno
import math
import os
import signal
import sys
import threading
import time

try:
    from shlex import quote
except ImportError:
    from pipes import quote

from horovod.run.common.util import env as env_util, safe_shell_exec
from horovod.run.common.util.hosts import get_host_assignments, parse_hosts
from horovod.run.driver import driver_service
from horovod.run.elastic.driver import ElasticDriver
from horovod.run.elastic.rendezvous import create_rendezvous_handler
from horovod.run.http.http_server import RendezvousServer
from horovod.run.util import network, threads


def _pad_rank(rank, size):
    width = int(math.log10(size - 1)) + 1
    return str(rank).zfill(width)


def _mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


class MultiFile(object):
    def __init__(self, files):
        self._files = files

    def write(self, text):
        for f in self._files:
            f.write(text)

    def flush(self):
        for f in self._files:
            f.flush()


def _slot_info_to_command_fn(run_command, env):
    # TODO: Workaround for over-buffered outputs. Investigate how mpirun avoids this problem.
    env = copy.copy(env)  # copy env so we do not leak env modifications
    env['PYTHONUNBUFFERED'] = '1'

    def slot_info_to_command(slot_info):
        """
        Given a slot_info, creates a command used by gloo to launch a single job.

        :param slot_info: host and slot to execute the run command on
        :return:
        """
        host_name = slot_info.hostname
        horovod_rendez_env = (
            'HOROVOD_HOSTNAME={hostname} '
            'HOROVOD_RANK={rank} '
            'HOROVOD_SIZE={size} '
            'HOROVOD_LOCAL_RANK={local_rank} '
            'HOROVOD_LOCAL_SIZE={local_size} '
            'HOROVOD_CROSS_RANK={cross_rank} '
            'HOROVOD_CROSS_SIZE={cross_size} '
                .format(hostname=host_name,
                        rank=slot_info.rank,
                        size=slot_info.size,
                        local_rank=slot_info.local_rank,
                        local_size=slot_info.local_size,
                        cross_rank=slot_info.cross_rank,
                        cross_size=slot_info.cross_size))

        return '{horovod_env} {env} {run_command}' .format(
            horovod_env=horovod_rendez_env,
            env=' '.join(['%s=%s' % (key, quote(value)) for key, value in env.items()
                          if env_util.is_exportable(key)]),
            run_command=run_command)

    return slot_info_to_command


def _create_elastic_worker_fn(exec_command, run_command, env, event):
    get_command_with_env = _slot_info_to_command_fn(run_command, env)

    def create_worker(slot_info, events):
        command = get_command_with_env(slot_info)
        events = [event] + (events or [])
        return exec_command(command, slot_info, events)
    return create_worker


def _exec_command_fn(settings, join_streams=True):
    """
    executes the jobs defined by run command on hosts.
    :param hosts_alloc: list of dict indicating the allocating info.
    For example,
        [{'Hostname':'worker-0', 'Rank': 0, 'Local_rank': 0, 'Cross_rank':0,
            'Size':2, 'Local_size':1, 'Cross_size':2},
        {'Hostname':'worker-1', 'Rank': 1, 'Local_rank': 0, 'Cross_rank':1,
            'Size':2, 'Local_size':1, 'Cross_size':2}
        ]
    :type hosts_alloc: list(dict)
    :param remote_host_names: names that are resolved to one of the addresses
    of remote hosts interfaces.
    :type remote_host_names: set
    :param _run_command: command to execute
    :type _run_command: string
    :return:
    :rtype:
    """
    ssh_port_arg = '-p {ssh_port}'.format(ssh_port=settings.ssh_port) if settings.ssh_port else ''

    def _exec_command(command, slot_info, events):
        index = slot_info.rank
        host_name = slot_info.hostname

        host_address = network.resolve_host_address(host_name)
        local_addresses = network.get_local_host_addresses()
        if host_address not in local_addresses:
            command = 'ssh -o StrictHostKeyChecking=no {host} {ssh_port_arg} ' \
                      '{local_command}'\
                .format(host=host_name,
                        ssh_port_arg=ssh_port_arg,
                        local_command=quote('cd {pwd} > /dev/null 2>&1 ; {local_command}'
                                            .format(pwd=os.getcwd(), local_command=command)))

        if settings.verbose:
            print(command)

        # Redirect output if requested
        stdout = stderr = None
        stdout_file = stderr_file = None
        if settings.output_filename:
            padded_rank = _pad_rank(index, settings.num_proc)
            output_dir_rank = os.path.join(settings.output_filename, 'rank.{rank}'.format(rank=padded_rank))
            if not os.path.exists(output_dir_rank):
                os.mkdir(output_dir_rank)

            stdout_file = open(os.path.join(output_dir_rank, 'stdout'), 'w')
            stderr_file = open(os.path.join(output_dir_rank, 'stderr'), 'w')

            stdout = MultiFile([sys.stdout, stdout_file])
            stderr = MultiFile([sys.stderr, stderr_file])

        try:
            exit_code = safe_shell_exec.execute(command, index=index, stdout=stdout, stderr=stderr, events=events,
                                                join_streams=join_streams)
            if exit_code != 0:
                print('Process {idx} exit with status code {ec}.'.format(idx=index, ec=exit_code))
        except Exception as e:
            print('Exception happened during safe_shell_exec, exception '
                  'message: {message}'.format(message=e))
            exit_code = 1
        finally:
            if stdout_file:
                stdout_file.close()
            if stderr_file:
                stderr_file.close()
        return exit_code, time.time()

    return _exec_command


def get_run_command(command, server_ip, nics, port, elastic=False):
    run_command = (
        'HOROVOD_GLOO_RENDEZVOUS_ADDR={addr} '
        'HOROVOD_GLOO_RENDEZVOUS_PORT={port} '
        'HOROVOD_CONTROLLER=gloo '
        'HOROVOD_CPU_OPERATIONS=gloo '
        'HOROVOD_GLOO_IFACE={iface} '
        'NCCL_SOCKET_IFNAME={nics} '
        '{elastic} '
        '{command}'  # expect a lot of environment variables
            .format(addr=server_ip,
                    port=port,
                    iface=list(nics)[0],  # TODO: add multiple ifaces in future
                    nics=','.join(nics),
                    elastic='HOROVOD_ELASTIC=1' if elastic else '',
                    command=' '.join(quote(par) for par in command)))
    return run_command


def register_shutdown_event():
    # Create a event for communication between threads
    event = threading.Event()

    def set_event_on_signal(signum, frame):
        event.set()

    signal.signal(signal.SIGINT, set_event_on_signal)
    signal.signal(signal.SIGTERM, set_event_on_signal)
    return event

def launch_gloo(command, exec_command, settings, nics, env, server_ip):
    """
    Launches the given command multiple times using gloo.
    Each command is launched via exec_command.

    :param command: command to launch
    :param exec_command: means to execute a single command
    :param settings: settings for the distribution
    :param nics: common interfaces
    :param env: environment to use
    :param server_ip: ip to use for rendezvous server
    """
    # Make the output directory if it does not exist
    if settings.output_filename:
        _mkdir_p(settings.output_filename)

    # start global rendezvous server and get port that it is listening on
    rendezvous = RendezvousServer(settings.verbose)

    # allocate processes into slots
    hosts = parse_hosts(settings.hosts)
    host_alloc_plan = get_host_assignments(hosts, settings.num_proc)

    # start global rendezvous server and get port that it is listening on
    global_rendezv_port = rendezvous.start_server()
    rendezvous.httpd.init(host_alloc_plan)
    run_command = get_run_command(command, server_ip, nics, global_rendezv_port)

    slot_info_to_command = _slot_info_to_command_fn(run_command, env)
    event = register_shutdown_event()
    args_list = [[slot_info_to_command(slot_info), slot_info, [event]]
                 for slot_info in host_alloc_plan]

    # If an error occurs in one thread, entire process will be terminated.
    # Otherwise, threads will keep running.
    res = threads.execute_function_multithreaded(exec_command,
                                                 args_list,
                                                 block_until_all_done=True)

    for name, value in sorted(res.items(), key=lambda item: item[1][1]):
        exit_code, timestamp = value
        if exit_code != 0:
            raise RuntimeError('Horovod detected that one or more processes exited with non-zero '
                               'status, thus causing the job to be terminated. The first process '
                               'to do so was:\nProcess name: {name}\nExit code: {code}\n'
                               .format(name=name, code=exit_code))


def gloo_run(settings, nics, env, server_ip, command):
    # Each thread will use ssh command to launch the job on each remote host. If an
    # error occurs in one thread, entire process will be terminated. Otherwise,
    # threads will keep running and ssh session.
    exec_command = _exec_command_fn(settings)
    launch_gloo(command, exec_command, settings, nics, env, server_ip)


def gloo_run_elastic(settings, env, command):
    # Make the output directory if it does not exist
    if settings.output_filename:
        _mkdir_p(settings.output_filename)

    rendezvous = RendezvousServer(settings.verbose)
    driver = ElasticDriver(rendezvous, settings.discovery,
                           settings.min_np, settings.max_np,
                           verbose=settings.verbose)

    handler = create_rendezvous_handler(driver)
    global_rendezv_port = rendezvous.start_server(handler)
    driver.wait_for_available_hosts(settings.num_proc)

    nics = driver_service.get_common_interfaces(settings, driver.get_available_hosts())
    server_ip = network.get_driver_ip(nics)

    exec_command = _exec_command_fn(settings, join_streams=False)
    event = register_shutdown_event()
    run_command = get_run_command(command, server_ip, nics, global_rendezv_port, elastic=True)
    create_worker = _create_elastic_worker_fn(exec_command, run_command, env, event)

    driver.start(settings.num_proc, create_worker)
    res = driver.get_results()
    driver.stop()

    for name, value in sorted(res.items(), key=lambda item: item[1][1]):
        exit_code, timestamp = value
        if exit_code != 0:
            raise RuntimeError('Horovod detected that one or more processes exited with non-zero '
                               'status, thus causing the job to be terminated. The first process '
                               'to do so was:\nProcess name: {name}\nExit code: {code}\n'
                               .format(name=name, code=exit_code))
