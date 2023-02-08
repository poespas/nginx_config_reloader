#!/usr/bin/env python
from __future__ import absolute_import
import argparse
import fnmatch
import pyinotify
import subprocess
import signal
import os
import logging
import logging.handlers
import shutil
import sys
import time

from nginx_config_reloader.copy_files import safe_copy_files
from nginx_config_reloader.settings import DIR_TO_WATCH, WATCH_IGNORE_FILES, MAGENTO1_CONF, MAGENTO2_CONF, MAGENTO_CONF, \
    FORBIDDEN_CONFIG_REGEX, ERROR_FILE, NGINX, BACKUP_CONFIG_DIR, CUSTOM_CONFIG_DIR, NGINX_PID_FILE, UNPRIVILEGED_GID, \
    UNPRIVILEGED_UID

logger = logging.getLogger(__name__)


class NginxConfigReloader(pyinotify.ProcessEvent):

    def my_init(
            self, logger=None, no_magento_config=False, no_custom_config=False, dir_to_watch=DIR_TO_WATCH,
            magento2_flag=None, notifier=None, use_systemd=False
    ):
        """Constructor called by ProcessEvent

        :param obj logger: The logger object
        :param bool no_magento_config: True if we should not install Magento configuration
        :param bool no_custom_config: True if we should not copy custom configuration
        :param str dir_to_watch: The directory to watch
        :param str magento2_flag: Magento 2 flag location
        """
        if not logger:
            self.logger = logging
        else:
            self.logger = logger
        self.no_magento_config = no_magento_config
        self.no_custom_config = no_custom_config
        self.dir_to_watch = dir_to_watch
        if not magento2_flag:
            self.magento2_flag = dir_to_watch + '/magento2.flag'
        else:
            self.magento2_flag = magento2_flag
        self.logger.info(self.dir_to_watch)
        self.notifier = notifier
        self.use_systemd = use_systemd

    def process_IN_DELETE(self, event):
        """Triggered by inotify on removal of file or removal of dir

        If the dir itself is removed, inotify will stop watching and also
        trigger IN_IGNORED.
        """
        if not event.dir:  # Will also capture IN_DELETE_SELF
            self.handle_event(event)

    def process_IN_MOVED(self, event):
        """Triggered by inotify when a file is moved from or to the dir"""
        self.handle_event(event)

    def process_IN_CREATE(self, event):
        """Triggered by inotify when a dir is created in the watch dir"""
        if event.dir:
            self.handle_event(event)

    def process_IN_CLOSE_WRITE(self, event):
        """Triggered by inotify when a file is written in the dir"""
        self.handle_event(event)

    def process_IN_IGNORED(self, event):
        """Triggered by inotify when it stops watching"""
        raise ListenTargetTerminated

    def process_IN_MOVE_SELF(self, event):
        """Triggered by inotify when watched dir is moved"""
        raise ListenTargetTerminated

    def handle_event(self, event):
        self.notifier._eventq.clear()

        if not any(fnmatch.fnmatch(event.name, pat) for pat in WATCH_IGNORE_FILES):
            self.logger.info("{} detected on {}.".format(event.maskname, event.name))
            self.apply_new_config()

    def install_magento_config(self):
        # Check if configs are present
        os.stat(MAGENTO1_CONF)
        os.stat(MAGENTO2_CONF)

        # Create new temporary filename for new config
        MAGENTO_CONF_NEW = MAGENTO_CONF + '_new'

        # Remove tmp link it it exists (leftover?)
        try:
            os.unlink(MAGENTO_CONF_NEW)
        except OSError:
            pass

        # Symlink new config to temporary filename
        if os.path.isfile(self.magento2_flag):
            os.symlink(MAGENTO2_CONF, MAGENTO_CONF_NEW)
        else:
            os.symlink(MAGENTO1_CONF, MAGENTO_CONF_NEW)

        # Move temporary symlink to actual location, overwriting existing link or file
        os.rename(MAGENTO_CONF_NEW, MAGENTO_CONF)

    def check_no_forbidden_config_directives_are_present(self):
        """
        Loop over the :FORBIDDEN_CONFIG_REGEX: to check if nginx config directory contains forbidden configuration
         options
        :return bool:
                        True    if forbidden config directives are present
                        False   if check couldn't find any forbidden config flags
        """
        if os.path.isdir(self.dir_to_watch):
            for rules in FORBIDDEN_CONFIG_REGEX:
                try:
                    # error file may contain messages that match a forbidden config pattern
                    # then validation could fail while the actual config is correct.
                    # we'll exclude the error file from searching for patterns,
                    # NOTE: exclusion of error_file requires to ensure the
                    # file is removed before moving it to nginx conf dir
                    # @TODO: use Python to search for forbidden configs instead
                    # of spawning external procs. Will have better testing
                    # and even may consume less system resources
                    check_external_resources = \
                        "[ $(grep -r --exclude={} -P '{}' '{}' | wc -l) -lt 1 ]".format(
                            ERROR_FILE, rules[0], self.dir_to_watch
                        )
                    subprocess.check_output(check_external_resources, shell=True)
                except subprocess.CalledProcessError:
                    error = "Unable to load config: {}".format(rules[1])
                    self.logger.error(error)
                    self.write_error_file(error)
                    return True
            return False

    def remove_error_file(self):
        """Try removing the error file. Return True on success or False on errors
        :rtype: bool
        """
        removed = False
        try:
            os.unlink(os.path.join(self.dir_to_watch, ERROR_FILE))
            removed = True
        except OSError:
            pass
        return removed

    def apply_new_config(self):
        if self.check_no_forbidden_config_directives_are_present():
            return False

        if not self.no_magento_config:
            try:
                self.install_magento_config()
            except OSError:
                self.logger.error("Installation of magento config failed")
                return False

        if not self.no_custom_config:
            try:
                self.fix_custom_config_dir_permissions()
                self.install_new_custom_config_dir()
            except (OSError, subprocess.CalledProcessError) as e:
                error_output = str(e)
                if hasattr(e, 'output'):
                    extra_output = e.output
                    if isinstance(e.output, bytes):
                        extra_output = extra_output.decode()
                    error_output += "\n\n{}".format(extra_output)
                self.logger.error("Installation of custom config failed")
                self.restore_old_custom_config_dir()
                self.write_error_file(error_output)
                return False

        try:
            subprocess.check_output([NGINX, '-t'], stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            self.logger.info("Config check failed")
            if not self.no_custom_config:
                self.restore_old_custom_config_dir()

            if isinstance(e.output, bytes):
                self.write_error_file(e.output.decode())
            else:
                self.write_error_file(e.output)

            return False
        else:
            self.remove_error_file()

        self.reload_nginx()
        return True

    def fix_custom_config_dir_permissions(self):
        try:
            os.chmod(self.dir_to_watch, 0o755)
            for root, dirs, _ in os.walk(self.dir_to_watch):
                for name in dirs:
                    os.chmod(os.path.join(root, name), 0o755)
        except subprocess.CalledProcessError:
            self.logger.info("Failed fixing permissions on watched directory")

    def install_new_custom_config_dir(self):
        self.remove_error_file()
        shutil.rmtree(BACKUP_CONFIG_DIR, ignore_errors=True)
        if os.path.exists(CUSTOM_CONFIG_DIR):
            shutil.move(CUSTOM_CONFIG_DIR, BACKUP_CONFIG_DIR)
        os.mkdir(CUSTOM_CONFIG_DIR)
        safe_copy_files(self.dir_to_watch, CUSTOM_CONFIG_DIR)

    def restore_old_custom_config_dir(self):
        shutil.rmtree(CUSTOM_CONFIG_DIR)
        if os.path.exists(BACKUP_CONFIG_DIR):
            shutil.move(BACKUP_CONFIG_DIR, CUSTOM_CONFIG_DIR)

    def reload_nginx(self):
        if self.use_systemd:
            subprocess.check_call(["systemctl", "reload", "nginx"])
        else:
            pid = self.get_nginx_pid()
            if not pid:
                self.logger.warning("Not reloading, nginx not running")
            else:
                self.logger.info("Reloading nginx config")
                os.kill(pid, signal.SIGHUP)

    def get_nginx_pid(self):
        try:
            with open(NGINX_PID_FILE, 'r') as f:
                return int(f.read())
        except (IOError, ValueError):
            return None

    def write_error_file(self, error):
        with open(os.path.join(self.dir_to_watch, ERROR_FILE), 'w') as f:
            f.write(error)


class ListenTargetTerminated(BaseException):
    pass


def wait_loop(logger=None, no_magento_config=False, no_custom_config=False, dir_to_watch=DIR_TO_WATCH,
              recursive_watch=False, use_systemd=False):
    """Main event loop

    There is an outer loop that checks the availability of the directory to watch.
    As soon as it becomes available, it starts an inotify-monitor that monitors
    configuration changes in an inner event loop. When the monitored directory is
    renamed or removed, the inotify-handler raises an exception to break out of the
    inner loop and we're back here in the outer loop.

    :param obj logger: The logger object
    :param bool no_magento_config: True if we should not install Magento configuration
    :param bool no_custom_config: True if we should not copy custom configuration
    :param str dir_to_watch: The directory to watch
    :param bool recursive_watch: True if we should watch the dir recursively
    :return None:
    """
    dir_to_watch = os.path.abspath(dir_to_watch)

    wm = pyinotify.WatchManager()
    notifier = pyinotify.Notifier(wm)

    nginx_config_changed_handler = NginxConfigReloader(
        logger=logger,
        no_magento_config=no_magento_config,
        no_custom_config=no_custom_config,
        dir_to_watch=dir_to_watch,
        notifier=notifier,
        use_systemd=use_systemd
    )

    class SymlinkChangedHandler(pyinotify.ProcessEvent):
        def process_IN_DELETE(self, event):
            if event.pathname == dir_to_watch:
                raise ListenTargetTerminated('watched directory was deleted')

    while True:
        while not os.path.exists(dir_to_watch):
            logger.warning("Configuration dir {} not found, waiting...".format(dir_to_watch))
            time.sleep(5)

        wm.add_watch(dir_to_watch, pyinotify.ALL_EVENTS, nginx_config_changed_handler,
                     rec=recursive_watch, auto_add=True)
        wm.watch_transient_file(dir_to_watch, pyinotify.ALL_EVENTS, SymlinkChangedHandler)

        # Install initial configuration
        nginx_config_changed_handler.apply_new_config()

        try:
            logger.info("Listening for changes to {}".format(dir_to_watch))
            notifier.coalesce_events()
            notifier.loop()
        except pyinotify.NotifierError as err:
            logger.critical(err)
        except ListenTargetTerminated:
            logger.warning("Configuration dir lost, waiting for it to reappear")


def as_unprivileged_user():
    os.setgid(UNPRIVILEGED_GID)
    os.setuid(UNPRIVILEGED_UID)


def parse_nginx_config_reloader_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--monitor', '-m', action='store_true', help='Monitor files on foreground with output')
    parser.add_argument(
        '--nomagentoconfig', action='store_true', help='Disable Magento configuration', default=False
    )
    parser.add_argument(
        '--nocustomconfig', action='store_true', help='Disable copying custom configuration', default=False
    )
    parser.add_argument('--watchdir', '-w', help='Set directory to watch', default=DIR_TO_WATCH)
    parser.add_argument('--recursivewatch', action='store_true', help='Enable recursive watching of subdirectories', default=False)
    parser.add_argument('--use-systemd', action='store_true', help='Reload nginx using systemd instead of process signal', default=False)
    return parser.parse_args()


def get_logger():
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(name)-12s %(levelname)-8s %(message)s'))
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger


def main():
    args = parse_nginx_config_reloader_arguments()
    log = get_logger()

    if args.monitor:
        # Track changed files in the nginx config dir and reload on change
        wait_loop(
            logger=log,
            no_magento_config=args.nomagentoconfig,
            no_custom_config=args.nocustomconfig,
            dir_to_watch=args.watchdir,
            recursive_watch=args.recursivewatch,
            use_systemd=args.use_systemd,
        )
        # should never return
        return 1
    else:
        # Reload the config once
        NginxConfigReloader(
            logger=log,
            no_magento_config=args.nomagentoconfig,
            no_custom_config=args.nocustomconfig,
            dir_to_watch=args.watchdir,
            use_systemd=args.use_systemd,
        ).apply_new_config()
        return 0


if __name__ == '__main__':
    sys.exit(main())
