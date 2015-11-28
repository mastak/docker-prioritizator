#!/usr/bin/env python3
import json
import os
import re
import sys
import subprocess
import time
from functools import partial


BASE_PATH = os.path.dirname(os.path.abspath(__file__))
ENV_TYPE_PATH = os.path.join(BASE_PATH, '.env_type')
CONFIG_PATH = os.path.join(BASE_PATH, 'config.json')
LOCAL_CONFIG_PATH = os.path.join(BASE_PATH, 'local_config.json')


class DockerComposeWrap(object):
    def __init__(self, env_config):
        self.env_config = env_config

    def config(self, name, default=None):
        return self.env_config.get(name, default)

    def cmd(self, *cmd_args, is_return=False, shell=False, quiet=False):
        if not quiet:
            print(' '.join(cmd_args))
        if is_return:
            returncode, result = subprocess.getstatusoutput(' '.join(cmd_args))
            # print(result)
        else:
            if shell:
                cmd_args = ' '.join(cmd_args)
            returncode = result = subprocess.call(cmd_args, shell=shell)

        if returncode != 0:
            raise subprocess.CalledProcessError(returncode=returncode, cmd=' '.join(cmd_args))

        return result

    def container_exec(self, container, *args, is_return=False, shell=False):
        container_id = self.container_id(container)
        command = ['docker', 'exec', '-i', container_id]
        command.extend(args)
        return self.cmd(*command, is_return=is_return, shell=shell)

    def container_run(self, container, *args, is_return=False):
        self._docker_compose('run', '--rm', container, *args, is_return=is_return)

    def container_id(self, container):
        cmd_args = self._get_compose_command('ps', '-q {}'.format(container))
        return self.cmd(*cmd_args, is_return=True, quiet=True)

    def reset_db(self):
        db_id = self.container_id('db')
        if db_id:
            remove = prompt("DB already exists, do you want remove old? y/n", default='n',
                            validate="^(y|n|N|Y)$")
            if remove.lower() == 'n':
                return
            self._docker_compose('stop', 'db')
            self._docker_compose('rm', '-f', 'db')
        self._docker_compose('up', '-d', 'db')

        # TODO: load all /files/db/*.sql
        # self._wait_db()
        # args = ['psql']
        # for db_file in db_files:
        #     restore_db = os.path.join(BASE_PATH, db_file)
        #     self.container_exec('db', *(args + ['<', restore_db]), shell=True)

    def deploy(self):
        self._docker_compose('build')
        self._docker_compose('up', '-d', 'db')

        desire_current_id = self.container_id('desire')
        if self._is_uwsgi():
            self._wait_db()
            self.container_run('desire', 'python', '/usr/src/app/manage.py', 'migrate', '--noinput')
        else:
            if not desire_current_id:
                self.container_run('desire', 'virtualenv', '/usr/src/env')
            self.container_run('desire', '/usr/src/env/bin/pip', 'install', '-r', '/usr/src/app/requirements.txt')
            self.container_run('desire', '/usr/src/env/bin/python', '/usr/src/app/manage.py', 'collectstatic', '--noinput')
            self._wait_db()
            self.container_run('desire', '/usr/src/env/bin/python', '/usr/src/app/manage.py', 'migrate', '--noinput')

        if desire_current_id:
            self.desire_current_id('scale', 'desire=2')
            desire_ids = str(desire_current_id).split("\n")
            self.cmd('docker', 'stop', *desire_ids)
            self.cmd('docker', 'rm', *desire_ids)

        self.start()

    def start(self):
        self._docker_compose('up', '-d', 'db')
        self._wait_db()
        self._docker_compose('up', '-d')

    def attach(self):
        desire_current_id = self.container_id('desire')
        self.cmd('docker', 'attach', desire_current_id)

    def bash(self):
        desire_current_id = self.container_id('desire')
        self.cmd('docker', 'exec', '-it', desire_current_id, 'bash')

    def clean_cache(self):
        desire_current_id = self.container_id('redis')
        self.cmd('docker', 'exec', '-it', desire_current_id, 'redis-cli', 'flushall')

    def get_env(self, name, default=None):
        with open(self.config('envs'), 'r') as f:
            for line in f:
                if not line.upper().startswith(name.upper()):
                    continue
                return line.split('=', 1)[1].strip('"\'\n ')
        return default

    def __getattr__(self, item):
        return partial(self._docker_compose, item)

    def _docker_compose(self, command,  *args, is_return=False):
        command = self._get_compose_command(command, *args)
        return self.cmd(*command, is_return=is_return)

    def _get_compose_command(self, *args):
        project_name = self.config('project_name')
        compose_files = self.config('compose_files', [])

        self._init_compose_env_vars()
        cmd_args = ["docker-compose"]
        if project_name:
            cmd_args.append("-p {}".format(project_name))

        for compose in compose_files:
            cmd_args.extend(["-f", os.path.join(BASE_PATH, compose)])

        if args:
            cmd_args.extend(args)
        return cmd_args

    def _init_compose_env_vars(self):
        envs_path = os.path.join(BASE_PATH, self.config('envs'))
        os.environ.setdefault('ENV_PATH', envs_path)

    def _is_uwsgi(self):
        return self.config('is_uwsgi', False)

    def _wait_db(self):
        db_user = self.get_env('POSTGRES_USER')
        db_password = self.get_env('POSTGRES_PASSWORD')

        db_command = ['psql', '-U', db_user, '-l']
        exit_status = 1
        while exit_status:
            try:
                exit_status = self.container_exec('db', *db_command)
            except subprocess.CalledProcessError as e:
                print(e)
                time.sleep(0.8)


def prompt(text, default='', validate=None):
    prompt_str = text.strip() + (default and " [%s] " % str(default).strip() or " ")
    value = None
    while value is None:
        value = input(prompt_str) or default
        if not validate:
            break

        if callable(validate):
            try:
                value = validate(value)
            except Exception as e:
                value = None
                print("Validation failed for the following reason:")
                print("    {}\n".format(e))
        else:
            result = re.findall(validate, value)
            if not result:
                print("Regular expression validation failed: '%s' does not match '%s'\n" %
                      (value, validate))
                value = None
    return value


def setup_env_type(envs_types):
    envs = ["{} - {}".format(v, i) for i, v in enumerate(envs_types)]
    prompt_text = "{}\nPlease specify target environment: ".format("\n".join(envs))
    validate_pattern = "|".join(map(str, range(len(envs_types))))

    environment_num = int(prompt(prompt_text, validate="^{}$".format(validate_pattern)))
    environment = envs_types[environment_num]
    with open(ENV_TYPE_PATH, 'w') as f:
        f.write(environment)
    return environment


def get_env_cofig(config):
    envs_types = list(config.keys())
    env_type = None
    if os.path.exists(ENV_TYPE_PATH):
        with open(ENV_TYPE_PATH, 'r') as f:
            env_type = f.readline().strip('"\'\n ')

    if env_type is None or env_type not in envs_types:
        env_type = setup_env_type(envs_types)

    return config[env_type]


def load_config(file_path):
    with open(file_path, 'r') as f:
        return json.loads(f.read())


def main(*argv):
    config = load_config(CONFIG_PATH)
    try:
        local_config = load_config(LOCAL_CONFIG_PATH)
    except FileNotFoundError:
        pass
    else:
        config.update(local_config)

    env_config = get_env_cofig(config)

    compose = DockerComposeWrap(env_config=env_config)
    if len(argv) == 1:
        print('help')
        exit()

    cmd = argv[1]
    cmd_args = argv[2:] if len(argv) > 2 else []
    getattr(compose, cmd)(*cmd_args)

if __name__ == '__main__':
    main(*sys.argv)
