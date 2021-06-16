#! /usr/bin/env python3

"""Reproduce SW-48083 again, with minimal dependencies

Jonathan's wbox setup looks like this:

dn40-re01<---ge100-0/0/3------ge100-0/0/3--->WC81917W80011<---ge100-0/0/18.2232------ge100-0/0/3.2232-->kvm29-ncc0
                                                                                          11.11.11.11

IxNetwork is running on win-client153
"""

import logging
import os
import sys
import textwrap
import time
import typing
import shlex
import argparse
from pathlib import Path

import contexttimer
import pexpect
import waiting

logger = logging.getLogger()


def bump_logging(delta, logger_name=None):
    """Adjust logging on one logger."""
    logger = logging.getLogger(logger_name)
    old_level = logger.getEffectiveLevel()
    logger.setLevel(old_level + delta)


def create_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        usage="check_dnos_pmtu.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    class IncreaseLogLevelAction(argparse.Action):
        def __call__(self, *args, **kwargs):
            bump_logging(-10)

    class DecreaseLogLevelAction(argparse.Action):
        def __call__(self, *args, **kwargs):
            bump_logging(-10)

    parser.add_argument(
        "-v",
        "--verbose",
        nargs=0,
        help="Increase logging level.",
        action=IncreaseLogLevelAction,
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-q",
        "--quiet",
        nargs=0,
        help="Decrease logging level.",
        action=DecreaseLogLevelAction,
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--do-clear-bgp-neighbors",
        action="store_true",
        dest="do_clear_bgp_neighbors",
        default=True,
        help="run `clear bgp neighbors *` (default)",
    )
    parser.add_argument(
        "--no-clear-bgp-neighbors",
        action="store_false",
        dest="do_clear_bgp_neighbors",
        help="keep bgp neighbors",
    )
    parser.add_argument(
        "--timeout-himss-reached",
        type=int,
        default=30,
        help="Timeout for initially reaching himss (30)",
    )
    parser.add_argument(
        "--timeout-lomss-reached",
        type=int,
        default=30,
        help="Timeout for initially reaching lomss (30)",
    )
    parser.add_argument(
        "--timeout-himss-restored",
        type=int,
        default=300,
        help="Timeout for initially reaching lomss (300)",
    )

    return parser


class Opts:
    client_dnos_hostname: str
    client_dnos_spawn_cmd: str
    middle_dnos_hostname: str
    middle_dnos_spawn_cmd: str
    server_dnos_hostname: str
    server_dnos_spawn_cmd: str

    iface_middle_client: str
    iface_middle_server: str
    iface_client: str
    iface_server: str
    ipaddr_client: typing.Optional[str] = None
    ipaddr_server: typing.Optional[str] = None

    lomtu = 2000
    himtu = 9100
    mss_margin = 100

    sshpass_filename = os.path.expanduser("~/.drivenets-default-dnroot-passwd.txt")

    do_clear_bgp_neighbors: bool
    timeout_himss_reached: int
    timeout_lomss_reached: int
    timeout_himss_restored: int
    steady_sleep_time = 3


class DNOSPexpectException(Exception):
    pass


def dnos_cmd(spawn, cmd, no_more=False, timeout=-1) -> str:
    """Run command on DNOS console via pexpect"""
    if no_more:
        cmd += " | no-more"
    spawn.sendline(cmd)
    result = spawn.expect(["ERROR:.*", "# $"])
    if result == 0:
        logger.warning("Received DNOS CLI ERROR: spawn=%s", spawn.buffer)
        raise DNOSPexpectException(f"DNOS CLI ERROR: {spawn!s}")
    elif result != 1:
        raise Exception(f"Unexpected expect result index {result}")

    output = spawn.before
    # strip old and new prompts:
    output = "\n".join(output.splitlines()[1:-1])
    return output


def dnos_wait_loading(spawn, timeout=-1):
    """Wait for the loading prompt"""
    logger.debug("wait ... dncli loading prompt")
    spawn.expect("DRIVENETS CLI Loading", timeout=timeout)
    logger.debug("received dncli loading prompt")
    spawn.expect("# $", timeout=timeout)
    logger.debug("received dncli command prompt")


def parse_table(input: str, expected_columns: int = None) -> typing.List[typing.List[str]]:
    """Parse a table with pipe separator.

    Returns a list of lists of strings
    Header not supported
    """
    table = []
    lines = input.splitlines()
    for line in lines:
        if line.strip() == "":
            continue
        row = line.split("|")
        if len(row) < 3:
            raise ValueError(
                f"Need at least two column separators, failed to parse line {line!r}"
            )
        if row[0].strip() != "":
            raise ValueError(f"Unexpected row heading {row[0]!r}")
        if row[-1].strip() != "":
            raise ValueError(f"Unexpected row trailer {row[-1]!r}")
        row = row[1:-1]
        row = [item.strip() for item in row]
        if expected_columns is not None and len(row) != expected_columns:
            raise ValueError(f"got {len(row)} columns instead of {expected_columns}")
        table.append(row)
    return table


class Main:
    last_mss_value: typing.Optional[int] = None

    def _pexpect_spawn_shell(self, cmd: str, **kw):
        logger.info("RUN: %s", cmd)
        return pexpect.spawn(
            "bash",
            ["-c", cmd],
            encoding="UTF-8",
            codec_errors="replace",
            timeout=120,
            echo=False,
            logfile=sys.stdout,
            **kw,
        )

    def init_dnos_setup(self):
        self.spawn_client = self._pexpect_spawn_shell(self.opts.client_dnos_spawn_cmd)
        self.spawn_middle = self._pexpect_spawn_shell(self.opts.middle_dnos_spawn_cmd)
        self.spawn_server = self._pexpect_spawn_shell(self.opts.server_dnos_spawn_cmd)
        dnos_wait_loading(self.spawn_client)
        dnos_wait_loading(self.spawn_middle)
        dnos_wait_loading(self.spawn_server)

    def init_opts(self, argv=None):
        self.opts = opts = Opts()

        # setup-specific settings:
        opts.client_dnos_hostname = "dn40-re01"
        opts.middle_dnos_hostname = "WC81917W80011"
        opts.server_dnos_hostname = "kvm29-ncc0"
        opts.iface_client = "ge100-0/0/3"
        opts.iface_middle_client = "ge100-0/0/3"
        opts.iface_middle_server = "ge100-0/0/18.2232"
        opts.iface_server = "ge100-0/0/18.2232"
        opts.ipaddr_client = "18.18.18.18"
        opts.ipaddr_server = "11.11.11.11"

        create_parser().parse_args(argv, self.opts)

        if not Path(opts.sshpass_filename).exists():
            raise Exception(f"Please write ssh password to {opts.sshpass_filename}")

        def _wrap_sshpass(hostname):
            return (
                f"sshpass -f {shlex.quote(opts.sshpass_filename)} ssh dnroot@{hostname}"
            )

        opts.client_dnos_spawn_cmd = _wrap_sshpass(opts.client_dnos_hostname)
        opts.middle_dnos_spawn_cmd = _wrap_sshpass(opts.middle_dnos_hostname)
        opts.server_dnos_spawn_cmd = _wrap_sshpass(opts.server_dnos_hostname)

    def set_middle_pmtu(self, mtu: int):
        mtu_switch_dnos = self.spawn_middle
        mtu_switch_iface = self.opts.iface_middle_server
        with contexttimer.Timer() as t:
            script = textwrap.dedent(
                f"""\
                configure
                    interface {mtu_switch_iface} mtu {mtu}
                    commit
                exit
                """
            )
            dnos_cmd(mtu_switch_dnos, script)
            logger.info("mtu increased in %.3f seconds", t.elapsed)

    def read_last_mss(self) -> int:
        cmd = "show system sessions"
        if self.opts.ipaddr_server:
            cmd += f" | include {self.opts.ipaddr_server}"
        if self.opts.ipaddr_client:
            cmd += f" | include {self.opts.ipaddr_client}"
        # last included value is highlighted which makes parsing harder
        cmd += " | include 179"

        session_output = dnos_cmd(self.spawn_client, cmd)
        #logger.info("%r", session_output)

        found_entry = None
        tab = parse_table(session_output)
        #logger.info("table:\n%r", tab)
        for row in tab:
            if row[1] != 'TCP':
                logger.debug("skip non-TCP %r", row)
                continue
            if row[4] != 'ESTABLISHED':
                logger.debug("skip non-ESTABLISHED %r", row)
                continue
            if self.opts.ipaddr_client:
                row_ipaddr_client = row[2].split(":")[0]
                if row_ipaddr_client != self.opts.ipaddr_client:
                    logger.debug("skip unrelated client ipaddr %r on %r", row_ipaddr_client, row)
                    continue
            if self.opts.ipaddr_server:
                row_ipaddr_server = row[3].split(":")[0]
                if row_ipaddr_server != self.opts.ipaddr_server:
                    logger.debug("skip unrelated server ipaddr %r on %r", row_ipaddr_server, row)
                    continue
            if found_entry:
                raise Exception("Multiple matching TCP sessions %r and %r" % (found_row, row))
            found_entry = row

        if not found_entry:
            raise ValueError("No matching TCP sessions")

        return int(found_entry[7])

    def try_read_last_mss(self) -> typing.Optional[int]:
        try:
            value = self.read_last_mss()
        except:
            logger.warning("failed to determine current mss", exc_info=True)
            return None
        self.last_mss_value = value
        return value

    def check_lomss_reached(self):
        mss = self.try_read_last_mss()
        logger.info("waiting for mss=%r below lomtu=%r", mss, self.opts.lomtu)
        return mss and mss <= self.opts.lomtu

    def check_himss_reached(self):
        mss = self.try_read_last_mss()
        logger.info("waiting for mss=%r nearing himtu=%r", mss, self.opts.himtu)
        return mss and mss >= self.opts.himtu - self.opts.mss_margin

    def check_himss_restored(self):
        mss = self.try_read_last_mss()
        logger.info("waiting to restore mss=%r nearing himtu=%r", mss, self.opts.himtu)
        return mss and mss >= self.opts.himtu - self.opts.mss_margin

    def steady_sleep(self):
        if self.opts.steady_sleep_time:
            logger.info(
                "sleep %.3f seconds in steady state", self.opts.steady_sleep_time
            )
            time.sleep(self.opts.steady_sleep_time)

    def _verbose_wait(self, func, timeout_seconds: int, message: str):
        with contexttimer.Timer() as t:
            waiting.wait(func, timeout_seconds=timeout_seconds)
            logger.info("ok - %s in %.3fsec", message, t.elapsed)

    def run_pmtu_test(self):
        dnos_cmd(self.spawn_server, "show bgp summary", no_more=True)
        dnos_cmd(self.spawn_client, "show bgp summary", no_more=True)

        # reach himtu:
        self.set_middle_pmtu(self.opts.himtu)
        if self.opts.do_clear_bgp_neighbors:
            dnos_cmd(self.spawn_client, "clear bgp neighbor *")
        self._verbose_wait(
            self.check_himss_reached,
            timeout_seconds=self.opts.timeout_himss_reached,
            message="reached hi mss",
        )
        self.steady_sleep()

        # reach lomtu:
        self.set_middle_pmtu(self.opts.lomtu)
        self._verbose_wait(
            self.check_lomss_reached,
            timeout_seconds=self.opts.timeout_lomss_reached,
            message="reached lo mss",
        )
        self.steady_sleep()

        # restored himtu:
        self.set_middle_pmtu(self.opts.himtu)
        self._verbose_wait(
            self.check_himss_restored,
            timeout_seconds=self.opts.timeout_himss_restored,
            message="reached restored hi mss",
        )

    def init_logging(self):
        logging.basicConfig(level=logging.INFO)

    def main(self, argv=None):
        try:
            self.init_logging()
            self.init_opts(argv)
            self.init_dnos_setup()
            self.run_pmtu_test()
        finally:
            logger.info("last mss value: %s", self.last_mss_value)


if __name__ == "__main__":
    Main().main()
