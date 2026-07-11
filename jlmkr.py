#!/usr/bin/env python3

"""
Create persistent Linux 'jails' on TrueNAS CE, with full access to \
all files via bind mounts, thanks to systemd-nspawn!
"""

__version__ = '2.1.1'
__author__ = 'Jip-Hop'
__copyright__ = 'Copyright (C) 2023, Jip-Hop'
__license__ = 'LGPL-3.0-only'
__disclaimer__ = """USE THIS SCRIPT AT YOUR OWN RISK!
IT COMES WITHOUT WARRANTY AND IS NOT SUPPORTED BY IXSYSTEMS."""

import argparse
import configparser
import hashlib
import io
import json
import os
import platform
import re
import readline
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from collections import defaultdict
from inspect import cleandoc
from packaging.version import parse as parse_version
from pathlib import Path
from textwrap import dedent

DEFAULT_CONFIG = """startup=0
gpu_passthrough_intel=0
gpu_passthrough_nvidia=0

# Force Proprietary NVIDIA drivers to be installed on TrueNAS CE Goldeye
# or newer
force_nvidia_legacy_driver=0

# Turning off seccomp filtering improves performance at the expense of
# security
seccomp=1

# Below you may add additional systemd-nspawn flags behind
# systemd_nspawn_user_args=
# To mount host storage in the jail, you may add: --bind='/mnt/pool/dataset:/home'
# To readonly mount host storage, you may add: --bind-ro=/etc/certificates
# To use macvlan networking add: --network-macvlan=eno1 --resolv-conf=bind-host
# To use bridge networking add: --network-bridge=br1 --resolv-conf=bind-host
# Ensure to change eno1/br1 to the interface name you want to use
# To allow syscalls required by docker add: --system-call-filter='add_key keyctl bpf'
systemd_nspawn_user_args=

# Specify command/script to run on the HOST before starting the jail
# For example to load kernel modules and config kernel settings
pre_start_hook=
# pre_start_hook=#!/usr/bin/bash
#     set -euo pipefail
#     echo 'PRE_START_HOOK_EXAMPLE'
#     echo 1 > /proc/sys/net/ipv4/ip_forward
#     modprobe br_netfilter
#     echo 1 > /proc/sys/net/bridge/bridge-nf-call-iptables
#     echo 1 > /proc/sys/net/bridge/bridge-nf-call-ip6tables

# Specify command/script to run on the HOST after starting the jail
# For example to attach to multiple bridge interfaces
# when using --network-veth-extra=ve-myjail-1:veth1
post_start_hook=
# post_start_hook=#!/usr/bin/bash
#     set -euo pipefail
#     echo 'POST_START_HOOK_EXAMPLE'
#     ip link set dev ve-myjail-1 master br2
#     ip link set dev ve-myjail-1 up

# Specify a command/script to run on the HOST after stopping the jail
post_stop_hook=
# post_stop_hook=echo 'POST_STOP_HOOK_EXAMPLE'

# Only used while creating the jail
distro=debian
release=bookworm

# Specify command/script to run IN THE JAIL on the first start (once
# networking is ready in the jail). Useful to install packages on top of
# the base rootfs
initial_setup=
# initial_setup=bash -c 'apt-get update && apt-get -y upgrade'

# Usually no need to change systemd_run_default_args
systemd_run_default_args=--collect
    --property=Delegate=yes
    --property=RestartForceExitStatus=133
    --property=SuccessExitStatus=133
    --property=TasksMax=infinity
    --property=Type=notify
    --setenv=SYSTEMD_NSPAWN_LOCK=0
    --property=KillMode=mixed

# Usually no need to change systemd_nspawn_default_args
systemd_nspawn_default_args=--bind-ro=/sys/module
    --boot
    --inaccessible=/sys/module/apparmor
    --quiet
    --keep-unit"""

# Use mostly default settings for systemd-nspawn but with systemd-run
# instead of a service file:
# <https://github.com/systemd/systemd/blob/main/units/systemd-nspawn%40.service.in>
#
# Use TasksMax=infinity since this is what Docker does:
# <https://github.com/docker/engine/blob/master/contrib/init/systemd/docker.service>
#
# Use SYSTEMD_NSPAWN_LOCK=0; otherwise jail won't start after a shutdown
#
# Would give "directory tree currently busy" error and I'd have to run
# `rm /run/systemd/nspawn/locks/*` and remove the .lck file from
# jail_path
#
# Disabling locking isn't a big deal as systemd-nspawn will prevent
# starting a container with the same name anyway: as long as jails are
# being started using this script, it won't be possible to start the
# same jail twice
#
# Always add --bind-ro=/sys/module to make lsmod happy
# <https://manpages.debian.org/bookworm/manpages/sysfs.5.en.html>

DOWNLOAD_SCRIPT_DIGEST = '645ba65a8846a2f402fc8bd870029b95fbcd3128e3046cd55642d577652cb0a0'
DOWNLOAD_SCRIPT_URL = 'https://raw.githubusercontent.com/Jip-Hop/lxc/b24d2d45b3875b013131b480e61c93b6fb8ea70c/templates/lxc-download.in'
MULTIARCH_ROOT_PATH = '/usr/lib/x86_64-linux-gnu'
SYSEXT_PATH = '/usr/share/truenas/sysext-extensions'
SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_NAME = SCRIPT_PATH.name
SCRIPT_DIR_PATH = SCRIPT_PATH.parent
JAILS_DIR_PATH = SCRIPT_DIR_PATH / 'jails'
JAIL_CONFIG_NAME = 'config'
JAIL_ROOTFS_NAME = 'rootfs'
SHORTNAME = 'jlmkr'
VERSION_GOLDEYE = '25.10'


# Colors are only needed for an interactive TTY
is_tty = sys.stdout.isatty()
BOLD = '\033[1m' if is_tty else ''
RED = '\033[91m' if is_tty else ''
YELLOW = '\033[93m' if is_tty else ''
UNDERLINE = '\033[4m' if is_tty else ''
NORMAL = '\033[0m' if is_tty else ''

DISCLAIMER = f"""{YELLOW}{BOLD}{__disclaimer__}{NORMAL}"""

# Used in parser getters to indicate the default behavior when a
# specific option is not found, which is needed to ensure `None` as a
# valid fallback value.
_UNSET = object()


class KeyValueParser(configparser.ConfigParser):
    """
    Simple comment preserving parser based on ConfigParser. Reads a file
    containing key/value pairs and/or comments. Values can span multiple
    lines, as long as they are indented deeper than the first line of
    the value. Comments or keys must NOT be indented.
    """

    def __init__(self, *args, **kwargs):
        # Set defaults if not specified by user
        if 'interpolation' not in kwargs:
            kwargs['interpolation'] = None
        if 'allow_no_value' not in kwargs:
            kwargs['allow_no_value'] = True
        if 'comment_prefixes' not in kwargs:
            kwargs['comment_prefixes'] = '#'

        super().__init__(*args, **kwargs)

        # Backup _comment_prefixes
        self._comment_prefixes_backup = self._comment_prefixes

        # Unset _comment_prefixes so comments won't be skipped
        self._comment_prefixes = ()

        # Starting point for the comment IDs
        self._comment_id = 0

        # Default delimiter to use
        delimiter = self._delimiters[0]

        # Template to store comments as key value pair
        self._comment_template = '#{0} ' + delimiter + ' {1}'

        # Regex to match the comment prefix
        self._comment_regex = re.compile(r'^#\d+\s*' + re.escape(delimiter) + r'[^\S\n]*')

        # Regex to match cosmetic newlines (skips newlines in multiline
        # values);  consecutive whitespace from start of line followed
        # by a line not starting with whitespace
        self._cosmetic_newlines_regex = re.compile(r'^(\s+)(?=^\S)', re.MULTILINE)

        # Dummy section name
        self._section_name = 'a'

    def _find_cosmetic_newlines(self, text):
        # Indices of the lines containing cosmetic newlines
        cosmetic_newline_indices = set()

        for match in re.finditer(self._cosmetic_newlines_regex, text):
            start_index = text.count('\n', 0, match.start())
            end_index = start_index + text.count('\n', match.start(), match.end())
            cosmetic_newline_indices.update(range(start_index, end_index))

        return cosmetic_newline_indices

    # TODO: Create a solution which doesn't depend on the internal _read method
    def _read(self, fp, fpname):
        lines = fp.readlines()
        cosmetic_newline_indices = self._find_cosmetic_newlines(''.join(lines))

        # Preprocess config file to preserve comments
        for i, line in enumerate(lines):
            if i in cosmetic_newline_indices or line.startswith(self._comment_prefixes_backup):
                # Store cosmetic newline or comment with unique key
                lines[i] = self._comment_template.format(self._comment_id, line)
                self._comment_id += 1

        # Convert to in-memory file and prepend a dummy section header
        lines = io.StringIO(f'[{self._section_name}]\n' + ''.join(lines))

        # Feed preprocessed file to original _read method
        return super()._read(lines, fpname)

    def read_default_string(self, string, source='<string>'):
        # Ignore all comments when parsing default key/values
        string = [l for l in string.splitlines() if not l.startswith(self._comment_prefixes_backup)]
        string = '\n'.join(string)

        # Feed preprocessed file to original _read method
        return super()._read(io.StringIO('[DEFAULT]\n' + string), source)

    def write(self, fp, space_around_delimiters=False):
        # Write the config to an in-memory file
        with io.StringIO() as sfile:
            super().write(sfile, space_around_delimiters)

            # Start from the beginning of sfile
            sfile.seek(0)

            line = sfile.readline()

            # Throw away lines until the dummy section header is reached
            while line.strip() != f'[{self._section_name}]':
                line = sfile.readline()

            lines = sfile.readlines()

        for i, line in enumerate(lines):
            # Remove the comment id prefix
            lines[i] = self._comment_regex.sub('', line, 1)

        fp.write(''.join(lines).rstrip())

    # Set value for specified option key
    def my_set(self, option, value):
        if isinstance(value, bool):
            value = str(int(value))
        elif isinstance(value, list):
            value = str('\n    '.join(value))
        elif not isinstance(value, str):
            value = str(value)

        super().set(self._section_name, option, value)

    # Return value for specified option key
    def my_get(self, option, fallback=_UNSET):
        return super().get(self._section_name, option, fallback=fallback)

    # Return value converted to boolean for specified option key
    def my_getboolean(self, option, fallback=_UNSET):
        return super().getboolean(self._section_name, option, fallback=fallback)


class ExceptionWithParser(Exception):
    def __init__(self, parser, message):
        self.parser = parser
        self.message = message
        super().__init__(message)


class CustomSubParser(argparse.ArgumentParser):
    """
    Workaround for exit_on_error=False not applying to:
    "error: the following arguments are required"

    <https://github.com/python/cpython/issues/103498>
    """
    def error(self, message):
        if not self.exit_on_error:
            raise ExceptionWithParser(self, message)

        super().error(message)


class Chroot:
    def __init__(self, new_root):
        self.new_root = new_root
        self.old_root = None
        self.initial_cwd = None

    def __enter__(self):
        self.old_root = os.open('/', os.O_PATH)
        self.initial_cwd = Path.cwd().absolute()
        os.chdir(self.new_root)
        os.chroot('.')

    def __exit__(self, exc_type, exc_value, traceback):
        os.chdir(self.old_root)
        os.chroot('.')
        os.close(self.old_root)
        os.chdir(self.initial_cwd)


def eprint(*args, **kwargs):
    """
    Print to stderr.
    """
    print(*args, file=sys.stderr, **kwargs)


def fail(*args, **kwargs):
    """
    Print to stderr and exit.
    """
    eprint(*args, **kwargs)
    sys.exit(1)


def nvidia_fail(error_msg):
    """
    Print custom NVIDIA error to stderr and exit.
    """
    extra_msg = 'disable the "gpu_passthrough_nvidia" and "force_nvidia_legacy_driver" settings if the problem persists'

    fail(f'{RED}ERROR: {error_msg}; {extra_msg}.{NORMAL}')


def get_jail_path(jail_name):
    return JAILS_DIR_PATH / jail_name


def get_jail_config_path(jail_name):
    return get_jail_path(jail_name) / JAIL_CONFIG_NAME


def get_jail_rootfs_path(jail_name):
    return get_jail_path(jail_name) /  JAIL_ROOTFS_NAME


# Is the NVIDIA Open Kernel driver installed?
def is_nvidia_open_driver_installed():
    driver_version_file = Path('/proc/driver/nvidia/version')

    if not driver_version_file.exists():
        nvidia_fail('NVIDIA driver missing')

    return 'Open Kernel' in driver_version_file.read_text()


def run_nvidia_smi_command(nvidia_smi_args, is_output=False):
    """
    Runs the NVIDIA System Management Interface program

    <https://docs.nvidia.com/deploy/nvidia-smi/index.html>
    """
    return subprocess.run(['nvidia-smi'] + nvidia_smi_args, check=True, capture_output=is_output)


def get_nvidia_smi_response(nvidia_smi_args):
    """
    Runs the NVIDIA System Management Interface program and returns
    the response

    <https://docs.nvidia.com/deploy/nvidia-smi/index.html>
    """
    smi = run_nvidia_smi_command(nvidia_smi_args, True)

    return smi.stdout.decode().strip()


def install_nvidia_modules():
    """
    Loads the NVIDIA Unified Virtual Memory kernel module, which will
    automatically load all other modules that are needed.
    """
    try:
        nvidia_uvm = 'nvidia-current-uvm'
        subprocess.run(['modinfo', nvidia_uvm], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        try:
            nvidia_uvm = 'nvidia-uvm'
            subprocess.run(['modinfo', nvidia_uvm], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            nvidia_fail(f'Failed to identify NVIDIA Unified Virtual Memory module: {e}')

    try:
        subprocess.run(['modprobe', nvidia_uvm], check=True)
    except subprocess.CalledProcessError as e:
        nvidia_fail(f'Failed to load NVIDIA Unified Virtual Memory module: {e}')


def uninstall_nvidia_modules():
    """
    Uninstalls all NVIDIA kernel modules.
    """
    try:
        nvidia_uvm = 'nvidia-current-uvm'
        subprocess.run(['modinfo', nvidia_uvm], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        try:
            nvidia_uvm = 'nvidia-uvm'
            subprocess.run(['modinfo', nvidia_uvm], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            nvidia_fail(f'Failed to identify NVIDIA Unified Virtual Memory module: {e}')

    # Persistence mode MUST be disabled in order to remove the NVIDIA
    # Modeset module while ignoring errors
    subprocess.run(['pkill', '-f', 'nvidia-persistenced'], stderr=subprocess.DEVNULL)
    time.sleep(1)

    try:
        modprobe = f'modprobe -r {nvidia_uvm} nvidia-drm nvidia-modeset nvidia'
        subprocess.run(modprobe, check=True, shell=True)
    except subprocess.CalledProcessError as e:
        nvidia_fail(f'Failed to remove one or more NVIDIA kernel modules: {e}')


def test_nvidia_driver():
    """
    Runs "nvidia-smi" to test the NVIDIA driver, which needs to run
    successfully, otherwise "nvidia-container-cli list" will fail as
    well.
    """
    try:
        run_nvidia_smi_command(['-f', '/dev/null'])
    except subprocess.CalledProcessError as e:
        nvidia_fail(f'Failed to test NVIDIA driver using nvidia-smi: {e}')


def is_nvidia_proprietary_driver_required():
    """
    Does the NVIDIA Proprietary driver need to be installed? The NVIDIA
    Open Kernel driver is only applicable to Turing models and newer.
    This is determined by checking if the GPU's Compute Capability is
    7.5 or higher. Anything less than this value will require the NVIDIA
    Open Kernel driver to be replaced with the Proprietary driver on
    TrueNAS CE Goldeye (25.10) and newer.

    <https://developer.nvidia.com/blog/nvidia-releases-open-source-gpu-kernel-modules/#which_gpus_are_supported_by_open_gpu_kernel_modules>
    <https://github.com/NVIDIA/open-gpu-kernel-modules?tab=readme-ov-file#compatible-gpus>
    <https://docs.nvidia.com/cuda/archive/12.6.2/cuda-c-programming-guide/index.html#compute-capability>
    <https://developer.nvidia.com/cuda/gpus>
    <https://leimao.github.io/blog/NVIDIA-GPU-Compute-Capability>
    """
    # Short-circuit because TrueNAS CE versions below Goldeye already
    # include the NVIDIA Proprietary driver
    if parse_version(get_truenas_version()) < parse_version(VERSION_GOLDEYE):
        return False

    # Was the Open Kernel driver already replaced with the Proprietary
    # driver?
    if not is_nvidia_open_driver_installed():
        return False

    compute_capability = None

    try:
        nvidia_smi_args = ['--query-gpu=compute_cap', '--format=csv,noheader']
        compute_capability = get_nvidia_smi_response(nvidia_smi_args)
    except subprocess.CalledProcessError as e:
        nvidia_fail(f'Failed to determine NVIDIA Compute Capability: {e}')

    return compute_capability and float(compute_capability) < 7.5


def get_truenas_version():
    """
    Returns the TrueNAS SemVer version that's currently installed.
    """
    os_version = Path('/etc/version')

    if not os_version.exists():
        nvidia_fail(f'Failed to determine TrueNAS because "/etc/version" is missing')

    os_version = os_version.read_text()

    if not os_version:
        nvidia_fail('Failed to determine TrueNAS version')

    return os_version


def delete_old_nvidia_open_driver_backups():
    """
    Deletes old NVIDIA Open Kernel driver backup(s) whenever TrueNAS
    gets upgraded because the Proprietary driver will get replaced with
    the latest Open Kernel driver.
    """
    sysext_dir = Path(SYSEXT_PATH)

    for back_up_file in list(sysext_dir.glob('nvidia-open-truenas*.raw')):
        ext_version = re.search(r'nvidia-open-truenas-([\d.]+).raw', str(back_up_file))
        ext_version = ext_version.group(1) if ext_version else None

        if ext_version and parse_version(ext_version) == parse_version(get_truenas_version()):
            continue

        back_up_file.unlink()


def download_nvidia_proprietary_driver():
    """
    Downloads the NVIDIA Proprietary driver when applicable.

    <https://github.com/zzzhouuu/truenas-nvidia-drivers>
    <https://truenas-drivers.zhouyou.info/index.html>
    <https://forums.truenas.com/t/nvidia-kernel-module-change-in-truenas-25-10-what-this-means-for-you/51070>
    <https://forums.truenas.com/t/nvidia-compatible-driver-test-for-truenas-25-10-goldeye/53395>
    <https://www.reddit.com/r/truenas/comments/1rn9hom/running_a_legacy_nvidia_gpu_gtx_1070_on_truenas>
    """
    os_version = get_truenas_version()
    base_driver_url = 'https://truenas-drivers.zhouyou.info'
    driver_url = f'{base_driver_url}/{os_version}/nvidia.raw'
    checksum_url = f'{base_driver_url}/{os_version}/nvidia.raw.sha256'
    driver_file = Path('/tmp/nvidia.raw')
    checksum_file = Path('/tmp/nvidia.raw.sha256')

    try:
        subprocess.run(['wget', '-q', driver_url, '-O', driver_file], check=True)
    except subprocess.CalledProcessError:
        nvidia_fail('Failed to download the NVIDIA Proprietary driver system extension')

    try:
        subprocess.run(['wget', '-q', checksum_url, '-O', checksum_file], check=True)
    except subprocess.CalledProcessError:
        nvidia_fail('Failed to download the NVIDIA Proprietary driver checksum')

    # Validate hash to ensure a successful download
    expected_checksum = checksum_file.read_text().strip()
    checksum_file.unlink(missing_ok=True)

    if not validate_sha256(driver_file, expected_checksum):
        nvidia_fail('NVIDIA Proprietary driver is corrupt and must be downloaded again')

    return driver_file


def toggle_nvidia_drivers_setting(is_enable):
    """
    Enables/disables the "Install NVIDIA Drivers" configuration setting.
    """
    # Just continue if the driver is already installed
    if is_enable and shutil.which('nvidia-smi'):
        return

    try:
        true_or_false = 'true' if is_enable else 'false'
        json = f'{{ "nvidia": {true_or_false} }}'
        midclt = f"midclt call docker.update '{json}'"

        subprocess.run(midclt, check=True, shell=True, stdout=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        enable_or_disable = 'enable' if is_enable else 'disable'
        nvidia_fail(f'Failed to {enable_or_disable} NVIDIA driver: {e}')

    # Wait for the change to take effect
    time.sleep(1)


def toggle_system_resources_dataset(is_editable):
    """
    Toggles the TrueNAS Unix System Resources (/usr) dataset to be
    editable or read-only.
    """
    try:
        zfs_list = 'zfs list -H -o name /usr'
        zfs_list = subprocess.run(zfs_list, check=True, shell=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        nvidia_fail(f'Failed to get /usr dataset: {e}')

    try:
        readonly = 'off' if is_editable else 'on'
        usr_dataset = zfs_list.stdout.decode().strip()

        subprocess.run(['zfs', 'set', f'readonly={readonly}', usr_dataset], check=True)
    except subprocess.CalledProcessError as e:
        action = 'editable' if is_editable else 'read-only'
        nvidia_fail(f'Failed to toggle /usr dataset to {action}: {e}')


def get_nvidia_proprietary_driver_file():
    """
    Returns the NVIDIA Proprietary driver file as a Path object.
    """
    return Path(f'{SYSEXT_PATH}/nvidia.raw')


def get_nvidia_open_driver_backup_file():
    """
    Returns the NVIDIA Open Kernel driver backup file as a Path object.
    """
    os_version = get_truenas_version()

    return Path(f'{SYSEXT_PATH}/nvidia-open-truenas-{os_version}.raw')


def install_nvidia_proprietary_driver():
    """
    Replaces the NVIDIA Open Kernel driver with the Proprietary driver
    when the GPU's compute capability is below 7.5, otherwise a check
    will be made to determine if the Proprietary driver needs to be
    upgraded due to TrueNAS CE being upgraded for versions 25.10 and
    higher.

    <https://github.com/zzzhouuu/truenas-nvidia-drivers>
    <https://truenas-drivers.zhouyou.info/index.html>
    <https://forums.truenas.com/t/nvidia-kernel-module-change-in-truenas-25-10-what-this-means-for-you/51070>
    <https://forums.truenas.com/t/nvidia-compatible-driver-test-for-truenas-25-10-goldeye/53395>
    <https://www.reddit.com/r/truenas/comments/1rn9hom/running_a_legacy_nvidia_gpu_gtx_1070_on_truenas>
    """
    # Was the NVIDIA Open Kernel driver already replaced with the NVIDIA
    # Proprietary driver?
    if not is_nvidia_open_driver_installed():
        return

    # Delete old backups to clear up space before proceeding with
    # installing the latest Proprietary driver
    delete_old_nvidia_open_driver_backups()

    driver_file = download_nvidia_proprietary_driver()

    # Temporarily disable the "Install NVIDIA Drivers" configuration
    # setting and uninstall the Open Kernel modules/driver
    uninstall_nvidia_modules()
    toggle_nvidia_drivers_setting(False)
    toggle_system_resources_dataset(True)

    is_installed = False
    installed_driver_file = get_nvidia_proprietary_driver_file()
    backup_driver_file = get_nvidia_open_driver_backup_file()

    try:
        # Backup the existing driver, but only if the backup doesn't
        # already exist to prevent the original from getting clobbered
        if installed_driver_file.is_file() and not backup_driver_file.is_file():
            shutil.move(installed_driver_file, backup_driver_file)

        # Move the NVIDIA Proprietary driver while the /usr dataset is
        # editable
        is_installed = shutil.move(driver_file, installed_driver_file)
    except FileNotFoundError:
        nvidia_fail('Downloaded NVIDIA Proprietary driver does not exist')
    except (PermissionError, OSError, shutil.Error) as e:
        nvidia_fail(f'Failed to move the NVIDIA Proprietary driver: {e}')
    except Exception as e:
        nvidia_fail(f'Failure occurred while moving the NVIDIA Proprietary driver: {e}')
    finally:
        # Restore the existing driver on failure; otherwise delete it
        # since the driver could always be reinstalled manually
        if not is_installed and backup_driver_file.is_file():
            shutil.move(backup_driver_file, installed_driver_file)

    # Re-enable the "Install NVIDIA Drivers" configuration setting to
    # automatically merge the system extensions, then install the
    # modules
    toggle_system_resources_dataset(False)
    toggle_nvidia_drivers_setting(True)
    install_nvidia_modules()

    # If all has gone according to plan, the Open Kernel driver should
    # no longer be installed; if it is, then something went wrong
    if is_nvidia_open_driver_installed():
        nvidia_fail('Failed to install the NVIDIA Proprietary driver')

    # Make sure "nvidia-smi" still works with the Proprietary driver
    test_nvidia_driver()


def enable_nvidia_persistence_mode():
    """
    Enables NVIDIA Persistence mode to avoid repetitively initializing
    the GPU, which will also ensure that the NVIDIA Modeset module gets
    created and initialized so that it can be bind mounted to the jail.
    """
    try:
        subprocess.run(['nvidia-persistenced'], check=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as persistenced_error:
        persistence_mode = None

        try:
            # Was persistence mode previously enabled?
            nvidia_smi_args = ['--query-gpu=persistence_mode', '--format=csv,noheader']
            persistence_mode = get_nvidia_smi_response(nvidia_smi_args)
        except subprocess.CalledProcessError as smi_error:
            nvidia_fail(f'Failed to determine NVIDIA Persistence Mode status: {smi_error}')

        if persistence_mode and 'Enabled' not in persistence_mode:
            nvidia_fail(f'Failed to initialize "NVIDIA Persistence Mode": {persistenced_error}')


def get_nvidia_driver_dependency_list(is_libraries=False):
    """
    Returns all the NVIDIA GPU dependencies that may need to be bind
    mounted to the jail.
    """
    nvidia_container_cli = ['nvidia-container-cli', 'list']

    if is_libraries:
        nvidia_container_cli.append('--libraries')

    try:
        dependencies = subprocess.run(nvidia_container_cli, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        nvidia_fail(f'Failed to identify NVIDIA driver files: {e}')

    return dependencies.stdout.decode().split('\n')


# Test Intel GPU by decoding mp4 file (output is discarded)
# Run the commands below in the jail:
# curl -o bunny.mp4 https://www.w3schools.com/html/mov_bbb.mp4
# ffmpeg -hwaccel vaapi -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format vaapi -i bunny.mp4 -f null - && echo 'SUCCESS!'

def passthrough_intel(is_gpu_passthrough_intel, systemd_nspawn_additional_args):
    """
    Enables Intel GPU passthrough from the host to the jail.
    """
    if not is_gpu_passthrough_intel:
        return

    dri_path = Path('/dev/dri')

    if not dri_path.exists():
        eprint(dedent(
            """
            No intel GPU seems to be present...
            Skip passthrough of intel GPU.
            """
        ))

        return

    systemd_nspawn_additional_args.append(f'--bind={dri_path}')


def passthrough_nvidia(
    is_gpu_passthrough_nvidia,
    is_force_nvidia_legacy_driver,
    systemd_nspawn_additional_args,
    jail_name
):
    """
    Enables NVIDIA GPU passthrough from the host to the jail.
    """
    jail_rootfs_path = get_jail_rootfs_path(jail_name)
    ld_config_file = jail_rootfs_path / f'etc/ld.so.conf.d/{SHORTNAME}-nvidia.conf'

    # Forcefully disable the "force_nvidia_legacy_driver" setting for
    # TrueNAS CE versions older than Goldeye because the NVIDIA
    # Proprietary driver will already be installed
    if parse_version(get_truenas_version()) < parse_version(VERSION_GOLDEYE):
        is_force_nvidia_legacy_driver = False

    # Should the config file be deleted, if it was created when
    # passthrough was previously enabled?
    if not is_gpu_passthrough_nvidia:
        print('Deleting the dynamic libraries config file')
        ld_config_file.unlink(missing_ok=True)

    # Should the original NVIDIA Open Kernel driver be restored, if a
    # backup of the driver exists?
    if not (is_force_nvidia_legacy_driver or is_nvidia_proprietary_driver_required()):
        backup_driver_file = get_nvidia_open_driver_backup_file()

        if backup_driver_file.is_file():
            print('Restoring the NVIDIA Open Kernel driver')
            uninstall_nvidia_modules()
            toggle_nvidia_drivers_setting(False)
            toggle_system_resources_dataset(True)

            shutil.move(backup_driver_file, get_nvidia_proprietary_driver_file())

            toggle_system_resources_dataset(False)
            toggle_nvidia_drivers_setting(True)
            install_nvidia_modules()

        # Short-circuit when both "gpu_passthrough_nvidia" and
        # "force_nvidia_legacy_driver" are disabled
        if not is_gpu_passthrough_nvidia:
            return

    # Does the system actually have an NVIDIA GPU installed?
    lspci = 'lspci -k | grep -E "VGA|3D|Display" | grep -i nvidia'
    lspci = subprocess.run(lspci, shell=True, capture_output=True)

    if not lspci.stdout.decode().strip():
        nvidia_fail('No NVIDIA GPU detected')
    if lspci.stderr:
        nvidia_fail(lspci.stderr.decode())

    # Make sure the "Install NVIDIA Drivers" configuration setting is
    # enabled, then install the modules and driver
    toggle_nvidia_drivers_setting(True)
    install_nvidia_modules()
    test_nvidia_driver()

    # Replace the NVIDIA Open Kernel driver with the NVIDIA  Proprietary
    # driver when applicable
    if is_force_nvidia_legacy_driver or is_nvidia_proprietary_driver_required():
        install_nvidia_proprietary_driver()

    # Short-circuit when the NVIDIA Proprietary driver is needed, but
    # when NVIDIA passthrough is not
    if not is_gpu_passthrough_nvidia:
        return

    # Enable Persistence mode to ensure that the NVIDIA Modeset module
    # gets installed and to avoid repetitive GPU initialization
    enable_nvidia_persistence_mode()

    # Get list of libraries
    nvidia_libraries = get_nvidia_driver_dependency_list(True)
    nvidia_libraries = set([x for x in nvidia_libraries if x])

    # Get full list of files, excluding library files
    nvidia_files = get_nvidia_driver_dependency_list()
    nvidia_files = list(set([x for x in nvidia_files if x]) ^ nvidia_libraries)

    # Ensure "nvidia-smi" is included just in case it wasn't included in
    # the list returned by "nvidia-container-cli"; if there's a
    # duplicate, it will automatically be excluded downstream
    nvidia_files.append('/usr/bin/nvidia-smi')

    # Because TrueNAS CE Goldeye replaced the NVIDIA Proprietary driver
    # with the NVIDIA Open Kernel driver, the library files, devices,
    # modules, etc. all need to be bind mounted, especially those that
    # exist directly in the root Multiarch directory
    nvidia_mounts = set()
    library_directories = set()

    for file_path in nvidia_files:
        if not Path(file_path).exists():
            # Exclude files that don't exist
            print(f"Skipped mounting {file_path}, it doesn't exist on the host...")
            continue

        # All files except for devices should be read-only
        bind_type = 'bind' if file_path.startswith('/dev/') else 'bind-ro'
        nvidia_mounts.add(f'--{bind_type}={file_path}')

    # Add library files that are directly in the root Multiarch
    # directory to the list of NVIDIA bind mounts because the Multiarch
    # directory MUST NOT be bind mounted to avoid it from getting
    # clobbered in the jail
    for file_path in nvidia_libraries:
        if not Path(file_path).exists():
            # Exclude files that don't exist
            print(f"Skipped mounting {file_path}, it doesn't exist on the host...")
            continue

        parent_dir = str(Path(file_path).parent)

        if parent_dir == MULTIARCH_ROOT_PATH:
            # Mount library files that are directly in the root
            # Multiarch path
            nvidia_mounts.add(f'--bind-ro={file_path}')
            continue

        nvidia_mounts.add(f'--bind-ro={parent_dir}')
        library_directories.add(parent_dir)

    nvidia_mounts = list(nvidia_mounts)
    nvidia_mounts.sort()

    # Does the parent directory exist for config file?
    if not ld_config_file.parent.exists():
        nvidia_fail(f'ld.so.conf.d directory is missing inside "{jail_name}"')

    if library_directories:
        print('\n'.join(x for x in library_directories), file=ld_config_file.open('w'))

        # Run ldconfig inside systemd-nspawn jail with the NVIDIA mounts
        # to ensure that the libraries get linked dynamically
        try:
            systemd_nspawn = [
                'systemd-nspawn',
                '--quiet',
                f'--machine={jail_name}',
                f'--directory={jail_rootfs_path}',
                *nvidia_mounts,
                'ldconfig',
            ]

            subprocess.run(systemd_nspawn, check=True)
        except subprocess.CalledProcessError as e:
            nvidia_fail(f'Failed to run ldconfig inside "{jail_name}": {e}')

    systemd_nspawn_additional_args += nvidia_mounts


def exec_jail(jail_name, cmd, is_return_code=True):
    """
    Executes a command in the jail with given name.
    """
    systemd_run = [
        'systemd-run',
        '--machine',
        jail_name,
        '--quiet',
        '--pipe',
        '--wait',
        '--collect',
        '--service-type=exec',
        *cmd,
    ]

    try:
        systemd_run = subprocess.run(systemd_run, check=True)
    except subprocess.CalledProcessError as e:
        if not is_return_code:
            raise e

        return e.returncode

    return systemd_run.returncode if is_return_code else systemd_run




def status_jail(jail_name, args):
    """
    Shows the status of the systemd service wrapping the jail with the
    given name.
    """
    try:
        subprocess.run(['machinectl', 'status', jail_name, *args], check=True)
    except subprocess.CalledProcessError as e:
        return e.returncode

    return 0


def log_jail(jail_name, args):
    """
    Shows the log file of the jail with given name.
    """
    try:
        subprocess.run(['journalctl', '-u', f'{SHORTNAME}-{jail_name}', *args], check=True)
    except subprocess.CalledProcessError as e:
        return e.returncode

    return 0


def shell_jail(args):
    """
    Opens a shell in the jail with given name.
    """
    try:
        subprocess.run(['machinectl', 'shell'] + args, check=True)
    except subprocess.CalledProcessError as e:
        return e.returncode

    return 0


def parse_config_file(jail_config_path):
    config = KeyValueParser()

    # Read default config to fall back to default values for keys not
    # found in the jail_config_path file
    config.read_default_string(DEFAULT_CONFIG)

    try:
        with jail_config_path.open('r') as fp:
            config.read_file(fp)
    except FileNotFoundError:
        eprint(f'Unable to find config file: {jail_config_path}')
        return None

    return config


def systemd_escape_path(path):
    """
    Escape path containing spaces, while properly handling backslashes in filenames.

    <https://manpages.debian.org/bookworm/systemd/systemd.syntax.7.en.html#QUOTING>
    <https://manpages.debian.org/bookworm/systemd/systemd.service.5.en.html#COMMAND_LINES>
    """
    path = map(lambda char: r'\s' if char == ' ' else '\\\\' if char == '\\' else char, str(path))

    return ''.join(path)


def add_hook(jail_path, systemd_run_additional_args, hook_command, hook_type):
    if not hook_command:
        return

    # Run the command directly if it doesn't start with a shebang
    if not hook_command.startswith('#!'):
        systemd_run_additional_args += [f'--property={hook_type}={hook_command}']
        return

    # Otherwise write a script file and call that
    hook_file = (jail_path / f'.{hook_type}').resolve()

    # Only write if contents are different
    if not hook_file.exists() or hook_file.read_text() != hook_command:
        print(hook_command, file=hook_file.open('w'))

    hook_file.chmod(0o700)
    systemd_run_additional_args += [f'--property={hook_type}={systemd_escape_path(hook_file)}']


def start_jail(jail_name):
    """
    Starts jail with given name.
    """
    if jail_is_running(jail_name):
        eprint(f'Skipped starting jail {jail_name}. It appears to be running already...')
        return 0

    jail_path = get_jail_path(jail_name)
    jail_config_path = get_jail_config_path(jail_name)
    jail_rootfs_path = get_jail_rootfs_path(jail_name)

    config = parse_config_file(jail_config_path)

    if not config:
        eprint('Aborting...')
        return 1

    is_seccomp = config.my_getboolean('seccomp')

    systemd_run_additional_args = [
        f'--unit={SHORTNAME}-{jail_name}',
        f'--working-directory={jail_path}',
        f'--description=My nspawn jail {jail_name} [created with Jailmaker]',
    ]

    systemd_nspawn_additional_args = [
        f'--machine={jail_name}',
        f'--directory={JAIL_ROOTFS_NAME}',
    ]

    # The systemd-nspawn manual explicitly mentions:
    # * Device nodes may not be created
    # <https://www.freedesktop.org/software/systemd/man/systemd-nspawn.html>
    #
    # * This means docker images containing device nodes can't be pulled
    # <https://github.com/moby/moby/issues/35245>
    #
    # * The solution is to use DevicePolicy=auto
    # <https://github.com/kinvolk/kube-spawn/pull/328>
    #
    # * DevicePolicy=auto is the default for systemd-run and allows
    # access to all devices as long as the --property=DeviceAllow= flag
    # isn't added
    # <https://manpages.debian.org/bookworm/systemd/systemd.resource-control.5.en.html>
    #
    # We can now successfully run:
    # $ mknod /dev/port c 1 4
    #
    # Or pull docker images containing device nodes:
    # $ docker pull oraclelinux@sha256:d49469769e4701925d5145c2676d5a10c38c213802cf13270ec3a12c9c84d643

    # Add hooks to execute commands on the host before/after starting
    # and after stopping a jail
    add_hook(jail_path, systemd_run_additional_args, config.my_get('pre_start_hook'), 'ExecStartPre')
    add_hook(jail_path, systemd_run_additional_args, config.my_get('post_start_hook'), 'ExecStartPost')
    add_hook(jail_path, systemd_run_additional_args, config.my_get('post_stop_hook'), 'ExecStopPost')

    passthrough_intel(config.my_getboolean('gpu_passthrough_intel'), systemd_nspawn_additional_args)
    passthrough_nvidia(
        config.my_getboolean('gpu_passthrough_nvidia'),
        config.my_getboolean('force_nvidia_legacy_driver'),
        systemd_nspawn_additional_args,
        jail_name
    )

    if is_seccomp is False:
        # Disabling seccomp filtering by passing
        # --setenv=SYSTEMD_SECCOMP=0 to systemd-run will improve
        # performance at the expense of security; it allows syscalls
        # which otherwise would be blocked or would have to be
        # explicitly allowed by passing --system-call-filter to
        # systemd-nspawn
        # <https://github.com/systemd/systemd/issues/18370>
        #
        # However, an additional layer of seccomp filtering may be
        # undesirable. For example when using docker to run containers
        # inside the jail created with systemd-nspawn. Even though
        # seccomp filtering is disabled for the systemd-nspawn jail
        # itself, docker can still use seccomp filtering to restrict the
        # actions available within its containers.
        #
        # Proof that seccomp can be used inside a jail started with
        # --setenv=SYSTEMD_SECCOMP=0
        #
        # Run a command in a docker container which is blocked by the
        # default docker seccomp profile:
        #
        # $ docker run --rm -it debian:jessie unshare --map-root-user --user sh -c whoami
        # unshare: unshare failed: Operation not permitted
        #
        # Now run unconfined to show command runs successfully:
        #
        # $ docker run --rm -it --security-opt seccomp=unconfined debian:jessie unshare --map-root-user --user sh -c whoami
        # root
        systemd_run_additional_args += ['--setenv=SYSTEMD_SECCOMP=0']

    initial_setup = False

    # When there's no Machine ID, then this indicates that this is the
    # first time the jail is started
    machine_id = jail_rootfs_path / 'etc/machine-id'

    # Only initialize "initial_setup" when creating a new jail (from a
    # config template)
    if not machine_id.exists() and (initial_setup := config.my_get('initial_setup')):
        # Ensure the jail init system is ready before executing
        # everything in "initial_setup"
        systemd_nspawn_additional_args += ['--notify-ready=yes']

    systemd_run = [
        'systemd-run',
        *shlex.split(config.my_get('systemd_run_default_args')),
        *systemd_run_additional_args,
        '--',
        'systemd-nspawn',
        *shlex.split(config.my_get('systemd_nspawn_default_args')),
        *systemd_nspawn_additional_args,
        *shlex.split(config.my_get('systemd_nspawn_user_args')),
    ]

    print(dedent(
        f"""
        Starting jail {jail_name} with the following command:

        {shlex.join(systemd_run)}
        """
    ))

    try:
        systemd_run = subprocess.run(systemd_run, check=True)
    except subprocess.CalledProcessError as e:
        eprint(dedent(
            f"""
            Failed to start jail {jail_name}...
            In case of a config error, you may fix it with:
            {SCRIPT_NAME} edit {jail_name}
            """
        ))

        return e.returncode

    return_code = systemd_run.returncode

    # Handle the initial setup after jail is up and running (for the
    # first time from a config template)
    if initial_setup:
        if not initial_setup.startswith('#!'):
            initial_setup = f'#!/bin/sh\n{initial_setup}'

        with tempfile.NamedTemporaryFile(
            mode='w+t',
            prefix='jlmkr-initial-setup.',
            dir=jail_rootfs_path,
            delete=False
        ) as initial_setup_file:
            # Write a script file to call during initial setup
            initial_setup_file.write(initial_setup)

        initial_setup_file_name = Path(initial_setup_file.name).name

        initial_setup_file_host_path = Path(initial_setup_file.name).resolve()
        initial_setup_file_host_path.chmod(0o700)

        print(f'About to run the initial setup script: {initial_setup_file_name}.')
        print('Waiting for networking in the jail to be ready.')
        print('Please wait (this may take 90s in case of bridge networking with STP is enabled)...')

        try:
            systemd_run = [
                '--',
                'systemd-run',
                f'--unit={initial_setup_file_name}',
                '--quiet',
                '--pipe',
                '--wait',
                '--service-type=exec',
                '--property=After=network-online.target',
                '--property=Wants=network-online.target',
                f'/{initial_setup_file_name}',
            ]
            systemd_run = exec_jail(jail_name, systemd_run, False)
        except subprocess.CalledProcessError as e:
            eprint('Tried to run the following commands inside the jail:')
            eprint(initial_setup)
            eprint()
            eprint(f'{RED}{BOLD}Failed to run initial setup...')
            eprint(f'You may want to manually run /{initial_setup_file_name} inside the jail for debugging purposes.')
            eprint(f'Or stop and remove the jail and try again.{NORMAL}')

            return e.returncode

        return_code = systemd_run.returncode

        # Clean up the initial_setup_file_host_path
        Path(initial_setup_file_host_path).unlink(missing_ok=True)
        print(f'Done with initial setup of jail {jail_name}!')

    return return_code


def restart_jail(jail_name):
    """
    Restart jail with given name.
    """
    return_code = stop_jail(jail_name)

    if return_code != 0:
        eprint('Abort restart.')
        return return_code

    return start_jail(jail_name)


def cleanup(jail_path):
    """
    Clean up jail.
    """
    if get_zfs_dataset(jail_path):
        eprint(f'Cleaning up: {jail_path}.')

        remove_zfs_dataset(jail_path)
        return

    # Workaround for shutil.rmtree() FileNotFoundError race condition,
    # which should be fixed in Python 3.13
    #
    # <https://github.com/python/cpython/issues/73885>
    # <https://stackoverflow.com/a/70549000>
    if jail_path.is_dir():
        def _onerror(func, path, exc_info):
            exc_type, exc_value, exc_traceback = exc_info

            if not issubclass(exc_type, FileNotFoundError):
                raise exc_value

            if issubclass(exc_type, PermissionError):
                # Update the file permissions with the immutable and
                # append-only bit cleared
                subprocess.run(['chattr', '-i', '-a', path], stderr=subprocess.DEVNULL)

                # Reattempt the removal
                func(path)

        eprint(f'Cleaning up: {jail_path}.')
        shutil.rmtree(jail_path, onerror=_onerror)


def input_with_default(prompt, default):
    """
    Ask user for input with a default value already provided.
    """
    readline.set_startup_hook(lambda: readline.insert_text(default))

    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()


def validate_sha256(file_path, digest):
    """
    Validates if a file matches a sha256 digest.
    """
    file_hash = None

    try:
        with file_path.open('rb') as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return False

    return file_hash == digest


def run_lxc_download_script(
    jail_name=None,
    jail_path=None,
    jail_rootfs_path=None,
    distro=None,
    release=None
):
    """
    Fetches the Jip-Hop LXC download script and executes it to return a
    list available images to create jails from.
    """
    arch = 'amd64'
    lxc_dir = Path('.lxc')
    lxc_cache = lxc_dir / 'cache'
    lxc_download_script = lxc_dir / 'lxc-download.sh'
    env_vars = {'LXC_CACHE_PATH': str(lxc_cache)}

    # Create the LXC directories when needed
    lxc_dir.mkdir(parents=True, exist_ok=True)
    lxc_dir.chmod(0o700)

    lxc_cache.mkdir(parents=True, exist_ok=True)
    lxc_cache.chmod(0o700)

    try:
        if lxc_download_script.stat().st_uid != 0:
            lxc_download_script.unlink()
    except FileNotFoundError:
        pass

    # Get the LXC download script if not present locally (or when the
    # hash doesn't match)
    if not validate_sha256(lxc_download_script, DOWNLOAD_SCRIPT_DIGEST):
        urllib.request.urlretrieve(DOWNLOAD_SCRIPT_URL, lxc_download_script)

        if not validate_sha256(lxc_download_script, DOWNLOAD_SCRIPT_DIGEST):
            eprint('Abort! Downloaded script has unexpected contents.')
            return 1

    lxc_download_script.chmod(0o700)

    if None in [jail_name, jail_path, jail_rootfs_path, distro, release]:
        # List images
        lxc_download = [lxc_download_script, '--list', f'--arch={arch}']
        lxc_list = subprocess.Popen(lxc_download, stdout=subprocess.PIPE, env=env_vars)

        for line in iter(lxc_list.stdout.readline, b''):
            line = line.decode().strip()

            # Filter out the known incompatible distros
            exclude = r'^(alpine|amazonlinux|busybox|devuan|funtoo|openwrt|plamo|voidlinux)\s'

            if not re.match(exclude, line):
                print(line)

        return lxc_list.wait()

    try:
        lxc_download = [
            lxc_download_script,
            f'--name={jail_name}',
            f'--path={jail_path}',
            f'--rootfs={jail_rootfs_path}',
            f'--arch={arch}',
            f'--dist={distro}',
            f'--release={release}',
        ]
        subprocess.run(lxc_download, check=True, env=env_vars)
    except subprocess.CalledProcessError as e:
        eprint('Aborting...')
        return e.returncode

    return 0


def agree(question, default=None):
    """
    Ask user a yes/no question.
    """
    hint = '[Y/n]' if default == 'y' else ('[y/N]' if default == 'n' else '[y/n]')

    while True:
        user_input = input(f'{question} {hint} ') or default

        if user_input.lower() in ['y', 'n']:
            return user_input.lower() == 'y'

        eprint('Invalid input. Please type "y" for yes or "n" for no and press enter.')


def get_mount_point(path):
    """
    Return the mount point on which the given path resides.
    """
    path = path.resolve()

    while not path.is_mount():
        path = path.parent

    return path


def get_relative_path_in_jailmaker_dir(absolute_path):
    return absolute_path.relative_to(SCRIPT_DIR_PATH)


def get_zfs_dataset(path):
    """
    Get ZFS dataset path.
    """
    def clean_field(field):
        # Restore spaces that were encoded
        #
        # <https://github.com/openzfs/zfs/issues/11182>
        return field.replace('\\040', ' ')

    path = str(path.resolve())
    mounts = Path('/proc/mounts')

    with mounts.open('r') as f:
        for line in f:
            fields = line.split()
            relative_path = fields[0]
            absolute_path = fields[1]
            fs_type = fields[2]

            if 'zfs' == fs_type and path == clean_field(absolute_path):
                return clean_field(relative_path)

    return ''


def get_zfs_base_path():
    """
    Get ZFS dataset path for Jailmaker directory.
    """
    zfs_base_path = get_zfs_dataset(SCRIPT_DIR_PATH)

    if not zfs_base_path:
        fail('Failed to get dataset path for Jailmaker directory.')

    return Path(zfs_base_path)


def create_zfs_dataset(absolute_path):
    """
    Create a ZFS dataset inside the Jailmaker directory at the provided
    absolute path.

    Examples:
    - /mnt/mypool/jailmaker/jails
    - /mnt/mypool/jailmaker/jails/newjail
    """
    relative_path = get_relative_path_in_jailmaker_dir(absolute_path)
    dataset_to_create = get_zfs_base_path() / relative_path

    eprint(f'Creating ZFS Dataset "{dataset_to_create}"')

    # subprocess.CalledProcessError will be caught by create_jail()
    subprocess.run(['zfs', 'create', dataset_to_create], check=True)


def remove_zfs_dataset(absolute_path):
    """
    Remove a ZFS dataset inside the Jailmaker directory at the provided
    absolute path.

    Example: /mnt/mypool/jailmaker/jails/oldjail
    """
    relative_path = get_relative_path_in_jailmaker_dir(absolute_path)
    dataset_to_remove = get_zfs_base_path() / relative_path

    eprint(f'Removing ZFS Dataset "{dataset_to_remove}"')

    # subprocess.CalledProcessError will be caught by create_jail()
    subprocess.run(['zfs', 'destroy', '-r', dataset_to_remove], check=True)


def check_jail_name_valid(name):
    """
    Confirms that the jail name matches the required format.
    """
    if re.match(r'^[.a-zA-Z0-9-]{1,64}$', name) and not name.startswith('.') and '..' not in name:
        return True

    eprint(dedent(
        f"""
        {YELLOW}{BOLD}WARNING: INVALID NAME{NORMAL}

        A valid name consists of:
        - allowed characters (alphanumeric, dash, dot)
        - no leading or trailing dots
        - no sequences of multiple dots
        - max 64 characters
        """
    ))

    return False


def check_jail_name_available(name, warn=True):
    """
    Confirms if a jail name is available for use.
    """
    if not get_jail_path(name).exists():
        return True

    if warn:
        print()
        eprint(f'A jail with this name, "{name}", already exists.')

    return False


def ask_jail_name(name=''):
    while True:
        print()
        name = input_with_default('Enter jail name: ', name).strip()

        if not (check_jail_name_valid(name) and check_jail_name_available(name)):
            continue

        return name


def agree_with_default(config, key, question):
    default_answer = 'y' if config.my_getboolean(key) else 'n'
    config.my_set(key, agree(question, default_answer))


def get_text_editor():
    def get_from_environ(key):
        if editor := os.environ.get(key):
            return shutil.which(editor)

    return (get_from_environ('VISUAL')
        or get_from_environ('EDITOR')
        or shutil.which('editor')
        or shutil.which('/usr/bin/editor')
        or 'nano'
    )


def interactive_config():
    config = KeyValueParser()
    config.read_string(DEFAULT_CONFIG)

    recommended_distro = config.my_get('distro')
    recommended_release = config.my_get('release')

    """
    Config handling
    """
    jail_name = ''

    print()

    if agree('Do you wish to create a jail from a config template?', 'n'):
        print(dedent(
            """
            A text editor will open so you can provide the config template.

            1. Please copy your config
            2. Paste it into the text editor
            3. Save and close the text editor
            """
        ))

        input('Press Enter to open the text editor.')

        with tempfile.NamedTemporaryFile(mode='w+t') as f:
            subprocess.call([get_text_editor(), f.name])
            f.seek(0)

            # Start over with a new KeyValueParser to parse user config
            config = KeyValueParser()
            config.read_file(f)

        # Ask for jail name
        jail_name = ask_jail_name(jail_name)
    else:
        print()

        if not agree(f'Install the recommended image ({recommended_distro} {recommended_release})?', 'y'):
            print(dedent(
                f"""
                {YELLOW}{BOLD}WARNING: ADVANCED USAGE{NORMAL}

                You may now choose from a list which distro to install.
                But not all of them may work with {SCRIPT_NAME} since these images are made for LXC.
                Distros based on systemd probably work (e.g. Ubuntu, Arch Linux and Rocky Linux).
                """
            ))

            input('Press Enter to continue...')
            print()

            if run_lxc_download_script() != 0:
                fail('Failed to list images. Aborting...')

            print()
            print('Choose from the DIST column.')
            print()
            config.my_set('distro', input('Distro: '))

            print()
            print('Choose from the RELEASE column (or ARCH if RELEASE is empty).')
            print()
            config.my_set('release', input('Release: '))

        jail_name = ask_jail_name(jail_name)

        print()
        agree_with_default(
            config,
            'gpu_passthrough_intel',
            'Passthrough the intel GPU (if present)?'
        )

        print()
        agree_with_default(
            config,
            'gpu_passthrough_nvidia',
            'Passthrough the nvidia GPU (if present)?'
        )

        print()
        agree_with_default(
            config,
            'force_nvidia_legacy_driver',
            'Force the NVIDIA Proprietary driver for TrueNAS CE Goldeye and newer (if GPU present)?'
        )

        print(dedent(
            f"""
            {YELLOW}{BOLD}WARNING: CHECK SYNTAX{NORMAL}

            You may pass additional flags to systemd-nspawn.
            With incorrect flags the jail may not start.
            It is possible to correct/add/remove flags post-install.
            """
        ))

        if agree('Show the man page for systemd-nspawn?', 'n'):
            subprocess.run(['man', 'systemd-nspawn'])
        else:
            try:
                base_os_version = platform.freedesktop_os_release().get(
                    'VERSION_CODENAME',
                    recommended_release
                )
            except AttributeError:
                base_os_version = recommended_release

            print(dedent(
                f"""
                You may read the systemd-nspawn manual online:
                https://manpages.debian.org/{base_os_version}/systemd-container/systemd-nspawn.1.en.html
                """
            ))

        # Backslashes and colons need to be escaped in bind mount
        # options. For example, to bind mount a file called:
        #
        # weird chars :?\"
        #
        # the corresponding command would be:
        #
        # --bind-ro='/mnt/data/weird chars \:?\\"'
        print(dedent(
            """
            Would you like to add additional systemd-nspawn flags?
            For example to mount directories inside the jail you may:
            Mount the TrueNAS location /mnt/pool/dataset to the /home directory of the jail with:
            --bind='/mnt/pool/dataset:/home'
            Or the same, but readonly, with:
            --bind-ro='/mnt/pool/dataset:/home'
            Or create macvlan interface with:
            --network-macvlan=eno1 --resolv-conf=bind-host
            """
        ))

        config.my_set(
            'systemd_nspawn_user_args',
            '\n    '.join(shlex.split(input('Additional flags: ') or '')),
        )

        print(dedent(
            f"""
            The `{SCRIPT_NAME} startup` command can automatically start a selection of jails.
            This comes in handy when you want to automatically start multiple jails after booting TrueNAS CE (e.g. from a Post Init Script).
            """
        ))

        config.my_set(
            'startup',
            agree(
                f'Do you want to start this jail when running: {SCRIPT_NAME} startup?',
                'n'
            )
        )

    print()
    is_start_now = agree('Do you want to start this jail now (when create is done)?', 'y')
    print()

    return jail_name, config, is_start_now


def create_jail(**kwargs):
    print(DISCLAIMER)

    if SCRIPT_DIR_PATH.name != 'jailmaker':
        eprint(dedent(
            f"""
            {SCRIPT_NAME} needs to create files.
            Currently it can not decide if it is safe to create files in:
            {SCRIPT_DIR_PATH}
            Please create a dedicated dataset called "jailmaker", store {SCRIPT_NAME} there and try again.
            """
        ))

        return 1

    if not get_mount_point(SCRIPT_DIR_PATH).is_relative_to('/mnt'):
        print(dedent(
            f"""
            {YELLOW}{BOLD}WARNING: BEWARE OF DATA LOSS{NORMAL}

            {SCRIPT_NAME} should be on a dataset mounted under /mnt (it currently is not).
            Storing it on the boot-pool means losing all jails when updating TrueNAS.
            Jails will be stored under:
            {SCRIPT_DIR_PATH}
            """
        ))

    jail_name = kwargs.pop('jail_name', None)
    is_start_now = False

    # Non-interactive create
    if jail_name:
        if not check_jail_name_valid(jail_name):
            return 1

        if not check_jail_name_available(jail_name):
            return 1

        is_start_now = kwargs.pop('start', is_start_now)
        jail_config_path = kwargs.pop('config')

        config = KeyValueParser()

        if jail_config_path:
            # TODO: fallback to default values for e.g. distro and release if they are not in the config file
            if jail_config_path == '-':
                print(f'Creating jail {jail_name} from config template passed via stdin.')
                config.read_string(sys.stdin.read())
            else:
                print(f'Creating jail {jail_name} from config template {jail_config_path}.')

                if jail_config_path not in config.read(jail_config_path):
                    eprint(f'Failed to read config template {jail_config_path}.')
                    return 1
        else:
            print(f'Creating jail {jail_name} with default config.')
            config.read_string(DEFAULT_CONFIG)

        is_user_override = False
        options = [
            'distro',
            'gpu_passthrough_intel',
            'gpu_passthrough_nvidia',
            'force_nvidia_legacy_driver',
            'release',
            'seccomp',
            'startup',
            'systemd_nspawn_user_args',
        ]

        for option in options:
            value = kwargs.pop(option)

            if (
                value is not None
                # String, non-empty list of args or int
                and (isinstance(value, int) or len(value))
                and value is not config.my_get(option, None)
            ):
                # TODO: this will wipe all systemd_nspawn_user_args from the template...
                # Should there be an option to append them instead?
                print(f'Overriding {option} config value with {value}.')
                config.my_set(option, value)
                is_user_override = True

        if not is_user_override:
            print(dedent(
                f"""
                Hint: run `{SCRIPT_NAME} create` without any arguments for interactive config.
                Or use CLI args to override the default options.
                For more info, run: `{SCRIPT_NAME} create --help`
                """
            ))
    else:
        jail_name, config, is_start_now = interactive_config()

    jail_path = get_jail_path(jail_name)
    distro = config.my_get('distro')
    release = config.my_get('release')

    # Clean up in except, but only when the jail_path is finalized;
    # otherwise the wrong directory may be affected
    try:
        # Create the dir or dataset where to store the jails
        if not JAILS_DIR_PATH.exists():
            if get_zfs_dataset(SCRIPT_DIR_PATH):
                # Creating "jails" dataset if "jailmaker" is a ZFS
                # dataset
                create_zfs_dataset(JAILS_DIR_PATH)
            else:
                JAILS_DIR_PATH.mkdir(parents=True, exist_ok=True)

            JAILS_DIR_PATH.chmod(0o700)

        # Creating a dataset for the jail if the jails dir is a dataset
        if get_zfs_dataset(JAILS_DIR_PATH):
            create_zfs_dataset(jail_path)

        jail_config_path = get_jail_config_path(jail_name)
        jail_rootfs_path = get_jail_rootfs_path(jail_name)

        # Create directory for rootfs
        jail_rootfs_path.mkdir(parents=True, exist_ok=True)

        # LXC download script needs to write to this file during
        # installation; however, it will be removed later because it
        # isn't needed
        jail_config_path.open('a').close()

        code = run_lxc_download_script(jail_name, jail_path, jail_rootfs_path, distro, release)

        if code != 0:
            cleanup(jail_path)
            return code

        # Assuming the name of the jail is "myjail" and the command
        # "machinectl shell myjail" fails, try the following:
        #
        # Stop the jail with:
        # $ machinectl stop myjail
        #
        # And start a shell inside the jail without the --boot option:
        # $ systemd-nspawn -q -D jails/myjail/rootfs /bin/sh
        #
        # Then set a root password with:
        # $ passwd
        # $ exit
        #
        # In case of amazonlinux the following may need to be run first:
        # $ yum update -y && yum install -y passwd
        #
        # Then log in from the host via:
        # $ machinectl login myjail
        #
        # SSH should also be enabled inside the jail to login, but  if
        # that doesn't work (e.g. for alpine) get a shell via:
        # $ nsenter -t $(machinectl show myjail -p Leader --value) -a /bin/sh -l
        #
        # But alpine jails made with Jailmaker have other issues, such
        # as not shutting down cleanly via systemctl and machinectl.

        # Use chroot to correctly resolve absolute /sbin/init symlink
        with Chroot(jail_rootfs_path):
            system_name = Path('/sbin/init').resolve().name

        if system_name != 'systemd' and parse_os_release(jail_rootfs_path).get('ID') != 'nixos':
            print(dedent(
                f"""
                {YELLOW}{BOLD}WARNING: DISTRO NOT SUPPORTED{NORMAL}

                Chosen distro appears not to use systemd...

                You probably will not get a shell with:
                machinectl shell {jail_name}

                You may get a shell with this command:
                nsenter -t $(machinectl show {jail_name} -p Leader --value) -a /bin/sh -l

                Read about the downsides of nsenter:
                https://github.com/systemd/systemd/issues/12785#issuecomment-503019081

                {BOLD}Using this distro with {SCRIPT_NAME} is NOT recommended.{NORMAL}
                """
            ))

            print('Autostart has been disabled.')
            print('You need to start this jail manually.')
            config.my_set('startup', 0)
            is_start_now = False

        # Remove config files created by systemd
        (jail_rootfs_path / 'etc/machine-id').unlink(missing_ok=True)
        (jail_rootfs_path / 'etc/resolv.conf').unlink(missing_ok=True)

        # Prevent root login failures when logging in with Secure TTY
        #
        # <https://github.com/systemd/systemd/issues/852>
        terminal_devices = '\n'.join([f'pts/{i}' for i in range(0, 11)])
        print(terminal_devices, file=(jail_rootfs_path / 'etc/securetty').open('w'))

        network_dir_path = jail_rootfs_path / 'etc/systemd/network'

        # Modify default network settings, if network_dir_path exists
        if network_dir_path.is_dir():
            host0_network_file = jail_rootfs_path / 'lib/systemd/network/80-container-host0.network'

            # Check if default host0 network file exists
            if host0_network_file.is_file():
                # Override the default 80-container-host0.network file
                # (by using the same name). This config applies when
                # using the --network-bridge option of systemd-nspawn
                # Disable LinkLocalAddressing on IPv4, or else the
                # container won't get IP address via DHCP, but keep it
                # enabled on IPv6, as SLAAC and DHCPv6 both require a
                # local-link address to function
                host0 = host0_network_file.read_text()
                host0 = host0.replace('LinkLocalAddressing=yes', 'LinkLocalAddressing=ipv6')

                print(host0, file=(network_dir_path / '80-container-host0.network').open('w'))

            # Setup DHCP for MACVLAN network interfaces. This config
            # applies when using the --network-macvlan option of
            # systemd-nspawn.
            #
            # <https://www.debian.org/doc/manuals/debian-reference/ch05.en.html#_the_modern_network_configuration_without_gui>
            macvlan_settings = cleandoc(
                """
                [Match]
                Virtualization=container
                Name=mv-*

                [Network]
                DHCP=yes
                LinkLocalAddressing=ipv6

                [DHCPv4]
                UseDNS=true
                UseTimezone=true
                """
            )

            print(macvlan_settings, file=(network_dir_path / 'mv-dhcp.network').open('w'))

            # Setup DHCP for veth-extra network interfaces. This config
            # applies when using the --network-veth-extra option of
            # systemd-nspawn.
            #
            # <https://www.debian.org/doc/manuals/debian-reference/ch05.en.html#_the_modern_network_configuration_without_gui>
            veth_extra_settings = cleandoc(
                """
                [Match]
                Virtualization=container
                Name=vee-*

                [Network]
                DHCP=yes
                LinkLocalAddressing=ipv6

                [DHCPv4]
                UseDNS=true
                UseTimezone=true
                """
            )

            print(veth_extra_settings, file=(network_dir_path / 'vee-dhcp.network').open('w'))

            # Override preset which caused systemd-networkd to be
            # disabled (e.g. Fedora 39)
            #
            # <https://www.freedesktop.org/software/systemd/man/latest/systemd.preset.html>
            # <https://github.com/lxc/lxc-ci/blob/f632823ecd9b258ed42df40449ec54ed7ef8e77d/images/fedora.yaml#L312C5-L312C38>
            preset_path = jail_rootfs_path / 'etc/systemd/system-preset'
            preset_path.mkdir(parents=True, exist_ok=True)

            preset_file = preset_path / '00-jailmaker.preset'

            print('enable systemd-networkd.service', file=preset_file.open('w'))

        with jail_config_path.open('w') as fp:
            config.write(fp)

        jail_config_path.chmod(0o600)
    except BaseException as error:
        # Clean up on any exception and rethrow
        cleanup(jail_path)
        raise error

    return start_jail(jail_name) if is_start_now else 0


def jail_is_running(jail_name):
    try:
        subprocess.run(['machinectl', 'show', jail_name], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        return False

    return True


def edit_jail(jail_name):
    """
    Edit jail with given name.
    """
    if not check_jail_name_valid(jail_name):
        return 1

    if check_jail_name_available(jail_name, False):
        eprint(f'A jail with name {jail_name} does not exist.')
        return 1

    jail_config_path = get_jail_config_path(jail_name)

    try:
        subprocess.run([get_text_editor(), jail_config_path], check=True)
    except subprocess.CalledProcessError as e:
        eprint(f'An error occurred while editing {jail_config_path}.')
        return e.returncode

    if jail_is_running(jail_name):
        print()
        print('Restart the jail for edits to apply (if you made any).')

    return 0


def stop_jail(jail_name):
    """
    Stop jail with given name and wait until stopped.
    """
    if not jail_is_running(jail_name):
        return 0

    try:
        subprocess.run(['machinectl', 'poweroff', jail_name], check=True)
    except subprocess.CalledProcessError as e:
        eprint('Error while stopping jail.')
        return e.returncode

    print(f'Wait for {jail_name} to stop', end='', flush=True)

    while jail_is_running(jail_name):
        time.sleep(1)
        print('.', end='', flush=True)

    print()
    print()

    return 0


def remove_jail(jail_name):
    """
    Remove jail with given name.
    """
    if not check_jail_name_valid(jail_name):
        return 1

    if check_jail_name_available(jail_name, False):
        eprint(f'A jail with name {jail_name} does not exist.')
        return 1

    # TODO: print which dataset is about to be removed before the user confirmation
    # TODO: print that all zfs snapshots will be removed if jail has it's own zfs dataset
    check = input(f'\nCAUTION: Type "{jail_name}" to confirm jail deletion!\n\n')

    if check != jail_name:
        eprint('Wrong name, nothing happened.')
        return 1

    print()
    jail_path = get_jail_path(jail_name)
    return_code = stop_jail(jail_name)

    if return_code != 0:
        return return_code

    print()
    cleanup(jail_path)
    return 0


def print_table(header, list_of_objects, empty_value_indicator):
    # Find max width for each column
    widths = defaultdict(int)

    for obj in list_of_objects:
        for hdr in header:
            value = obj.get(hdr)

            if value is None:
                obj[hdr] = value = empty_value_indicator

            widths[hdr] = max(widths[hdr], len(str(value)), len(str(hdr)))

    # Print header
    print(UNDERLINE + ' '.join(hdr.upper().ljust(widths[hdr]) for hdr in header) + NORMAL)

    # Print rows
    for obj in list_of_objects:
        print(' '.join(str(obj.get(hdr)).ljust(widths[hdr]) for hdr in header))


def get_all_jail_names():
    try:
        jail_names = [jail_dir.name for jail_dir in JAILS_DIR_PATH.iterdir()]
    except FileNotFoundError:
        jail_names = []

    return jail_names


def parse_os_release(new_root):
    result = {}

    with Chroot(new_root):
        # Use chroot to correctly resolve os-release symlink (for nixos)
        for candidate in ['/etc/os-release', '/usr/lib/os-release']:
            try:
                with Path(candidate).open(encoding='utf-8') as f:
                    # TODO: Is there a solution which doesn't depend on the internal _parse_os_release method?
                    result = platform._parse_os_release(f)
                    break
            except OSError:
                # Silently ignore failing to read os release info
                pass

    return result


def list_jails():
    """
    List all available and running jails.
    """
    jails = {}
    empty_value_indicator = '-'

    jail_names = get_all_jail_names()

    if not jail_names:
        print('No jails.')
        return 0

    # Get running jails from machinectl
    running_machines = []

    try:
        machinectl = ['machinectl', 'list', '-o', 'json']
        json_data = subprocess.run(machinectl, capture_output=True, text=True)
        running_machines = json.loads(json_data.stdout.strip())
    except subprocess.CalledProcessError as e:
        eprint(f'Failed to get list of jails: {e}')
    except json.JSONDecodeError as e:
        eprint(f'Error parsing JSON: {e}')

    # Index the machines that are running by their name because only
    # systemd-nspawn machines should be stored
    running_machines = {
        item['machine']: item for item in running_machines if item['service'] == 'systemd-nspawn'
    }

    for jail_name in jail_names:
        jail_rootfs_path = get_jail_rootfs_path(jail_name)
        jails[jail_name] = {'name': jail_name, 'running': False}
        jail = jails[jail_name]

        config = parse_config_file(get_jail_config_path(jail_name))
        if config:
            jail['startup'] = config.my_getboolean('startup')
            jail['gpu_intel'] = config.my_getboolean('gpu_passthrough_intel')
            jail['gpu_nvidia'] = config.my_getboolean('gpu_passthrough_nvidia')

        if jail_name in running_machines:
            machine = running_machines[jail_name]

            # Augment the jails dict with output from machinectl
            jail['running'] = True
            jail['os'] = machine['os'] or None
            jail['version'] = machine['version'] or None

            addresses = machine.get('addresses')

            if not addresses:
                jail['addresses'] = empty_value_indicator
            else:
                addresses = addresses.split('\n')
                jail['addresses'] = addresses[0]

                if len(addresses) > 1:
                    jail['addresses'] += '…'
        else:
            # Parse os-release info ourselves
            jail_platform = parse_os_release(jail_rootfs_path)

            jail['os'] = jail_platform.get('ID')
            jail['version'] = jail_platform.get('VERSION_ID') or jail_platform.get('VERSION_CODENAME')

    print_table(
        [
            'name',
            'running',
            'startup',
            'gpu_intel',
            'gpu_nvidia',
            'os',
            'version',
            'addresses',
        ],
        sorted(jails.values(), key=lambda x: x['name']),
        empty_value_indicator,
    )

    return 0


def startup_jails():
    is_fail = False

    for jail_name in get_all_jail_names():
        config = parse_config_file(get_jail_config_path(jail_name))

        if config and config.my_getboolean('startup') and start_jail(jail_name) != 0:
            is_fail = True

    return 1 if is_fail else 0


def split_at_string(lst, string):
    try:
        index = lst.index(string)
        return lst[:index], lst[index + 1 :]
    except ValueError:
        return lst, []


def init_parser():
    parser = argparse.ArgumentParser(description=__doc__, epilog=DISCLAIMER, allow_abbrev=False)
    parser.add_argument('--version', action='version', version=__version__)

    return parser


def add_parser(subparser, **kwargs):
    # Always add help except for when explicitly disabled
    is_add_help = kwargs.get('add_help', True)

    # Commands having additional arguments after the jail name
    is_split_args = kwargs.pop('split_args', False)

    # Additional positional arguments
    options_or_positional_args = kwargs.pop('options_or_positional_args', [])

    # Never add help with the built-in add_help because it'll be added
    # below as needed
    kwargs['add_help'] = False

    kwargs['epilog'] = DISCLAIMER
    kwargs['exit_on_error'] = False

    func = kwargs.pop('func')
    parser = subparser.add_parser(**kwargs)
    parser.set_defaults(func=func)

    parser.set_defaults(is_split_args=is_split_args)

    if is_add_help:
        parser.add_argument(
            '-h',
            '--help',
            help='show this help message and exit',
            action='store_true'
        )

    for params in options_or_positional_args:
        name_or_flags = params.pop('name_or_flags')

        if isinstance(name_or_flags, list):
            parser.add_argument(*name_or_flags, **params)
            continue

        parser.add_argument(name_or_flags, **params)

    # Setting the add_help after the parser has been created with
    # add_parser() has no effect, but it allows a lookup if this parser
    # has a help message available
    parser.add_help = is_add_help

    return parser


def init_commands(parser):
    subparsers = parser.add_subparsers(
        title='commands',
        dest='command',
        metavar='',
        parser_class=CustomSubParser
    )

    jail_name_help = {
        'name_or_flags': 'jail_name',
        'help': 'name of the jail'
    }

    command_definitions = [
        {
            'name': 'create',
            'help': 'create a new jail',
            'func': create_jail,
            'split_args': True,
            'options_or_positional_args': [
                jail_name_help | {'nargs': '?'},
                {'name_or_flags': '--distro'},
                {'name_or_flags': '--release'},
                {
                    'name_or_flags': '--start',
                    'help': 'start jail after create',
                    'action': 'store_true',
                },
                {
                    'name_or_flags': '--startup',
                    'type': int,
                    'choices': [0, 1],
                    'help': f'start this jail when running: {SCRIPT_NAME} startup',
                },
                {
                    'name_or_flags': '--seccomp',
                    'type': int,
                    'choices': [0, 1],
                    'help': 'turning off seccomp filtering improves performance at the expense of security',
                },
                {
                    'name_or_flags': ['-c', '--config'],
                    'help': 'path to config file template or - for stdin',
                },
                {
                    'name_or_flags': ['-gi', '--gpu_passthrough_intel'],
                    'type': int,
                    'choices': [0, 1],
                },
                {
                    'name_or_flags': ['-gn', '--gpu_passthrough_nvidia'],
                    'type': int,
                    'choices': [0, 1],
                },
                {
                    'name_or_flags': ['-fl', '--force_nvidia_legacy_driver'],
                    'type': int,
                    'choices': [0, 1],
                },
                {
                    'name_or_flags': 'systemd_nspawn_user_args',
                    'nargs': '*',
                    'help': 'add additional systemd-nspawn flags',
                },
            ],
        },
        {
            'name': 'edit',
            'help': f'edit jail config with {get_text_editor()} text editor',
            'func': edit_jail,
            'options_or_positional_args': [jail_name_help.copy()],
        },
        {
            'name': 'exec',
            'help': 'execute a command in the jail',
            'func': exec_jail,
            'split_args': True,
            'options_or_positional_args': [
                jail_name_help.copy(),
                {
                    'name_or_flags': 'cmd',
                    'nargs': '*',
                    'help': 'command to execute',
                },
            ],
        },
        {
            'name': 'images',
            'help': 'list available images to create jails from',
            'func': run_lxc_download_script,
        },
        {
            'name': 'list',
            'help': 'list jails',
            'func': list_jails,
        },
        {
            'name': 'log',
            'help': 'show jail log',
            'func': log_jail,
            'split_args': True,
            'options_or_positional_args': [
                jail_name_help.copy(),
                {
                    'name_or_flags': 'args',
                    'nargs': '*',
                    'help': 'args to pass to journalctl',
                },
            ],
        },
        {
            'name': 'remove',
            'help': 'remove previously created jail',
            'func': remove_jail,
            'options_or_positional_args': [jail_name_help.copy()],
        },
        {
            'name': 'restart',
            'help': 'restart a running jail',
            'func': restart_jail,
            'options_or_positional_args': [jail_name_help.copy()],
        },
        {
            'name': 'shell',
            'help': 'open shell in running jail (alias for machinectl shell)',
            'func': shell_jail,
            'add_help': False,
            'options_or_positional_args': [
                {
                    'name_or_flags': 'args',
                    'nargs': '*',
                    'help': 'args to pass to machinectl shell',
                },
            ],
        },
        {
            'name': 'start',
            'help': 'start previously created jail',
            'func': start_jail,
            'options_or_positional_args': [jail_name_help.copy()],
        },
        {
            'name': 'startup',
            'help': 'startup selected jails',
            'func': startup_jails,
        },
        {
            'name': 'status',
            'help': 'show jail status',
            'func': status_jail,
            'split_args': True,
            'options_or_positional_args': [
                jail_name_help.copy(),
                {
                    'name_or_flags': 'args',
                    'nargs': '*',
                    'help': 'args to pass to systemctl',
                },
            ],
        },
        {
            'name': 'stop',
            'help': 'stop a running jail',
            'func': stop_jail,
            'options_or_positional_args': [jail_name_help.copy()],
        },
    ]

    commands = {}

    for definition in command_definitions:
        command = definition.get('name')
        commands[command] = add_parser(subparsers, **definition)

    return commands


def show_help_when_needed(parser, commands):
    # Ignore all args after the first '--'
    args_to_parse = split_at_string(sys.argv[1:], '--')[0]

    # Check for help
    if not any(item in args_to_parse for item in ['-h', '--help']):
        return

    # More than Likely the help output needs to be shown...
    try:
        args = vars(parser.parse_known_args(args_to_parse)[0])

        # Exit if a subparser wasn't invoked: jlmkr.py --help
        if args.get('help'):
            is_print_help = True
            command = args.get('command')
            jail_name = args.get('jail_name')
            is_split_args = args.get('is_split_args', False)

            # Edge case for some commands
            if is_split_args and jail_name:
                # Ignore all args after the jail name
                args_to_parse = split_at_string(args_to_parse, jail_name)[0]

                # Add back the jail_name as it may be a required positional to
                # avoid the except clause below
                args_to_parse += [jail_name]

                # Parse one more time...
                args = vars(parser.parse_known_args(args_to_parse)[0])

                # then check if help is still in the remaining args
                is_print_help = args.get('help')

            if is_print_help:
                commands[command].print_help()
                sys.exit()
    except ExceptionWithParser as e:
        # Print help output on error, e.g. due to:
        # "error: the following arguments are required"
        if e.parser.add_help:
            e.parser.print_help()
            sys.exit()


def run_command(parser):
    # Parse to find command and function and ignore unknown args, which may be
    # present, such as args intended to pass through to systemd-run
    args = vars(parser.parse_known_args()[0])
    command = args.pop('command', None)
    jail_name = args.get('jail_name')
    is_split_args = args.get('is_split_args', False)

    # Start over with original args
    args_to_parse = sys.argv[1:]

    if not command:
        # Parse args and show error for unknown args
        parser.parse_args(args_to_parse)

        if agree('Create a new jail?', 'y'):
            print()
            sys.exit(create_jail())

        parser.print_help()
        sys.exit()

    if command == 'shell':
        # Pass anything after the "shell" command to machinectl
        _, shell_args = split_at_string(args_to_parse, command)
        sys.exit(args['func'](shell_args))

    if is_split_args and jail_name:
        jlmkr_args, remaining_args = split_at_string(args_to_parse, jail_name)

        if remaining_args and remaining_args[0] != '--':
            # Add '--' after the jail name to ensure further args
            #
            # Example:
            # --help or --version, are captured as systemd_nspawn_user_args
            args_to_parse = jlmkr_args + [jail_name, '--'] + remaining_args

    # Parse args again, but show error for unknown args
    args = vars(parser.parse_args(args_to_parse))

    # Clean the args
    args.pop('help')
    args.pop('command', None)
    args.pop('is_split_args', None)
    func = args.pop('func')

    sys.exit(func(**args))


def main():
    if SCRIPT_PATH.stat().st_uid != 0:
        fail(f'This script should be owned by the root user... Fix it manually with: `chown root {SCRIPT_PATH}`.')

    parser = init_parser()
    commands = init_commands(parser)

    if os.getuid() != 0:
        parser.print_help()
        fail('Run this script as root...')

    # Set appropriate permissions (if not already set) for this file,
    # since it's executed as root
    SCRIPT_PATH.chmod(0o760)

    show_help_when_needed(parser, commands)

    # Exit on parse errors (e.g. missing positional args)
    for command in commands:
        commands[command].exit_on_error = True

    run_command(parser)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
