# set up logging before importing any other components
from config import get_version, initialize_logging; initialize_logging('flare')

import atexit
import glob
import logging
import os.path
import re
import simplejson as json
import subprocess
import sys
import tarfile
import tempfile
from time import strftime
from urlparse import urljoin

# DD imports
from checks.check_status import CollectorStatus, DogstatsdStatus, ForwarderStatus
from config import (
    check_yaml,
    get_confd_path,
    get_config,
    get_config_path,
    get_logging_config,
    get_os,
)
from util import (
    get_hostname,
    Platform,
)

# 3p
import requests


# Globals
log = logging.getLogger('flare')

def configcheck():
    osname = get_os()
    all_valid = True
    for conf_path in glob.glob(os.path.join(get_confd_path(osname), "*.yaml")):
        basename = os.path.basename(conf_path)
        try:
            check_yaml(conf_path)
        except Exception, e:
            all_valid = False
            print "%s contains errors:\n    %s" % (basename, e)
        else:
            print "%s is valid" % basename
    if all_valid:
        print "All yaml files passed. You can now run the Datadog agent."
        return 0
    else:
        print("Fix the invalid yaml files above in order to start the Datadog agent. "
                "A useful external tool for yaml parsing can be found at "
                "http://yaml-online-parser.appspot.com/")
        return 1

class Flare(object):
    """
    Compress all important logs and configuration files for debug,
    and then send them to Datadog (which transfers them to Support)
    """

    DATADOG_SUPPORT_URL = '/zendesk/flare'
    PASSWORD_REGEX = re.compile('( *(\w|_)*pass(word)?:).+')
    COMMENT_REGEX = re.compile('^ *#.*')
    APIKEY_REGEX = re.compile('^api_key: *\w+(\w{5})$')
    REPLACE_APIKEY = r'api_key: *************************\1'
    COMPRESSED_FILE = 'datadog-agent-{0}.tar.bz2'
    # We limit to 10MB arbitrary
    MAX_UPLOAD_SIZE = 10485000


    def __init__(self, cmdline=False, case_id=None):
        self._case_id = case_id
        self._cmdline = cmdline
        self._init_tarfile()
        self._save_logs_path()
        config = get_config()
        self._api_key = config.get('api_key')
        self._url = "{0}{1}".format(config.get('dd_url'), self.DATADOG_SUPPORT_URL)
        self._hostname = get_hostname(config)
        self._prefix = "datadog-{0}".format(self._hostname)

    # Collect all conf and logs files and compress them
    def collect(self):
        if not self._api_key:
            raise Exception('No api_key found')
        log.info("Collecting logs and configuration files:")

        self._add_logs_tar()
        self._add_conf_tar()
        log.info("  * datadog-agent configcheck output")
        self._add_command_output_tar('configcheck.log', configcheck)
        log.info("  * datadog-agent status output")
        self._add_command_output_tar('status.log', self._supervisor_status)
        log.info("  * datadog-agent info output")
        self._add_command_output_tar('info.log', self._info_all)

        log.info("Saving all files to {0}".format(self._tar_path))
        self._tar.close()

    # Upload the tar file
    def upload(self, confirmation=True):
        self._check_size()

        if confirmation:
            self._ask_for_confirmation()

        email = self._ask_for_email()

        log.info("Uploading {0} to Datadog Support".format(self._tar_path))
        url = self._url
        if self._case_id:
            url = urljoin(self._url, str(self._case_id))
        files = {'flare_file': open(self._tar_path, 'rb')}
        data = {
            'api_key': self._api_key,
            'case_id': self._case_id,
            'hostname': self._hostname,
            'email': email
        }
        r = requests.post(url, files=files, data=data)
        self._analyse_result(r)

    # Start by creating the tar file which will contain everything
    def _init_tarfile(self):
        # Default temp path
        self._tar_path = os.path.join(
            tempfile.gettempdir(),
            self.COMPRESSED_FILE.format(strftime("%Y-%m-%d-%H-%M-%S"))
        )

        if os.path.exists(self._tar_path):
            os.remove(self._tar_path)
        self._tar = tarfile.open(self._tar_path, 'w:bz2')

    # Save logs file paths
    def _save_logs_path(self):
        prefix = ''
        if Platform.is_windows():
            prefix = 'windows_'
        config = get_logging_config()
        self._collector_log = config.get('{0}collector_log_file'.format(prefix))
        self._forwarder_log = config.get('{0}forwarder_log_file'.format(prefix))
        self._dogstatsd_log = config.get('{0}dogstatsd_log_file'.format(prefix))
        self._jmxfetch_log = config.get('jmxfetch_log_file')

    # Add logs to the tarfile
    def _add_logs_tar(self):
        self._add_log_file_tar(self._collector_log)
        self._add_log_file_tar(self._forwarder_log)
        self._add_log_file_tar(self._dogstatsd_log)
        self._add_log_file_tar(self._jmxfetch_log)
        self._add_log_file_tar(
            "{0}/*supervisord.log*".format(os.path.dirname(self._collector_log))
        )

    def _add_log_file_tar(self, file_path):
        for f in glob.glob('{0}*'.format(file_path)):
            log.info("  * {0}".format(f))
            self._tar.add(
                f,
                os.path.join(self._prefix, 'log', os.path.basename(f))
            )

    # Collect all conf
    def _add_conf_tar(self):
        conf_path = get_config_path()
        log.info("  * {0}".format(conf_path))
        self._tar.add(
            self._strip_comment(conf_path),
            os.path.join(self._prefix, 'etc', 'datadog.conf')
        )

        if not Platform.is_windows():
            supervisor_path = os.path.join(
                os.path.dirname(get_config_path()),
                'supervisor.conf'
            )
            log.info("  * {0}".format(supervisor_path))
            self._tar.add(
                self._strip_comment(supervisor_path),
                os.path.join(self._prefix, 'etc', 'supervisor.conf')
            )

        for file_path in glob.glob(os.path.join(get_confd_path(), '*.yaml')):
            self._add_clean_confd(file_path)

    # Return path to a temp file without comment
    def _strip_comment(self, file_path):
        _, temp_path = tempfile.mkstemp(prefix='dd')
        atexit.register(os.remove, temp_path)
        temp_file = open(temp_path, 'w')
        orig_file = open(file_path, 'r').read()

        for line in orig_file.splitlines(True):
            if not self.COMMENT_REGEX.match(line):
                temp_file.write(re.sub(self.APIKEY_REGEX, self.REPLACE_APIKEY, line))
        temp_file.close()

        return temp_path

    # Remove password before collecting the file
    def _add_clean_confd(self, file_path):
        basename = os.path.basename(file_path)

        temp_path, password_found = self._strip_password(file_path)
        log.info("  * {0}{1}".format(file_path, password_found))
        self._tar.add(
            temp_path,
            os.path.join(self._prefix, 'etc', 'conf.d', basename)
        )

    # Return path to a temp file without password and comment
    def _strip_password(self, file_path):
        _, temp_path = tempfile.mkstemp(prefix='dd')
        atexit.register(os.remove, temp_path)
        temp_file = open(temp_path, 'w')
        orig_file = open(file_path, 'r').read()
        password_found = ''
        for line in orig_file.splitlines(True):
            if self.PASSWORD_REGEX.match(line):
                line = re.sub(self.PASSWORD_REGEX, r'\1 ********', line)
                password_found = ' - this file contains a password which '\
                                 'has been removed in the version collected'
            if not self.COMMENT_REGEX.match(line):
                temp_file.write(line)
        temp_file.close()

        return temp_path, password_found

    # Add output of the command to the tarfile
    def _add_command_output_tar(self, name, command):
        temp_file = os.path.join(tempfile.gettempdir(), name)
        if os.path.exists(temp_file):
            os.remove(temp_file)
        backup = sys.stdout
        sys.stdout = open(temp_file, 'w')
        command()
        sys.stdout.close()
        sys.stdout = backup
        self._tar.add(temp_file, os.path.join(self._prefix, name))
        os.remove(temp_file)

    # Print supervisor status (and nothing on windows)
    def _supervisor_status(self):
        if Platform.is_windows():
            print 'Windows - status not implemented'
        else:
            print '/etc/init.d/datadog-agent status'
            self._print_output_command(['/etc/init.d/datadog-agent', 'status'])
            print 'supervisorctl status'
            self._print_output_command(['/opt/datadog-agent/bin/supervisorctl',
                                        '-c', '/etc/dd-agent/supervisor.conf',
                                        'status'])

    # Print output of command
    def _print_output_command(self, command):
        try:
            status = subprocess.check_output(command, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError, e:
            status = 'Not able to get ouput, exit number {0}, exit ouput:\n'\
                     '{1}'.format(str(e.returncode), e.output)
        print status

    # Print info of all agent components
    def _info_all(self):
        CollectorStatus.print_latest_status(verbose=True)
        DogstatsdStatus.print_latest_status(verbose=True)
        ForwarderStatus.print_latest_status(verbose=True)

    # Check if the file is not too big before upload
    def _check_size(self):
        if os.path.getsize(self._tar_path) > self.MAX_UPLOAD_SIZE:
            log.info('{0} won\'t be uploaded, its size is too important.\n'\
                      'You can send it directly to support by mail.')
            sys.exit(1)

    # Function to ask for confirmation before upload
    def _ask_for_confirmation(self):
        print '{0} is going to be uploaded to Datadog.'.format(self._tar_path)
        choice = raw_input('Do you want to continue [Y/n]? ').lower()
        if choice not in ['yes', 'y', '']:
            print 'Aborting (you can still use {0})'.format(self._tar_path)
            sys.exit(1)

    # Ask for email if needed
    def _ask_for_email(self):
        if self._case_id:
            return None
        return raw_input('Please enter your email: ').lower()

    # Print output (success/error) of the request
    def _analyse_result(self, resp):
        if resp.status_code == 200:
            log.info("Your logs were successfully uploaded. For future reference,"\
                     " your internal case id is {0}".format(json.loads(resp.text)['case_id']))
        elif resp.status_code == 400:
            raise Exception('Your request is incorrect, error {0}'.format(resp.text))
        elif resp.status_code == 500:
            raise Exception('An error has occurred while uploading: {0}'.format(resp.text))
        else:
            raise Exception('An unknown error has occured: {0}\n'\
                            'Please contact support by email'.format(text))
