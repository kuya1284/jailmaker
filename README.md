# Jailmaker

Persistent Linux **"jails"** on TrueNAS CE (formerly TrueNAS Scale) to install software ([K3s](https://k3s.io/), 
[Docker](https://www.docker.com/), [Portainer](https://www.portainer.io/), [Podman](https://podman.io/), etc.) with full 
access to all files via bind mounts.

## Project Maintenance

### July 10, 2026

[Jip-Hop previously announced](https://github.com/Jip-Hop/jailmaker/discussions/241) that his repo was no longer going 
to be maintained with the final release being **v2.1.1**. It was last tested to work with TrueNAS SCALE **v24.10.0**. 
However, with the release of TrueNAS CE **v25.10** (Goldeye), support for 
[older NVIDIA GPUs broke](https://forums.truenas.com/t/nvidia-compatible-driver-test-for-truenas-25-10-goldeye/53395) 
due to iXsystems [migrating](https://forums.truenas.com/t/nvidia-kernel-module-change-in-truenas-25-10-what-this-means-for-you/51070) 
from the NVIDIA Proprietary driver to the NVIDIA Open Kernel driver. The reason for this migration was to support NVIDIA 
Blackwell (RTX 50 Series) GPUs and newer. A kind patron who goes by [zzzhouuu](https://github.com/zzzhouuu) created a 
[TrueNAS NVIDIA Driver Build repo](https://github.com/zzzhouuu/truenas-nvidia-drivers) to help maintain support for 
legacy NVIDIA GPUs. Additional information and instructions on zzzhouuu's site, 
[TrueNAS Scale GPU Drivers](https://truenas-drivers.zhouyou.info/index.html).

In addition to legacy NVIDIA GPUs breaking, the NVIDIA Open Kernel driver also broke the bind mounts of the NVIDIA 
libraries for newer NVIDIA GPUs as well. This is because the path in which the libraries are stored have changed; they 
are now located directly in the base **System Extensions** directory, `/usr/share/truenas/sysext-extensions`.

This fork of [Jip-Hop's original Jailmaker repo](https://github.com/Jip-Hop/jailmaker) was created to help continue his 
efforts to help restore support for NVIDIA GPUs:

* Support for newer NVIDIA GPUs using the **Open Kernel driver**
* Support for legacy NVIDIA GPUs using the **Proprietary driver**

Why continue the effort?

* iXsystems [announced](https://www.youtube.com/watch?v=ixYNPAv9olY) that they have decided to drop [Incus](https://linuxcontainers.org/incus/) support in **TrueNAS 26**, and will be reverting back to [libvirt](https://libvirt.org/).
* Running Docker apps in a self-contained environment could make things easier with managing containerized apps, especially if you need to install OS packages, such as **cron** or **logrotate**, that can't be installed directly on the TrueNAS host.

There won't be a lot of effort put into Jip-Hop's brilliant Jailmaker script; however, minimal effort will be given 
as time permits to help address issues that may arise whenever something stops functioning after upgrading to a new 
version of TrueNAS CE.


## Video Tutorial

[![TrueNAS Scale - Setting up Sandboxes with Jailmaker - YouTube Video](https://img.youtube.com/vi/S0nTRvAHAP8/0.jpg)
<br>
Watch on YouTube](https://www.youtube.com/watch?v=S0nTRvAHAP8 "TrueNAS Scale - Setting up Sandboxes with Jailmaker - YouTube Video")

## Disclaimer

**USING THIS SCRIPT IS AT YOUR OWN RISK! IT COMES WITHOUT WARRANTY AND IS NOT SUPPORTED BY IXSYSTEMS.**

## Summary

TrueNAS CE can create persistent Linux 'jails' with `systemd-nspawn`. This script helps with the following:

* Setting up the jail so it won't be lost when you upgrade TrueNAS CE
* Choosing a distro (Debian 12 strongly recommended, but Ubuntu, Arch Linux or Rocky Linux seem good choices too)
* Will create a ZFS dataset for each jail if the `jailmaker` directory is a dataset (easy snapshotting)
* Optional: configuring the jail so you can run Docker inside it
* Optional: GPU passthrough (Intel, NVIDIA with drivers bind mounted from the host, [AMD reportedly works too](https://github.com/Jip-Hop/jailmaker/issues/109#issuecomment-2216306765))
* Starting the jail with your config applied

## Installation

Beginning with 24.04 (Dragonfish), TrueNAS CE (previously TrueNAS Scale) officially includes the `systemd-nspawn` 
containerization program in the base system. Technically there's nothing to install. You only need the `jlmkr.py` script 
file in the right place. [Instructions with screenshots](https://www.truenas.com/docs/scale/24.04/scaletutorials/apps/sandboxes/) 
are provided on the TrueNAS website. Start by creating a new dataset called `jailmaker` with the default settings (from 
TrueNAS web interface). Then login as the `root` user and download `jlmkr.py`.

```shell
cd /mnt/mypool/jailmaker
curl --location --remote-name https://raw.githubusercontent.com/Jip-Hop/jailmaker/main/jlmkr.py
chmod +x jlmkr.py
```

The `jlmkr.py` script (and the jails + config it creates) are now stored on the `jailmaker` dataset and will survive 
updates of TrueNAS CE. If the automatically created `jails` directory is also a ZFS dataset (which is true for new 
users), then the `jlmkr.py` script will automatically create a new dataset for every jail created. This allows you to 
create a snapshot for each individual jails For legacy users (where the `jails` directory is not a dataset) each jail 
will be stored in a plain directory.

### Alias

Optionally you may create a shell alias for the currently logged in (admin) user to conveniently run `jlmkr.py` without 
having to change into the `jailmaker` directory or specify the full absolute path. I suggest to create the `jlmkr` alias 
like this:

```shell
echo "alias jlmkr=\"sudo -E '/mnt/mypool/jailmaker/jlmkr.py'\"" >> ~/.bashrc
```

Please replace `/mnt/mypool/jailmaker/` with the actual path to where you stored `jlmkr.py`. If you're using zsh instead 
of bash, then you should replace `.bashrc` in the command above with `.zshrc`. If you've created the alias, you may use 
it instead of `./jlmkr.py`.

The alias will be available the next time you load the shell, but to use the alias immediately you can 
`source ~/.bashrc` or `source ~/.zshrc`, as appropriate.

## Usage

### Create Jail

Creating a jail with the default settings is as simple as:

```shell
./jlmkr.py create --start myjail
```

By default, the hostname in the jail will default to the jail name; however, you may set a custom internal hostname.

```shell
./jlmkr.py create --start --hostname myhostname myjail
```

You may also specify a path to a config template, for a quick and consistent jail creation process.

```shell
./jlmkr.py create --start --config /path/to/config/template myjail
```

Or you can override the default config by using flags.

```shell
./jlmkr.py create --start --distro ubuntu --release jammy myjail --bind-ro='/mnt'
```

Or you can override the options in the config template using flags as well. See `./jlmkr.py create --help` for the 
available options. Anything passed after the jail name will be passed to `systemd-nspawn` when starting the jail. See 
the `systemd-nspawn` manual for available options, specifically 
[Mount Options](https://manpages.debian.org/bookworm/systemd-container/systemd-nspawn.1.en.html#Mount_Options) and 
[Networking Options](https://manpages.debian.org/bookworm/systemd-container/systemd-nspawn.1.en.html#Networking_Options) 
are frequently used.

```shell
./jlmkr.py create --start --config /path/to/config/template --distro ubuntu --release jammy myjail --bind-ro='/mnt'
```

If you omit the jail name, the interactive session will be used to configure the jail. You'll be presented with 
questions which will guide you through the process.

```shell
./jlmkr.py create
```

After answering the questions, a new jail will be created and will start automatically if you answered "Yes" to the 
question about starting the jail immediately.

#### Overriding Options
The priority of the options are handled in the following order when creating a jail, 
1. Interactive inputs (HIGHEST, but only when jail name is omitted)
2. CLI flags
3. Config template
4. Default config (LOWEST)

### Startup Jails on Boot

```shell
# Call startup using the absolute path to jlmkr.py
/mnt/mypool/jailmaker/jlmkr.py startup
```

In order to start jails automatically after TrueNAS boots, run `/mnt/mypool/jailmaker/jlmkr.py startup` as **Post Init 
Script** with Type `Command` from the TrueNAS web interface. This will start all the jails with `startup=1` in the 
config file.

If you need to support for **NVIDIA GPU Passthrough** on **TrueNAS CE Goldeye or newer**, use this command instead to 
automatically start the **NVIDIA Persistence Mode**:

`nvidia-persistenced && /mnt/mypool/jailmaker/jlmkr.py startup`

This will help avoid repetitively initializing the GPU whenever it's needed. This is useful for apps like 
[Beszel](https://beszel.dev/). If you have no plans to run apps that need to constantly query the GPU, but only plan to 
use the GPU on demand for transcoding only, such as for [Jellyfin](https://jellyfin.org/), [Plex](https://watch.plex.tv/), 
[Immich](https://immich.app/), etc., then you can omit the `nvidia-persistenced` command from the **Post Init Script**.

### Start Jail

```shell
./jlmkr.py start myjail
```

### List Jails

See list of jails (including running, startup state, GPU passthrough, distro, and IP).

```shell
./jlmkr.py list
```

### Execute Command in Jail

You may want to execute a command inside a jail, for example manually from the TrueNAS shell, a shell script or a CRON 
job. The example below executes the `env` command inside the jail.

```shell
./jlmkr.py exec myjail env
```

This example executes bash inside the jail with a command as additional argument.

```shell
./jlmkr.py exec myjail bash -c 'echo test; echo $RANDOM;'
```

### Edit Jail Config

```shell
./jlmkr.py edit myjail
```

Once you've created a jail, it will exist in a directory inside the `jails` dir next to `jlmkr.py`. For example 
`/mnt/mypool/jailmaker/jails/myjail` if you've named your jail `myjail`. You may edit the jail configuration file using 
the `./jlmkr.py edit myjail` command. This opens the config file in your favorite editor, as determined by following 
[Debian's guidelines](https://www.debian.org/doc/debian-policy/ch-customized-programs.html#editors-and-pagers) on the 
matter. You'll have to stop the jail and start it again with `jlmkr` for these changes to take effect.

### Remove Jail

Delete a jail and remove its files (requires confirmation).

```shell
./jlmkr.py remove myjail
```

### Stop Jail

```shell
./jlmkr.py stop myjail
```

### Restart Jail

```shell
./jlmkr.py restart myjail
```

### Jail Shell

Switch into the jail's shell.

```shell
./jlmkr.py shell myjail
```

### Jail Status

```shell
./jlmkr.py status myjail
```

### Jail Logs

View a jail's logs.

```shell
./jlmkr.py log myjail
```

### NVIDIA Proprietary Driver Install/Unistall

TrueNAS 25.10 (Goldeye) and newer replaced the NVIDIA Proprietary driver with Open Kernel driver. You may replace the 
Open Kernel driver with the Proprietary driver (thanks to **zzzhouuu's** [manually compiled GPU driver extensions](https://github.com/zzzhouuu/truenas-nvidia-drivers)) 
using the following command, which restore functionality to legacy NVIDIA GPUs, such as Pascal, Maxwell, Volta, etc.

```shell
./jlmkr.py nvidia --action install
```

Execute the following command to restore the Open Kernel driver.

```shell
./jlmkr.py nvidia --action uninstall
```

**NOTE:** The commands above will have no effect on TrueNAS version older than 25.10. When `gpu_passthrough_nvidia` is 
set, Jailmaker will automatically install the NVIDIA Proprietary driver for systems having a legacy NVIDIA GPU. 
Jailmaker will base this on the **compute capability** of the NVIDIA GPU; if the value must be lower than **7.5**. The 
NVIDIA Proprietary driver will only work for GPUs having an architecture older than Blackwell (i.e. Turing, Ada 
Lovelace, etc.). For systems running a Blackwell-based GPUs or newer, the NVIDIA Open Kernel driver must be used.

### Additional Commands

Expert users may use the following additional commands to manage jails directly: `machinectl`, `systemd-nspawn`, 
`systemd-run`, `systemctl` and `journalctl`. The `jlmkr` script uses these commands under the hood and implements a 
subset of their functions. If you use them directly you will bypass any safety checks or configuration done by `jlmkr` 
and not everything will work in the context of TrueNAS CE.

## Security

By default, the `root` user in the jail with uid 0 is mapped to the host's uid 0. This has 
[obvious security implications](https://linuxcontainers.org/lxc/security/#privileged-containers). If this is not 
acceptable to you, you may lock down the jails by [limiting capabilities](https://manpages.debian.org/bookworm/systemd-container/systemd-nspawn.1.en.html#Security_Options) 
and/or using [user namespacing](https://manpages.debian.org/bookworm/systemd-container/systemd-nspawn.1.en.html#User_Namespacing_Options) 
or use a VM instead.

### Secure computing mode (seccomp)
[Secure computing mode (seccomp)](https://docs.docker.com/engine/security/seccomp/) is a Linux kernel feature that 
restricts programs from making unauthorized system calls. This means that when `seccomp` is enabled there can be times 
when a process that runs inside a jail will be killed with the error **"Operation not permitted."**  In order to find 
out which syscall needs to be added to the `--system-call-filter=` configuration you can use `strace`.  

For example:
```
# /usr/bin/intel_gpu_top
Failed to initialize PMU! (Operation not permitted)

# strace /usr/bin/intel_gpu_top 2>&1 |grep Operation\ not\ permitted
perf_event_open({type=0x10 /* PERF_TYPE_??? */, size=PERF_ATTR_SIZE_VER7, config=0x100002, sample_period=0, sample_type=0, read_format=PERF_FORMAT_TOTAL_TIME_ENABLED|PERF_FORMAT_GROUP, precise_ip=0 /* arbitrary skid */, use_clockid=1, ...}, -1, 0, -1, 0) = -1 EPERM (Operation not permitted)
write(2, "Failed to initialize PMU! (Opera"..., 52Failed to initialize PMU! (Operation not permitted)
```
The syscall that needs to be added to the `--system-call-filter` option in the `jailmaker` config in this case would be 
`perf_event_open`. You may need to run strace multiple times.

The `seccomp` feature is important for security, but as a last resort can be disabled by setting `seccomp=0` in the jail 
config.

## Networking

By default, a jail will use the same networking namespace, with access to all (physical) interfaces the TrueNAS host has 
access to. No further setup is required. You may download and install additional packages inside the jail. Note that 
some ports are already occupied by TrueNAS CE (e.g. `443` for the web interface), so your jail can't listen on these 
ports.

Depending on the service this may be OK. For example [Home Assistant](https://www.home-assistant.io/) will bind to port 
`8123`, leaving the `80` and `443` ports free from clashes for the TrueNAS web interface. You can then either connect to 
the service on `8123`, or use a reverse proxy such as [Traefik](https://traefik.io/traefik).

But clashes may happen if you want some services (e.g. [Traefik](https://traefik.io/traefik)) inside the jail to listen 
on port `443`. To work around this issue when using host networking, you may disable DHCP and add several static IP 
addresses (aliases) through the TrueNAS web interface. If you set up the TrueNAS web interface to only listen on one of 
these IP addresses, the ports on the remaining IP addresses remain available for the jail to listen on.

See [the networking docs](./docs/network.md) for more advanced options (bridge and macvlan networking).

## Docker

Using the [docker config template](./templates/docker/README.md) is recommended if you want to run Docker inside the 
jail. You may of course manually install Docker inside a jail. But keep in mind that you need to add 
`--system-call-filter='add_key keyctl bpf'` (or disable seccomp filtering). It is 
[not recommended to use host networking for a jail in which you run Docker](https://github.com/Jip-Hop/jailmaker/issues/119). 
Docker needs to manage iptables rules, which it can safely do in its own networking namespace (when using 
[bridge or macvlan networking](./docs/network.md) for the jail).

## Documentation

Additional documentation can be found in [the docs directory](./docs/) (contributions are welcome!).

## Comparison

TODO: write comparison between `systemd-nspawn` (without `jailmaker`), LXC, VMs, Docker (on the host).

## Incompatible Distros

The rootfs image `jlmkr.py` downloads comes from the [Linux Containers Image](https://images.linuxcontainers.org) 
server. These images are made for [LXC](https://linuxcontainers.org/lxc/introduction/). We can use them with 
`systemd-nspawn` too, although not all of them work properly. For example, the `alpine` image doesn't work well. If you 
stick with common systemd based distros (Debian, Ubuntu, Arch Linux, etc.) you should be fine.

## Filing Issues and Community Support

When in need of help or when you think you've found a bug in `jailmaker`, 
[please start with reading this](https://github.com/Jip-Hop/jailmaker/discussions/135).

## References

* [TrueNAS Forum Thread about Jailmaker](https://forums.truenas.com/t/linux-jails-sandboxes-containers-with-jailmaker/417)
* [systemd-nspawn](https://manpages.debian.org/bookworm/systemd-container/systemd-nspawn.1.en.html)
* [machinectl](https://manpages.debian.org/bookworm/systemd-container/machinectl.1.en.html)
* [systemd-run](https://manpages.debian.org/bookworm/systemd/systemd-run.1.en.html)
* [Run docker in systemd-nspawn](https://wiki.archlinux.org/title/systemd-nspawn#Run_docker_in_systemd-nspawn)
* [The original Jailmaker gist](https://gist.github.com/Jip-Hop/4704ba4aa87c99f342b2846ed7885a5d)
* [Github > zzzhouuu > TrueNAS Nvidia Driver Build](https://github.com/zzzhouuu/truenas-nvidia-drivers)
* [zzzhouuu's manually compiled legacy GPU driver extensions](https://truenas-drivers.zhouyou.info/index.html)
