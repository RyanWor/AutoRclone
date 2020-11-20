import os
import json
import time
import glob
import psutil
import logging
import subprocess
import configparser

import filelock

from logging.handlers import RotatingFileHandler

# ------------Configuration item start------------------

# Account directory
sa_json_folder = r'/root/folderrclone/accounts'  # Absolute directory, no'/' at the end, no spaces in the path

# Rclone run command
# 1. Fill in what you are using/want to use, here is move, or copy/sync ......
# 2. It is recommended to add `--rc`, no matter if you don't add it, the script will automatically add it later
# 3. Because you can’t afford to screen, if you want to pay attention to the running status of rclone, please use `--log-file` to redirect rclone output to a file
cmd_rclone = 'rclone move /home/tomove GDrive:/tmp --drive-server-side-across-configs -v --log-file /tmp/rclone.log'

# Check rclone interval (s)
check_after_start = 60  # After pulling up the rclone process, check the rclone status only after resting xxs to prevent rclone rc core/stats from reporting an error and exiting
check_interval = 10  # The interval between the main process's rclone rc core/stats check

# rclone account replacement monitoring conditions
switch_sa_level = 1  # The number of rules that need to be met. The larger the number, the stricter the switching conditions. It must be less than the number of True (ie enabled) below, namely 1-4 (max)
switch_sa_rules = {
    'up_than_750': False,  # The current account has passed 750G
    'error_user_rate_limit': False,  # Rclone Directly prompt rate limit error
    'zero_transferred_between_check_interval': True,  # The amount of rclone transfer during the 100 check interval is 0
    'all_transfers_in_zero': False,  # All current transfers transfer size is 0
}

# rclone account switching method (runtime or config)
# runtime is to modify the additional `--drive-service-account-file` parameter when starting rclone
# config is to modify the rclone configuration file `$HOME/.config/rclone/rclone.conf`, at this time you need to specify the following rclone configuration parameters
switch_sa_way = 'runtime'

# rclone configuration parameters (used if and only when switch_sa_way is `config` and need to be modified)
rclone_config_path = '/root/.config/rclone/rclone.conf'  # Rclone configuration file location
rclone_dest_name = 'GDrive'  # Rclone destination name (same as corresponding in cmd_rclone, and ensure that SA has been added)

# Temporary file for this script
instance_lock_path = r'/tmp/autorclone.lock'
instance_config_path = r'/tmp/autorclone.conf'

# Run log of this script
script_log_file = r'/tmp/autorclone.log'
logging_datefmt = "%m/%d/%Y %I:%M:%S %p"
logging_format = "%(asctime)s - %(levelname)s - %(funcName)s - %(message)s"

# ------------End of configuration item------------------

# Run variables
instance_config = {}
sa_jsons = []

# Log related
logFormatter = logging.Formatter(fmt=logging_format, datefmt=logging_datefmt)

logger = logging.getLogger()
logger.setLevel(logging.NOTSET)
while logger.handlers:  # Remove un-format logging in Stream, or all of messages are appearing more than once.
    logger.handlers.pop()

if script_log_file:
    fileHandler = RotatingFileHandler(filename=script_log_file, mode='a',
                                      backupCount=2, maxBytes=5 * 1024 * 1024,
                                      encoding=None, delay=0)
    fileHandler.setFormatter(logFormatter)
    logger.addHandler(fileHandler)

consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(logFormatter)
logger.addHandler(consoleHandler)


def write_config(name, value):
    instance_config[name] = value
    with open(instance_config_path, 'w') as f:
        json.dump(instance_config, f, sort_keys=True)


# Get the next Service Account Credentials JSON file path
def get_next_sa_json_path(_last_sa):
    if _last_sa not in sa_jsons:  # Empty string or wrong sa_json_path, start from the beginning
        next_sa_index = 0
    else:
        _last_sa_index = sa_jsons.index(_last_sa)
        next_sa_index = _last_sa_index + 1
    # Exceed the list length to start from the beginning
    if next_sa_index > len(sa_jsons):
        next_sa_index = next_sa_index - len(sa_jsons)
    return sa_jsons[next_sa_index]


def switch_sa_by_config(cur_sa):
    # Get rclone configuration
    config = configparser.ConfigParser()
    config.read(rclone_config_path)

    if rclone_dest_name not in config:
        logger.critical('Can\'t find section %s in your rclone.conf (path: %s)', (rclone_dest_name, rclone_config_path))
        exit(1)

    # Change SA information
    sa_in_config = config[rclone_dest_name].get('service_account_file', '')
    config[rclone_dest_name]['service_account_file'] = cur_sa
    logger.info('Change rclone.conf SA information from %s to %s' % (sa_in_config, cur_sa))

    # Save
    with open(rclone_config_path, 'w') as configfile:
        config.write(configfile)

    logger.info('Change SA information in rclone.conf Success')


def get_email_from_sa(sa):
    return json.load(open(sa, 'r'))['client_email']


# Forcibly kill Rclone
def force_kill_rclone_subproc_by_parent_pid(sh_pid):
    if psutil.pid_exists(sh_pid):
        sh_proc = psutil.Process(sh_pid)
        logger.info('Get The Process information - pid: %s, name: %s' % (sh_pid, sh_proc.name()))
        for child_proc in sh_proc.children():
            if child_proc.name().find('rclone') > -1:
                logger.info('Force Killed rclone process which pid: %s' % child_proc.pid)
                child_proc.kill()


if __name__ == '__main__':
    # Singleton mode (￣y▽,￣)╭
    instance_check = filelock.FileLock(instance_lock_path)
    with instance_check.acquire(timeout=0):
        # Load account information
        sa_jsons = glob.glob(os.path.join(sa_json_folder, '*.json'))
        if len(sa_jsons) == 0:
            logger.error('No Service Account Credentials JSON file exists.')
            exit(1)

        # Load instance configuration
        if os.path.exists(instance_config_path):
            logger.info('Instance config exist, Load it...')
            config_raw = open(instance_config_path).read()
            instance_config = json.loads(config_raw)

        # Check the last recorded pid information
        if 'last_pid' in instance_config:
            last_pid = instance_config.get('last_pid')
            logger.debug('Last PID exist, Start to check if it is still alive')
            force_kill_rclone_subproc_by_parent_pid(last_pid)

        # Check the last recorded sa information, if any, rearrange sa_jsons
        # So we start with a new 750G every time
        last_sa = instance_config.get('last_sa', '')
        if last_sa in sa_jsons:
            logger.info('Get `last_sa` from config, resort list `sa_jsons`')
            last_sa_index = sa_jsons.index(last_sa)
            sa_jsons = sa_jsons[last_sa_index:] + sa_jsons[:last_sa_index]

        # Fix cmd_rclone to prevent missing `--rc`
        if cmd_rclone.find('--rc') == -1:
            logger.warning('Lost important param `--rc` in rclone commands, AutoAdd it.')
            cmd_rclone += ' --rc'

        # Account switching cycle
        while True:
            logger.info('Switch to next SA..........')
            last_sa = current_sa = get_next_sa_json_path(last_sa)
            write_config('last_sa', current_sa)
            logger.info('Get SA information, file: %s , email: %s' % (current_sa, get_email_from_sa(current_sa)))

            # Switch Rclone to run commands
            if switch_sa_way == 'config':
                switch_sa_by_config(current_sa)
                cmd_rclone_current_sa = cmd_rclone
            else:
                # It is regarded as `runtime` by default, with additional'--drive-service-account-file' parameter
                cmd_rclone_current_sa = cmd_rclone + ' --drive-service-account-file %s' % (current_sa,)

            # Start a subprocess to adjust rclone
            proc = subprocess.Popen(cmd_rclone_current_sa, shell=True)

            # Wait so that rclone is fully up
            logger.info('Wait %s seconds to full call rclone command: %s' % (check_after_start, cmd_rclone_current_sa))
            time.sleep(check_after_start)

            # Record pid information
            # Note that because the subprocess starts sh first, and then sh starts rclone, the pid information recorded here is actually sh
            # proc.pid + 1 is the pid of the rclone process in general, but not sure
            # So be sure to use force_kill_rclone_subproc_by_parent_pid(sh_pid) method to kill rclone
            write_config('last_pid', proc.pid)
            logger.info('Run Rclone command Success in pid %s' % (proc.pid + 1))

            # The main process uses `rclone rc core/stats` to check the status of the child processes
            cnt_error = 0
            cnt_403_retry = 0
            cnt_transfer_last = 0
            cnt_get_rate_limit = False
            while True:
                try:
                    response = subprocess.check_output('rclone rc core/stats', shell=True)
                except subprocess.CalledProcessError as error:
                    cnt_error = cnt_error + 1
                    err_msg = 'check core/stats failed for %s times,' % cnt_error
                    if cnt_error > 3:
                        logger.error(err_msg + ' Force kill exist rclone process %s.' % proc.pid)
                        proc.kill()
                        exit(1)

                    logger.warning(err_msg + ' Wait %s seconds to recheck.' % check_interval)
                    time.sleep(check_interval)
                    continue  # check again
                else:
                    cnt_error = 0

                # Parse `rclone rc core/stats` output
                response_json = json.loads(response.decode('utf-8').replace('\0', ''))
                cnt_transfer = response_json.get('bytes', 0)

                # Output current situation
                logger.info('Transfer Status - Upload: %s GiB, Avg upspeed: %s MiB/s, Transfered: %s.' % (
                    response_json.get('bytes', 0) / pow(1024, 3),
                    response_json.get('speed', 0) / pow(1024, 2),
                    response_json.get('transfers', 0)
                ))

                # Determine whether to switch
                should_switch = 0
                switch_reason = 'Switch Reason: '

                # Check if the current total upload exceeds 750 GB
                if switch_sa_rules.get('up_than_750', False):
                    if cnt_transfer > 750 * pow(1000, 3):  # 这里是 750GB 而不是 750GiB
                        should_switch += 1
                        switch_reason += 'Rule `up_than_750` hit, '

                # Check the amount of rclone transferred during monitoring
                if switch_sa_rules.get('zero_transferred_between_check_interval', False):
                    if cnt_transfer - cnt_transfer_last == 0:  # Not increased
                        cnt_403_retry += 1
                        if cnt_403_retry % 10 == 0:
                            logger.warning('Rclone seems not transfer in %s checks' % cnt_403_retry)
                        if cnt_403_retry >= 100:  # No increase after more than 100 inspections
                            should_switch += 1
                            switch_reason += 'Rule `zero_transferred_between_check_interval` hit, '
                    else:
                        cnt_403_retry = 0
                    cnt_transfer_last = cnt_transfer

                # Rclone directly prompts error 403 ratelimitexceed
                if switch_sa_rules.get('error_user_rate_limit', False):
                    last_error = response_json.get('lastError', '')
                    if last_error.find('userRateLimitExceeded') > -1:
                        should_switch += 1
                        switch_reason += 'Rule `error_user_rate_limit` hit, '

                # Rclone directly prompts error 403 ratelimitexceed
                if switch_sa_rules.get('all_transfers_in_zero', False):
                    graceful = True
                    if response_json.get('transferring', False):
                        for transfer in response_json['transferring']:
                            # Handle the situation where `bytes` or `speed` does not exist (think that the transfer has been completed) @yezi1000
                            if 'bytes' not in transfer or 'speed' not in transfer:
                                continue
                            elif transfer.get('bytes', 0) != 0 and transfer.get('speed', 0) > 0:  # There are currently unfinished transfers
                                graceful = False
                                break
                    if graceful:
                        should_switch += 1
                        switch_reason += 'Rule `all_transfers_in_zero` hit, '

                # Greater than the set replacement level
                if should_switch >= switch_sa_level:
                    logger.info('Transfer Limit may hit (%s), Try to Switch..........' % switch_reason)
                    force_kill_rclone_subproc_by_parent_pid(proc.pid)  # Kill the current rclone process
                    break  # Exit the main process monitoring loop and switch to the next account

                time.sleep(check_interval)