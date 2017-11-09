import os
import cmd
from collections import namedtuple
from pprint import pprint
import time
from traceback import print_exc

from bluetooth import (
    BluetoothSocket,
    RFCOMM,
)
from bluetooth.btcommon import BluetoothError
import matplotlib
from matplotlib import pyplot
from osbrain import (
    Agent,
    run_agent,
    run_nameserver,
)
import pandas


matplotlib.interactive(True)


LogFilter = namedtuple('LogFilter', 'level,function')


def complete_subcommands(text, subcommands):
    if not text:
        return subcommands
    return [c for c in subcommands if c.startswith(text)]


def log_as_dataframe(log):
    columns = ['timestamp', 'level', 'source', 'function', 'data']
    df = pandas.DataFrame(log, columns=columns)
    return df.set_index('timestamp').sort_index()


def explode_csv_series(series):
    def x(row):
        return [float(x) for x in row.split(',')]

    return series.apply(x).apply(pandas.Series)


def log_matches_filter(log, log_filter):
    if not log_filter:
        return False
    if log_filter.level and log_filter.level != log[1]:
        return False
    if log_filter.function and log_filter.function != log[3]:
        return False
    return True


class Proxy(Agent):
    def on_init(self):
        self.log = []
        self.buffer = b''
        self.log_filter = None
        self.filtered = None

    def setup(self, address, port):
        self.rfcomm = BluetoothSocket(RFCOMM)
        self.rfcomm.connect((address, port))
        self.rfcomm.settimeout(0.01)
        self.each(0, 'receive')

    def filter_next(self, level=None, function=None):
        self.log_filter = LogFilter(level=level, function=function)

    def wait_filtered(self, timeout=0.5):
        t0 = time.time()
        while True:
            self.receive()
            if self.filtered:
                print(self.filtered)
                break
            if time.time() - t0 > timeout:
                print('`wait_filtered` timed out!')
                break
        self.filtered = None
        self.log_filter = None

    def process_received(self, received):
        self.buffer += received
        if b'\n' not in self.buffer:
            return 0
        splits = self.buffer.split(b'\n')
        for message in splits[:-1]:
            fields = message.split(b',')
            log = [x.decode('utf-8') for x in fields[:4]]
            log[0] = float(log[0])
            body = b','.join(fields[4:])
            if not body.startswith(b'RAW'):
                body = body.decode('utf-8')
            else:
                raise NotImplementedError()
            log = tuple(log + [body])
            if log[1] == 'ERROR':
                print(log)
            if log_matches_filter(log, self.log_filter):
                self.filtered = log
                self.log_filter = None
            self.log.append(log)
        self.buffer = splits[-1]
        return len(splits) - 1

    def send(self, message):
        for retry in range(10):
            try:
                self.rfcomm.settimeout(1.)
                self.rfcomm.send(message)
            except Exception as error:
                print_exc()
            # Wait for the robot ACK
            t0 = time.time()
            while (time.time() - t0 < 0.1):
                received = self.receive()
                for i in range(received):
                    log = self.log[-1 - i]
                    body = log[-1]
                    if log[1] != 'DEBUG':
                        continue
                    if 'Processing' not in body:
                        continue
                    if body != 'Processing "%s"' % message.strip('\0'):
                        break
                    return True
        print('Command "%s" unsuccessful!' % message)
        return False

    def receive(self):
        try:
            self.rfcomm.settimeout(0.01)
            received = self.rfcomm.recv(1024)
            return self.process_received(received)
        except BluetoothError as error:
            if str(error) != 'timed out':
                raise
        return 0

    def tail(self, N):
        return self.log[-N:]

    def last_log_time(self):
        if not self.log:
            return 0
        return float(self.log[-1].split(b',')[0])


class Theseus(cmd.Cmd):
    prompt = '>>> '
    LOG_SUBCOMMANDS = ['all', 'clear']
    PLOT_SUBCOMMANDS = ['linear_speed_profile', 'angular_speed_profile']
    RUN_SUBCOMMANDS = [
        'angular_speed_profile',
        'linear_speed_profile',
        'static_turn_right_profile',
    ]
    SET_SUBCOMMANDS = [
        'micrometers_per_count ',
        'wheels_separation ',
        'max_linear_speed ',
        'linear_acceleration ',
        'linear_deceleration ',
        'angular_acceleration ',
        'kp_linear ',
        'kd_linear ',
        'kp_angular ',
        'kd_angular ',
        'ki_angular_side ',
        'ki_angular_front ',
        'side_sensors_error_factor ',
        'front_sensors_error_factor ',
    ]

    def cmdloop(self, intro=None):
        """Modified cmdloop() to handle keyboard interruptions."""
        while True:
            try:
                super().cmdloop(intro='')
                self.postloop()
                break
            except KeyboardInterrupt:
                print('^C')
                self.interrupted = True
                return False

    def emptyline(self):
        """Do nothing on empty line."""
        pass

    def preloop(self):
        self.interrupted = False
        self.ns = run_nameserver()
        self.proxy = run_agent('proxy', base=Proxy)
        self.proxy.after(0, 'setup', address='00:21:13:01:D1:59', port=1)

    def postloop(self):
        if not self.interrupted:
            self.ns.shutdown()

    def do_battery(self, *args):
        """Get battery voltage."""
        self.proxy.filter_next(function='log_battery_voltage')
        self.proxy.send('battery\0')
        self.proxy.wait_filtered()

    def do_configuration_variables(self, *args):
        """Get configuration variables."""
        self.proxy.filter_next(function='log_configuration_variables')
        self.proxy.send('configuration_variables\0')
        self.proxy.wait_filtered()

    def do_set(self, line):
        """Set robot variables."""
        if any(line.startswith(x) for x in self.SET_SUBCOMMANDS):
            self.proxy.send('set %s\0' % line)
            self.do_configuration_variables()
        else:
            print('Invalid set command "%s"!' % line)

    def do_clear(self, *args):
        """Clear screen."""
        os.system('clear')

    def do_log(self, extra):
        """Get the full log."""
        if extra == 'all':
            pprint(self.proxy.get_attr('log'))
        elif extra == 'clear':
            self.proxy.set_attr(log=[])
        elif extra.isnumeric():
            pprint(self.proxy.tail(int(extra)))
        else:
            pprint(self.proxy.tail(10))

    def do_plot(self, extra):
        """Plot different logged data."""
        if extra == 'linear_speed_profile':
            self.plot_linear_speed_profile()
        elif extra == 'angular_speed_profile':
            self.plot_angular_speed_profile()
        else:
            print('Please, specify what to plot!')

    def do_run(self, extra):
        """Run different procedures on the mouse."""
        if extra in self.RUN_SUBCOMMANDS:
            self.proxy.send('run %s\0' % extra)
        else:
            print('Please, specify what to run!')

    def complete_log(self, text, line, begidx, endidx):
        return complete_subcommands(text, self.LOG_SUBCOMMANDS)

    def complete_plot(self, text, line, begidx, endidx):
        return complete_subcommands(text, self.PLOT_SUBCOMMANDS)

    def complete_run(self, text, line, begidx, endidx):
        return complete_subcommands(text, self.RUN_SUBCOMMANDS)

    def complete_set(self, text, line, begidx, endidx):
        return complete_subcommands(text, self.SET_SUBCOMMANDS)

    def do_exit(self, *args):
        """Exit shell."""
        return True

    def do_EOF(self, line):
        """Exit shell."""
        return True

    def plot_linear_speed_profile(self):
        """Plot a linear profile out of the current log data."""
        df = log_as_dataframe(self.proxy.get_attr('log'))
        if not len(df):
            print('Empty dataframe...')
            return
        df = df[(df['level'] == 'INFO') &
                (df['function'] == 'log_linear_speed')]
        if not len(df):
            print('Empty dataframe...')
            return
        df = explode_csv_series(df['data'])
        speed_columns = ['target_speed', 'ideal_speed', 'left_speed',
                         'right_speed']
        pwm_columns = ['pwm_left', 'pwm_right']
        df.columns = speed_columns + pwm_columns
        fig, (ax1, ax2) = pyplot.subplots(nrows=2, ncols=1, sharex=True)
        for column in speed_columns:
            ax1.plot(df[column], label=column)
        for column in pwm_columns:
            ax2.plot(df[column], label=column)
        ax1.legend()
        ax2.legend()
        pyplot.show(block=False)

    def plot_angular_speed_profile(self):
        """Plot the angular speed profile with the current log data."""
        df = log_as_dataframe(self.proxy.get_attr('log'))
        if not len(df):
            print('Empty dataframe...')
            return
        df = df[(df['level'] == 'INFO') &
                (df['function'] == 'log_angular_speed')]
        if not len(df):
            print('Empty filtered dataframe...')
            return
        df = explode_csv_series(df['data'])
        speed_columns = ['target_speed', 'ideal_speed', 'angular_speed']
        pwm_columns = ['pwm_left', 'pwm_right']
        df.columns = speed_columns + pwm_columns
        fig, (ax1, ax2) = pyplot.subplots(nrows=2, ncols=1, sharex=True)
        for column in speed_columns:
            ax1.plot(df[column], label=column)
        for column in pwm_columns:
            ax2.plot(df[column], label=column)
        ax1.legend()
        ax2.legend()
        pyplot.show(block=False)


if __name__ == '__main__':
    Theseus().cmdloop()
